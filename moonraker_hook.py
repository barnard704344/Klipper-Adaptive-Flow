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
# CONFIGURATION
# =============================================================================
CONFIG = {
    'listen_port': 7126,  # Port for webhook listener
    'moonraker_url': 'http://localhost:7125',
    'analyze_script': os.path.expanduser('~/Klipper-Adaptive-Flow/analyze_print.py'),
    'auto_apply': False,  # Set True to auto-apply safe suggestions
    'notify_console': True,  # Send results to Klipper console
    'log_file': os.path.expanduser('~/printer_data/logs/adaptive_flow_hook.log'),
}

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


def send_console_message(message):
    """Send a message to Klipper console via Moonraker."""
    try:
        # Escape the message for G-code
        safe_msg = message.replace('"', "'").replace('\n', ' ')[:100]
        gcode = f'RESPOND MSG="{safe_msg}"'
        
        url = f"{CONFIG['moonraker_url']}/printer/gcode/script"
        data = json.dumps({'script': gcode}).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
        
        with urllib.request.urlopen(req, timeout=5) as response:
            pass
    except Exception as e:
        logger.debug(f"Console message failed: {e}")


def run_analysis(auto_apply=False):
    """Run the print analysis script."""
    cmd = [sys.executable, CONFIG['analyze_script']]
    if auto_apply:
        cmd.append('--auto')
    
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
        
        # Extract key info for console notification
        if CONFIG['notify_console']:
            lines = result.stdout.split('\n')
            for line in lines:
                if 'Assessment:' in line or 'Quality Prediction:' in line:
                    send_console_message(f"AF: {line.strip()}")
                elif 'Issues Found' in line:
                    send_console_message(f"AF: {line.strip()}")
        
        return result.returncode == 0
        
    except subprocess.TimeoutExpired:
        logger.error("Analysis timed out")
        return False
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
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
                        run_analysis(auto_apply=CONFIG['auto_apply'])
                    
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
            
            success = run_analysis(auto_apply=auto_apply)
            
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
                run_analysis(auto_apply=CONFIG['auto_apply'])
            
            last_state = state
            
        except Exception as e:
            logger.debug(f"Poll error: {e}")
        
        time.sleep(5)


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Adaptive Flow Moonraker Integration')
    parser.add_argument('--mode', choices=['webhook', 'poll'], default='poll',
                        help='webhook=listen for notifications, poll=check print state')
    parser.add_argument('--port', type=int, default=CONFIG['listen_port'],
                        help='Port for webhook listener')
    parser.add_argument('--auto-apply', action='store_true',
                        help='Auto-apply safe suggestions')
    args = parser.parse_args()
    
    CONFIG['auto_apply'] = args.auto_apply
    CONFIG['listen_port'] = args.port
    
    logger.info(f"Starting Adaptive Flow hook (mode={args.mode}, auto_apply={args.auto_apply})")
    
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

1. Create service file:
   sudo nano /etc/systemd/system/adaptive-flow-hook.service

2. Add this content:
   [Unit]
   Description=Adaptive Flow Print Analyzer Hook
   After=moonraker.service
   Requires=moonraker.service

   [Service]
   Type=simple
   User=pi
   WorkingDirectory=/home/pi/Klipper-Adaptive-Flow
   Environment=ADAPTIVE_FLOW_API_KEY=your_key_here
   ExecStart=/usr/bin/python3 /home/pi/Klipper-Adaptive-Flow/moonraker_hook.py --mode poll
   Restart=always
   RestartSec=10

   [Install]
   WantedBy=multi-user.target

3. Enable and start:
   sudo systemctl daemon-reload
   sudo systemctl enable adaptive-flow-hook
   sudo systemctl start adaptive-flow-hook

4. Check status:
   sudo systemctl status adaptive-flow-hook
   journalctl -u adaptive-flow-hook -f
"""
