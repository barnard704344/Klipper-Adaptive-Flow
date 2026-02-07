#!/usr/bin/env python3
"""
Adaptive Flow Moonraker Integration

This script monitors print jobs and automatically runs analysis after each print.
It can optionally auto-apply safe tuning suggestions.

Installation:
    1. Copy to printer: ~/Klipper-Adaptive-Flow/moonraker_hook.py
    2. Add to moonraker.conf (see below)
    3. Restart Moonraker

Add to moonraker.conf:
    [job_queue]
    
    [notifier print_complete]
    url: json://localhost:7125/server/custom/adaptive_flow_analyze
    events: complete
    body: {"filename": "{event_args[0].filename}", "status": "{event_args[0].state}"}

Or simpler - run as systemd service (see install instructions at bottom)
"""

import os
import sys
import json
import time
import logging
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import urllib.request

# =============================================================================
# CONFIGURATION - Loaded from analysis_config.cfg or defaults
# =============================================================================
CONFIG = {
    'listen_port': 7126,  # Port for webhook listener
    'moonraker_url': 'http://localhost:7125',
    'analyze_script': os.path.expanduser('~/Klipper-Adaptive-Flow/analyze_print.py'),
    'auto_apply': False,  # Set True to auto-apply safe suggestions
    'notify_console': True,  # Send results to Klipper console
    'log_file': os.path.expanduser('~/printer_data/logs/adaptive_flow_hook.log'),
    'provider': None,  # LLM provider: github (free), openai, anthropic
    'hook_mode': 'poll',  # poll or webhook
}


def load_config_file():
    """Load settings from analysis_config.cfg if it exists."""
    config_paths = [
        os.path.join(os.path.dirname(__file__), 'analysis_config.cfg'),
        os.path.expanduser('~/Klipper-Adaptive-Flow/analysis_config.cfg'),
        os.path.expanduser('~/printer_data/config/analysis_config.cfg'),
    ]
    
    for config_path in config_paths:
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#') or line.startswith('['):
                            continue
                        if ':' in line:
                            key, value = line.split(':', 1)
                            key = key.strip()
                            value = value.strip()
                            
                            if not value:
                                continue
                            
                            # Parse booleans
                            if value.lower() == 'true':
                                value = True
                            elif value.lower() == 'false':
                                value = False
                            elif value.isdigit():
                                value = int(value)
                            
                            # Map config keys
                            if key == 'provider':
                                CONFIG['provider'] = value
                            elif key == 'auto_apply':
                                CONFIG['auto_apply'] = value
                            elif key == 'notify_console':
                                CONFIG['notify_console'] = value
                            elif key == 'moonraker_url':
                                CONFIG['moonraker_url'] = value
                            elif key == 'hook_mode':
                                CONFIG['hook_mode'] = value
                            elif key == 'webhook_port' and isinstance(value, int):
                                CONFIG['listen_port'] = value
                
                return config_path
            except Exception as e:
                pass  # Will log after logger is set up
    
    return None


# Load config before setting up logging
_config_file = load_config_file()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(CONFIG['log_file']),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('AdaptiveFlowHook')

if _config_file:
    logger.info(f"Loaded config from {_config_file}")


def send_console_message(message, max_line_length=100):
    """Send a message to Klipper console via Moonraker.
    
    Long messages are split into multiple lines to avoid truncation.
    """
    try:
        # Escape the message for G-code
        safe_msg = message.replace('"', "'").replace('\n', ' ')
        
        # Split long messages into multiple lines
        if len(safe_msg) <= max_line_length:
            lines = [safe_msg]
        else:
            # Word-wrap the message
            lines = []
            words = safe_msg.split(' ')
            current_line = ''
            for word in words:
                if len(current_line) + len(word) + 1 <= max_line_length:
                    current_line = f"{current_line} {word}".strip()
                else:
                    if current_line:
                        lines.append(current_line)
                    current_line = word
            if current_line:
                lines.append(current_line)
        
        url = f"{CONFIG['moonraker_url']}/printer/gcode/script"
        
        for line in lines:
            gcode = f'RESPOND MSG="{line}"'
            data = json.dumps({'script': gcode}).encode('utf-8')
            req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
            
            with urllib.request.urlopen(req, timeout=5) as response:
                pass
    except Exception as e:
        logger.debug(f"Console message failed: {e}")


def run_analysis(auto_apply=False, provider=None):
    """Run the print analysis script."""
    cmd = [sys.executable, CONFIG['analyze_script']]
    if auto_apply:
        cmd.append('--auto')
    if provider:
        cmd.extend(['--provider', provider])
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, 'PYTHONUNBUFFERED': '1'}
        )
        
        logger.info(f"Analysis completed with return code {result.returncode}")
        
        if result.stdout:
            logger.info(f"Output:\n{result.stdout}")
        if result.stderr:
            logger.warning(f"Stderr:\n{result.stderr}")
        
        # Send key results to Klipper console
        if CONFIG['notify_console']:
            if result.returncode != 0:
                send_console_message(f"AF: Analysis failed (exit code {result.returncode})")
                if result.stderr:
                    # Send first line of error
                    err_line = result.stderr.strip().split('\n')[0][:80]
                    send_console_message(f"AF: Error: {err_line}")
                return False
            
            if not result.stdout.strip():
                send_console_message("AF: Analysis completed but no output generated")
                return True
            
            lines = result.stdout.split('\n')
            sent_count = 0
            for line in lines:
                # Skip decorative lines
                if line.startswith('===') or line.startswith('---') or not line.strip():
                    continue
                
                # Check if line contains bullet point or continuation
                stripped = line.strip()
                is_bullet_or_continuation = stripped.startswith('•') or stripped.startswith('... ')
                
                # Send informative lines
                if is_bullet_or_continuation or any(keyword in line for keyword in [
                    'Quality:', 'ANALYSIS:', 'ALL GOOD', 'No issues',
                    'critical issue', 'other issue', 'suggestion',
                    'Report:', 'Mainsail:', '🎉', '✅', '⚠️', '❌', '🔴', '🟡', '💡'
                ]):
                    clean_line = line.strip()
                    send_console_message(f"AF: {clean_line}")
                    sent_count += 1
            
            if sent_count == 0:
                # Fallback: send first few non-empty lines
                send_console_message("AF: Analysis complete (see log for details)")
        
        return result.returncode == 0
        
    except subprocess.TimeoutExpired:
        logger.error("Analysis timed out")
        if CONFIG['notify_console']:
            send_console_message("AF: Analysis timed out after 120 seconds")
        return False
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        if CONFIG['notify_console']:
            send_console_message(f"AF: Analysis error: {str(e)[:60]}")
        return False


class WebhookHandler(BaseHTTPRequestHandler):
    """Handle incoming webhooks from Moonraker."""
    
    def log_message(self, format, *args):
        logger.debug(f"HTTP: {format % args}")
    
    def do_POST(self):
        """Handle POST requests (print complete notifications)."""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            
            logger.info(f"Received webhook: {self.path}")
            logger.debug(f"Body: {body}")
            
            if 'adaptive_flow_analyze' in self.path:
                # Parse the notification
                try:
                    data = json.loads(body) if body else {}
                    filename = data.get('filename', 'unknown')
                    status = data.get('status', 'unknown')
                    
                    logger.info(f"Print complete: {filename} ({status})")
                    
                    if status in ['complete', 'completed']:
                        # Give logging a moment to flush
                        time.sleep(2)
                        
                        # Run analysis
                        send_console_message("AF: Analyzing print session...")
                        run_analysis(auto_apply=CONFIG['auto_apply'], provider=CONFIG['provider'])
                    
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON in webhook body: {body}")
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
            
        except Exception as e:
            logger.error(f"Webhook error: {e}")
            self.send_response(500)
            self.end_headers()
    
    def do_GET(self):
        """Handle GET requests (health check, manual trigger)."""
        parsed = urlparse(self.path)
        
        if parsed.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status": "healthy"}')
        
        elif parsed.path == '/analyze':
            # Manual trigger
            logger.info("Manual analysis triggered")
            send_console_message("AF: Manual analysis triggered...")
            
            params = parse_qs(parsed.query)
            auto_apply = params.get('auto', ['0'])[0] == '1'
            provider = params.get('provider', [CONFIG['provider']])[0]
            
            success = run_analysis(auto_apply=auto_apply, provider=provider)
            
            self.send_response(200 if success else 500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': success}).encode())
        
        else:
            self.send_response(404)
            self.end_headers()


def monitor_print_state():
    """Alternative: Poll Moonraker for print state changes."""
    last_state = None
    
    while True:
        try:
            url = f"{CONFIG['moonraker_url']}/printer/objects/query?print_stats"
            with urllib.request.urlopen(url, timeout=5) as response:
                data = json.loads(response.read().decode())
            
            state = data.get('result', {}).get('status', {}).get('print_stats', {}).get('state', '')
            
            if last_state == 'printing' and state == 'complete':
                logger.info("Print completed (detected via polling)")
                time.sleep(3)  # Let logs flush
                send_console_message("AF: Analyzing print session...")
                run_analysis(auto_apply=CONFIG['auto_apply'], provider=CONFIG['provider'])
            
            last_state = state
            
        except Exception as e:
            logger.debug(f"Poll error: {e}")
        
        time.sleep(5)


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Adaptive Flow Moonraker Integration',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
LLM Providers:
  github      GitHub Models (FREE!) - set GITHUB_TOKEN
  openai      ChatGPT / GPT-4o-mini - set OPENAI_API_KEY
  anthropic   Claude - set ANTHROPIC_API_KEY

Examples:
  python3 moonraker_hook.py --provider github
  python3 moonraker_hook.py --auto-apply
        """
    )
    
    # Use config file defaults
    default_mode = CONFIG.get('hook_mode', 'poll')
    default_port = CONFIG.get('webhook_port', 7126)
    default_auto_apply = CONFIG.get('auto_apply', False)
    default_provider = CONFIG.get('provider')
    
    parser.add_argument('--mode', choices=['webhook', 'poll'], default=default_mode,
                        help=f'webhook=listen for notifications, poll=check print state (default: {default_mode})')
    parser.add_argument('--port', type=int, default=default_port,
                        help=f'Port for webhook listener (default: {default_port})')
    parser.add_argument('--auto-apply', action='store_true', default=default_auto_apply,
                        help=f'Auto-apply safe suggestions (default: {default_auto_apply})')
    parser.add_argument('--provider', '-p',
                        choices=['github', 'openai', 'anthropic'],
                        default=default_provider,
                        help='LLM provider to use (from config or env)')
    args = parser.parse_args()
    
    CONFIG['auto_apply'] = args.auto_apply
    CONFIG['listen_port'] = args.port
    CONFIG['provider'] = args.provider
    
    provider_str = args.provider or 'auto-detect from API key'
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, 'analysis_config.cfg')
    if os.path.exists(config_path):
        logger.info(f"Config loaded from: {config_path}")
    logger.info(f"Starting Adaptive Flow hook (mode={args.mode}, provider={provider_str}, auto_apply={args.auto_apply})")
    
    if args.mode == 'webhook':
        server = HTTPServer(('0.0.0.0', args.port), WebhookHandler)
        logger.info(f"Webhook server listening on port {args.port}")
        logger.info(f"  Health check: http://localhost:{args.port}/health")
        logger.info(f"  Manual trigger: http://localhost:{args.port}/analyze")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
    else:
        # Polling mode - simpler, no Moonraker config needed
        logger.info("Polling Moonraker for print state changes...")
        monitor_print_state()


if __name__ == '__main__':
    main()


# =============================================================================
# INSTALLATION AS SYSTEMD SERVICE
# =============================================================================
"""
To run automatically on boot, create a systemd service:

1. Configure analysis_config.cfg with your API key and preferred settings

2. Create service file:
   sudo nano /etc/systemd/system/adaptive-flow-hook.service

3. Add this content:
   [Unit]
   Description=Adaptive Flow Print Analyzer Hook
   After=moonraker.service
   Requires=moonraker.service

   [Service]
   Type=simple
   User=pi
   WorkingDirectory=/home/pi/Klipper-Adaptive-Flow
   ExecStart=/usr/bin/python3 /home/pi/Klipper-Adaptive-Flow/moonraker_hook.py
   Restart=always
   RestartSec=10

   [Install]
   WantedBy=multi-user.target

   Note: All settings come from analysis_config.cfg - no need for Environment= lines

4. Enable and start:
   sudo systemctl daemon-reload
   sudo systemctl enable adaptive-flow-hook
   sudo systemctl start adaptive-flow-hook

5. Check status:
   sudo systemctl status adaptive-flow-hook
   journalctl -u adaptive-flow-hook -f
"""
