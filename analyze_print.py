#!/usr/bin/env python3
"""
Adaptive Flow Print Analyzer

Analyzes print session logs and provides tuning suggestions via LLM.
Uses high-quality LLM providers for accurate, reliable analysis.

Usage:
    python3 analyze_print.py                     # Analyze most recent print
    python3 analyze_print.py <summary.json>      # Analyze specific print
    python3 analyze_print.py --auto              # Auto-apply safe suggestions
    python3 analyze_print.py --provider openai   # Use specific provider
    
Configuration:
    Edit analysis_config.cfg with your provider and API key.
    
Supported providers (use --provider flag):
    github      - GitHub Models (FREE, recommended) - requires GITHUB_TOKEN
    openai      - ChatGPT / OpenAI GPT-4o-mini - requires OPENAI_API_KEY  
    anthropic   - Anthropic Claude - requires ANTHROPIC_API_KEY
"""

import os
import sys
import json
import glob
import csv
import urllib.parse
from datetime import datetime

# =============================================================================
# PROVIDER CONFIGURATIONS
# =============================================================================
# Only quality LLM providers that produce reliable, accurate analysis
PROVIDERS = {
    'github': {
        'api_url': 'https://models.inference.ai.azure.com/chat/completions',
        'model': 'gpt-4o-mini',
        'key_env': ['GITHUB_TOKEN', 'GH_TOKEN'],
        'format': 'openai',
    },
    'openai': {
        'api_url': 'https://api.openai.com/v1/chat/completions',
        'model': 'gpt-4o-mini',
        'key_env': ['OPENAI_API_KEY'],
        'format': 'openai',
    },
    'anthropic': {
        'api_url': 'https://api.anthropic.com/v1/messages',
        'model': 'claude-3-haiku-20240307',
        'key_env': ['ANTHROPIC_API_KEY'],
        'format': 'anthropic',
    },
}

# =============================================================================
# CONFIGURATION - Loaded from analysis_config.cfg, env vars, or defaults
# =============================================================================
CONFIG = {
    # API Settings - these can be overridden by --provider flag
    'api_key': os.environ.get('ADAPTIVE_FLOW_API_KEY', ''),
    'api_url': os.environ.get('ADAPTIVE_FLOW_API_URL', 'https://api.openai.com/v1/chat/completions'),
    'model': '',  # Set by provider default or config file
    'format': 'openai',  # openai, anthropic, or gemini-native
    
    # Log directory
    'log_dir': os.path.expanduser('~/printer_data/logs/adaptive_flow'),
    
    # Printer connection (for auto-apply)
    'moonraker_url': 'http://localhost:7125',
    
    # Analysis settings
    'analyze_klippy_log': True,
    'max_csv_rows': 100,
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
                            
                            # Skip empty values
                            if not value:
                                continue
                            
                            # Parse booleans
                            if value.lower() == 'true':
                                value = True
                            elif value.lower() == 'false':
                                value = False
                            elif value.isdigit():
                                value = int(value)
                            
                            # Map config keys to CONFIG dict
                            if key == 'api_key':
                                CONFIG['api_key'] = value
                            elif key == 'model':
                                CONFIG['model'] = value
                            elif key == 'moonraker_url':
                                CONFIG['moonraker_url'] = value
                            elif key == 'log_dir':
                                CONFIG['log_dir'] = os.path.expanduser(value)
                            elif key == 'analyze_klippy_log':
                                CONFIG['analyze_klippy_log'] = value
                            elif key == 'max_csv_rows' and isinstance(value, int):
                                CONFIG['max_csv_rows'] = value

                
                return config_path
            except Exception as e:
                print(f"Warning: Failed to load {config_path}: {e}")
    
    return None


def get_config_provider():
    """Get provider from config file."""
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
                        if line.startswith('provider:'):
                            value = line.split(':', 1)[1].strip()
                            if value and value in PROVIDERS:
                                return value
            except:
                pass
    return None


def save_analysis_results(analysis, summary_file, provider, model):
    """Save analysis results to JSON and human-readable text files."""
    try:
        # Use report_dir if configured, otherwise fall back to log_dir
        report_dir = CONFIG.get('report_dir', CONFIG['log_dir'])
        report_dir = os.path.expanduser(report_dir)
        
        # Create report directory if it doesn't exist
        os.makedirs(report_dir, exist_ok=True)
        
        # Create analysis filename based on the print summary file
        # IMPORTANT: Must create a DIFFERENT filename to avoid overwriting the original summary
        if summary_file:
            base_name = os.path.basename(summary_file)
            # Remove the _summary.json suffix first, then add analysis_ prefix
            if '_summary.json' in base_name:
                base_name = base_name.replace('_summary.json', '')
            else:
                base_name = base_name.replace('.json', '')
            base_name = f"analysis_{base_name}"
        else:
            base_name = f"analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        json_file = os.path.join(report_dir, f"{base_name}.json")
        text_file = os.path.join(report_dir, f"{base_name}.txt")
        
        # Add metadata
        result = {
            'analyzed_at': datetime.now().isoformat(),
            'provider': provider,
            'model': model,
            'source_file': summary_file,
            'analysis': analysis
        }
        
        # Save JSON
        with open(json_file, 'w') as f:
            json.dump(result, f, indent=2)
        
        # Save human-readable text report
        with open(text_file, 'w') as f:
            f.write("=" * 70 + "\n")
            f.write("ADAPTIVE FLOW PRINT ANALYSIS REPORT\n")
            f.write("=" * 70 + "\n\n")
            f.write(f"Analyzed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Provider: {provider} ({model})\n")
            f.write(f"Source: {os.path.basename(summary_file) if summary_file else 'N/A'}\n\n")
            
            f.write("-" * 70 + "\n")
            f.write("ASSESSMENT\n")
            f.write("-" * 70 + "\n")
            f.write(f"{analysis.get('assessment', 'N/A')}\n\n")
            f.write(f"Quality Prediction: {analysis.get('print_quality_prediction', 'N/A').upper()}\n\n")
            
            issues = analysis.get('issues', [])
            if issues:
                f.write("-" * 70 + "\n")
                f.write(f"ISSUES ({len(issues)})\n")
                f.write("-" * 70 + "\n")
                for issue in issues:
                    severity = issue.get('severity', 'unknown').upper()
                    f.write(f"[{severity}] {issue.get('description', '')}\n")
                f.write("\n")
            
            suggestions = analysis.get('suggestions', [])
            if suggestions:
                f.write("-" * 70 + "\n")
                f.write(f"SUGGESTIONS ({len(suggestions)})\n")
                f.write("-" * 70 + "\n")
                for i, sug in enumerate(suggestions, 1):
                    safe = "SAFE TO AUTO-APPLY" if sug.get('safe_to_auto_apply') else "MANUAL REVIEW REQUIRED"
                    f.write(f"\n{i}. {sug.get('parameter', 'unknown')}\n")
                    f.write(f"   Current: {sug.get('current', '?')}\n")
                    f.write(f"   Suggested: {sug.get('suggested', '?')}\n")
                    f.write(f"   Reason: {sug.get('reason', '')}\n")
                    f.write(f"   [{safe}]\n")
                f.write("\n")
            
            klippy_concerns = analysis.get('klippy_concerns', '')
            if klippy_concerns and klippy_concerns.lower() != 'none':
                f.write("-" * 70 + "\n")
                f.write("KLIPPER LOG CONCERNS\n")
                f.write("-" * 70 + "\n")
                f.write(f"{klippy_concerns}\n\n")
            
            notes = analysis.get('notes', '')
            if notes:
                f.write("-" * 70 + "\n")
                f.write("NOTES\n")
                f.write("-" * 70 + "\n")
                f.write(f"{notes}\n\n")
            
            f.write("=" * 70 + "\n")
            f.write("END OF REPORT\n")
            f.write("=" * 70 + "\n")
        
        return text_file, json_file
    except Exception as e:
        print(f"\n⚠️  Failed to save analysis: {e}")
        return None, None


def configure_provider(provider_name):
    """Configure API settings for a specific provider."""
    if provider_name not in PROVIDERS:
        print(f"Unknown provider: {provider_name}")
        print(f"Available: {', '.join(PROVIDERS.keys())}")
        return False
    
    provider = PROVIDERS[provider_name]
    CONFIG['api_url'] = provider['api_url']
    CONFIG['format'] = provider['format']
    
    # Use model from config file if set, otherwise provider default
    if not CONFIG.get('model'):
        CONFIG['model'] = provider['model']
    
    # API key priority: config file > environment variable
    # Config file key is already in CONFIG['api_key'] from load_config_file()
    if not CONFIG.get('api_key'):
        # Fall back to environment variables
        for env_var in provider.get('key_env', []):
            key = os.environ.get(env_var, '')
            if key:
                CONFIG['api_key'] = key
                break
    
    if not CONFIG.get('api_key'):
        print(f"Error: No API key found for {provider_name}")
        print(f"Set 'api_key' in analysis_config.cfg or environment variable")
        return False
    
    return True

# =============================================================================
# ANALYSIS PROMPT - The secret sauce
# =============================================================================
ANALYSIS_PROMPT = """You are an expert 3D printing engineer analyzing thermal control data from a Klipper printer running Adaptive Flow.

## System Overview
Adaptive Flow dynamically adjusts:
- **Temperature**: Boosts hotend temp based on volumetric flow (mm³/s) and speed
- **Pressure Advance**: Reduces PA as temperature increases (lower viscosity = less PA needed)
- **DynZ**: Detects convex surface stress and reduces acceleration
- **Smart Cooling**: Adjusts fan based on flow rate and layer time

## Print Session Summary
```json
{summary_json}
```

## CSV Data Sample (time-series)
Columns: elapsed_s, temp_actual, temp_target, boost, flow, speed, pwm, pa, z_height, predicted_flow, dynz_active, accel, fan_pct
```csv
{csv_sample}
```

## Klipper Log (errors/warnings during print)
```
{klippy_issues}
```

## Analysis Guidelines

IMPORTANT: Only report issues that are actually present in the data. A successful print should have NO issues.

**What's NORMAL (not issues):**
- avg_pwm 0.3-0.7 = heater has headroom ✓
- max_pwm = 1.0 momentarily = NORMAL during temp ramps ✓
- avg_thermal_lag < 3°C = good response ✓
- max_boost < max_boost_limit = within limits ✓
- Low avg_boost (1-5°C) = print had low flow demand, this is FINE ✓
- fan_min < fan_max = Smart Cooling is modulating ✓
- dynz_active_pct 0-5% = minimal stress ✓
- max_speed high but avg_flow low = travel moves, not extrusion ✓

**Potential Issues (only flag if ALL conditions met):**
- Heater saturated: avg_pwm > 0.85 AND avg_thermal_lag > 5°C (not just max_pwm = 1.0)
- Under-extrusion risk: avg_flow > 10 mm³/s AND avg_boost < 5°C AND avg_pwm > 0.8 (all three required)
- Thermal runaway risk: avg_thermal_lag > 8°C sustained
- Fan not modulating: fan_min = fan_max (constant, no variation)
- Excessive DynZ: dynz_active_pct > 30% (too much relief applied)

**FALSE POSITIVES to avoid:**
- max_pwm = 1.0 is NOT overheating (momentary spikes are normal)
- max_speed = 300mm/s is NOT under-extrusion risk (that's travel speed, look at avg_flow instead)
- Low avg_boost is NOT a problem (means the print didn't need much boost)
- High max_speed + low boost is normal for travel moves or low-flow sections

**Do NOT report as issues:**
- Normal PWM values (0.4-0.7 is healthy)
- Normal thermal lag (1-3°C is expected)
- Low/zero boost (means print didn't need it)
- DynZ not activating (means no stress zones detected)

## Output Format
Return ONLY valid JSON (no markdown, no explanation):
{
    "assessment": "1-2 sentence summary. Start with quality prediction.",
    "issues": [
        {"severity": "high|medium|low", "description": "Specific issue with data reference"}
    ],
    "suggestions": [
        {
            "parameter": "exact_variable_name",
            "current": "value_from_data",
            "suggested": "new_value", 
            "reason": "Brief explanation",
            "safe_to_auto_apply": true|false
        }
    ],
    "print_quality_prediction": "excellent|good|fair|poor",
    "klippy_concerns": "Specific concern or 'none'",
    "notes": "Optional additional observations or empty string"
}

**Parameter names** (use exactly):
Temperature: flow_k, speed_boost_k, max_boost_limit, ramp_rate_rise, ramp_rate_fall
DynZ: dynz_enable, dynz_accel_relief, dynz_activate_score, dynz_speed_thresh
Smart Cooling: sc_enable, sc_flow_gate, sc_flow_k, sc_min_fan, sc_max_fan

**safe_to_auto_apply=true** only for: ramp_rate_rise, ramp_rate_fall, sc_flow_k, sc_short_layer_time
**safe_to_auto_apply=false** for: max_boost_limit, dynz_accel_relief, sc_min_fan, sc_max_fan

If the print data looks good, return an empty issues array and "excellent" or "good" prediction."""


def load_summary(summary_path):
    """Load summary JSON file and validate it contains print data."""
    with open(summary_path, 'r') as f:
        data = json.load(f)
    
    # Check if this is an analysis result file (corrupted/overwritten) instead of print data
    if 'analyzed_at' in data or 'analysis' in data:
        print(f"\n⚠️  WARNING: This file appears to be an analysis result, not print data!")
        print(f"   The original print summary may have been overwritten.")
        print(f"   Looking for the original summary data...")
        
        # Try to extract source file reference if available
        if 'source_file' in data and data['source_file']:
            print(f"   Original source: {data['source_file']}")
        
        # Return empty structure with clear indicators
        return {
            'material': 'CORRUPTED_FILE',
            'samples': 0,
            'duration_min': 0,
            'avg_boost': 0,
            'max_boost': 0,
            'avg_pwm': 0,
            'max_pwm': 0,
            '_error': 'This file contains analysis results, not print data. Run a new print to generate fresh data.'
        }
    
    return data


def load_csv_sample(csv_path, max_rows=100):
    """Load last N rows of CSV for detailed analysis."""
    if not os.path.exists(csv_path):
        return "CSV file not found"
    
    try:
        with open(csv_path, 'r') as f:
            reader = csv.reader(f)
            rows = list(reader)
        
        # Get header + last max_rows
        if len(rows) <= max_rows + 1:
            sample_rows = rows
        else:
            sample_rows = [rows[0]] + rows[-(max_rows):]
        
        return '\n'.join([','.join(row) for row in sample_rows])
    except Exception as e:
        return f"Error reading CSV: {e}"


def extract_klippy_issues(start_time_str, duration_min):
    """Extract relevant warnings/errors from klippy.log during the print.
    
    Looks for:
    - Thermal warnings (heater, temp)
    - Timing issues (Timer too close, MCU)
    - Motor/driver issues (stepper, tmc)
    - Print issues (pause, resume, error)
    - Any !! error lines
    """
    import re
    from datetime import datetime, timedelta
    
    klippy_log = os.path.expanduser('~/printer_data/logs/klippy.log')
    if not os.path.exists(klippy_log):
        return "klippy.log not found"
    
    # Parse start time
    try:
        start_dt = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
        # Remove timezone info for comparison with naive timestamps from log
        if start_dt.tzinfo is not None:
            start_dt = start_dt.replace(tzinfo=None)
    except:
        return "Could not parse print start time"
    
    end_dt = start_dt + timedelta(minutes=duration_min + 5)  # Add 5 min buffer
    
    # Keywords to look for (case insensitive)
    issue_patterns = [
        r'!!',  # Error prefix
        r'thermal',
        r'heater',
        r'temp.*error',
        r'temp.*warning',
        r'timer too close',
        r'mcu.*error',
        r'mcu.*timeout',
        r'stepper',
        r'tmc.*error',
        r'driver',
        r'pause',
        r'shutdown',
        r'lost communication',
        r'overdue',
    ]
    pattern = re.compile('|'.join(issue_patterns), re.IGNORECASE)
    
    # Klipper log timestamp format: "Stats 1703350000.123"
    # We need to match this to print time
    issues = []
    
    try:
        # Only read last 2MB of log to stay reasonable
        max_bytes = 2 * 1024 * 1024
        file_size = os.path.getsize(klippy_log)
        
        with open(klippy_log, 'r', errors='ignore') as f:
            if file_size > max_bytes:
                f.seek(file_size - max_bytes)
                f.readline()  # Skip partial line
            
            for line in f:
                # Check if line matches any issue pattern
                if pattern.search(line):
                    # Try to extract timestamp
                    ts_match = re.search(r'Stats (\d+\.\d+)', line)
                    if ts_match:
                        try:
                            log_dt = datetime.fromtimestamp(float(ts_match.group(1)))
                            # Check if within print window
                            if start_dt <= log_dt <= end_dt:
                                # Clean up the line
                                clean_line = line.strip()[:200]  # Limit length
                                if clean_line not in issues:  # Dedupe
                                    issues.append(clean_line)
                        except:
                            pass
                    elif '!!' in line:
                        # Always include error lines
                        clean_line = line.strip()[:200]
                        if clean_line not in issues:
                            issues.append(clean_line)
        
        # Limit to most relevant issues
        if len(issues) > 30:
            issues = issues[:30] + [f"... and {len(issues) - 30} more issues"]
        
        if not issues:
            return "No issues found in klippy.log during print"
        
        return '\n'.join(issues)
        
    except Exception as e:
        return f"Error reading klippy.log: {e}"


def find_latest_summary():
    """Find the most recent summary JSON file."""
    pattern = os.path.join(CONFIG['log_dir'], '*_summary.json')
    files = glob.glob(pattern)
    
    if not files:
        return None
    
    # Sort by modification time, newest first
    files.sort(key=os.path.getmtime, reverse=True)
    return files[0]


def call_llm_api(prompt, summary_json, csv_sample, klippy_issues=""):
    """Call the LLM API with the analysis prompt."""
    import urllib.request
    import ssl
    
    if not CONFIG['api_key']:
        print("ERROR: No API key configured.")
        print("Edit analysis_config.cfg or use --provider flag.")
        print("\nAvailable providers:")
        for name, info in PROVIDERS.items():
            key_vars = ', '.join(info.get('key_env', ['none']))
            print(f"  --provider {name:12} (keys: {key_vars})")
        print("\nRecommended: --provider github (free with any GitHub account)")
        return None
    
    # Build the full prompt
    full_prompt = ANALYSIS_PROMPT.replace('{summary_json}', json.dumps(summary_json, indent=2))
    full_prompt = full_prompt.replace('{csv_sample}', csv_sample)
    full_prompt = full_prompt.replace('{klippy_issues}', klippy_issues or "No issues extracted")
    
    # Prepare API request
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {CONFIG["api_key"]}'
    }
    
    payload = {
        'model': CONFIG['model'],
        'messages': [
            {'role': 'system', 'content': 'You are a 3D printing thermal control expert. Always respond with valid JSON.'},
            {'role': 'user', 'content': full_prompt}
        ],
        'temperature': 0.3,  # Low temp for consistent, focused analysis
        'max_tokens': 2000
    }
    
    try:
        # Handle Anthropic's different API format
        if CONFIG['format'] == 'anthropic' or 'anthropic.com' in CONFIG['api_url']:
            headers['x-api-key'] = CONFIG['api_key']
            headers['anthropic-version'] = '2023-06-01'
            if 'Authorization' in headers:
                del headers['Authorization']
            payload = {
                'model': CONFIG['model'],
                'max_tokens': 2000,
                'messages': payload['messages']
            }
        
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(CONFIG['api_url'], data=data, headers=headers)
        
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=60, context=ctx) as response:
            result = json.loads(response.read().decode('utf-8'))
        
        # Extract content based on API format
        if 'choices' in result:
            # OpenAI format (GitHub, OpenAI)
            content = result['choices'][0]['message']['content']
        elif 'content' in result:
            # Anthropic format
            content = result['content'][0]['text']
        else:
            content = str(result)
        
        return content
        
    except Exception as e:
        print(f"API Error: {e}")
        return None


def parse_llm_response(response_text):
    """Extract JSON from LLM response."""
    # Try to find JSON block in response
    try:
        # Look for ```json ... ``` block
        if '```json' in response_text:
            start = response_text.index('```json') + 7
            end = response_text.index('```', start)
            json_str = response_text[start:end].strip()
        elif '```' in response_text:
            start = response_text.index('```') + 3
            end = response_text.index('```', start)
            json_str = response_text[start:end].strip()
        else:
            # Try to parse the whole thing
            json_str = response_text.strip()
        
        return json.loads(json_str)
    except Exception as e:
        print(f"Warning: Could not parse JSON response: {e}")
        print("Raw response:")
        print(response_text)
        return None


def apply_suggestion(suggestion, moonraker_url):
    """Apply a suggestion via Moonraker API."""
    import urllib.request
    
    param = suggestion['parameter']
    value = suggestion['suggested']
    
    # Map parameter names to Klipper commands
    param_commands = {
        'speed_boost_k': f'SET_GCODE_VARIABLE MACRO=_AUTO_TEMP_CORE VARIABLE=speed_boost_k VALUE={value}',
        'speed_boost_threshold': f'SET_GCODE_VARIABLE MACRO=_AUTO_TEMP_CORE VARIABLE=speed_boost_threshold VALUE={value}',
        'ramp_rate_fall': f'SAVE_VARIABLE VARIABLE=material_ramp_fall VALUE={value}',
        'ramp_rate_rise': f'SAVE_VARIABLE VARIABLE=material_ramp_rise VALUE={value}',
        'max_boost_limit': f'SET_GCODE_VARIABLE MACRO=_AUTO_TEMP_CORE VARIABLE=max_boost_limit VALUE={value}',
        'flow_smoothing': f'SET_GCODE_VARIABLE MACRO=_AUTO_TEMP_CORE VARIABLE=flow_smoothing VALUE={value}',
    }
    
    if param not in param_commands:
        print(f"  Unknown parameter: {param}")
        return False
    
    gcode = param_commands[param]
    
    try:
        url = f"{moonraker_url}/printer/gcode/script?script={urllib.parse.quote(gcode)}"
        req = urllib.request.Request(url, method='POST')
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status == 200:
                print(f"  ✓ Applied: {gcode}")
                return True
    except Exception as e:
        print(f"  ✗ Failed to apply {param}: {e}")
    
    return False


def main():
    import argparse
    
    # Load config file first (sets defaults)
    config_file = load_config_file()
    config_provider = get_config_provider()
    
    parser = argparse.ArgumentParser(
        description='Analyze Adaptive Flow print sessions using LLM',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Providers:
  github      GitHub Models (FREE!) - set GITHUB_TOKEN
  openai      ChatGPT / GPT-4o-mini - set OPENAI_API_KEY
  anthropic   Claude - set ANTHROPIC_API_KEY  

Configuration:
  Edit analysis_config.cfg to set provider and API key.

Examples:
  python3 analyze_print.py                     # Uses config file
  python3 analyze_print.py --provider github   # Use GitHub Models
  python3 analyze_print.py --auto              # Auto-apply safe suggestions
        """
    )
    parser.add_argument('summary_file', nargs='?', help='Path to summary JSON (default: most recent)')
    parser.add_argument('--auto', action='store_true', help='Auto-apply safe suggestions')
    parser.add_argument('--raw', action='store_true', help='Show raw LLM response')
    parser.add_argument('--verbose', '-v', action='store_true', help='Show full analysis in console (default: brief summary)')
    parser.add_argument('--provider', '-p', choices=list(PROVIDERS.keys()),
                        help='LLM provider to use (overrides config file)')
    parser.add_argument('--model', '-m', help='Override model name')
    parser.add_argument('--list-providers', action='store_true', help='Show available providers')
    args = parser.parse_args()
    
    # List providers if requested
    if args.list_providers:
        print("Available LLM providers:\n")
        for name, info in PROVIDERS.items():
            key_vars = ', '.join(info.get('key_env', ['none needed']))
            has_key = any(os.environ.get(k) for k in info.get('key_env', []))
            status = "✓ configured" if has_key else "✗ no key"
            print(f"  {name:12} model: {info['model']:30} [{status}]")
            print(f"               keys: {key_vars}")
        if config_file:
            print(f"\nConfig file: {config_file}")
            if config_provider:
                print(f"Configured provider: {config_provider}")
        return 0
    
    # Configure provider: command line > config file > environment
    provider = args.provider or config_provider
    if provider:
        if not configure_provider(provider):
            return 1
        print(f"Using provider: {provider} (model: {CONFIG['model']})")
        if config_file and not args.provider:
            print(f"  (from {os.path.basename(config_file)})")
    
    # Override model if specified
    if args.model:
        CONFIG['model'] = args.model
    
    # Find summary file
    if args.summary_file:
        summary_path = args.summary_file
    else:
        summary_path = find_latest_summary()
        if not summary_path:
            print(f"No print logs found in {CONFIG['log_dir']}")
            print("Run a print first to generate logs.")
            return 1
    
    print(f"Analyzing: {summary_path}")
    print("-" * 60)
    
    # Load data
    summary = load_summary(summary_path)
    
    # Check for corrupted/overwritten file
    if summary.get('_error'):
        print(f"\n❌ ERROR: {summary['_error']}")
        print("\nThis can happen if a previous version of the script overwrote the summary file.")
        print("To fix: Run a new print to generate fresh logging data.")
        return 1
    
    # Validate we have actual data
    if summary.get('samples', 0) == 0:
        print(f"\n⚠️  WARNING: Summary file contains 0 samples!")
        print("   This print may have been too short, or logging wasn't active.")
        print("   Make sure AT_START is called at print start and AT_END at print end.")
    
    csv_path = summary_path.replace('_summary.json', '.csv')
    csv_sample = load_csv_sample(csv_path, CONFIG.get('max_csv_rows', 100))
    
    # Extract klippy.log issues for this print
    start_time = summary.get('start_time', '')
    duration_min = summary.get('duration_min', 60)
    klippy_issues = extract_klippy_issues(start_time, duration_min)
    
    # Print quick stats
    print(f"Material: {summary.get('material', 'Unknown')}")
    print(f"Duration: {summary.get('duration_min', 0)} minutes")
    print(f"Samples: {summary.get('samples', 0)}")
    print(f"Avg Boost: {summary.get('avg_boost', 0)}°C, Max: {summary.get('max_boost', 0)}°C")
    print(f"Avg PWM: {summary.get('avg_pwm', 0):.1%}, Max: {summary.get('max_pwm', 0):.1%}")
    
    # DynZ stats
    dynz_pct = summary.get('dynz_active_pct', 0)
    accel_min = summary.get('accel_min', 0)
    if dynz_pct > 0:
        print(f"DynZ Active: {dynz_pct}% of print, Min Accel: {accel_min} mm/s²")
    else:
        print(f"DynZ Active: 0% (no stress zones detected)")
    
    # Show klippy issues if any
    if "No issues" not in klippy_issues and "not found" not in klippy_issues:
        issue_count = klippy_issues.count('\n') + 1
        print(f"Klippy Issues: {issue_count} relevant log entries found")
    else:
        print(f"Klippy Issues: None")
    
    print("-" * 60)
    print("Sending to LLM for analysis...")
    
    # Call LLM
    response = call_llm_api(ANALYSIS_PROMPT, summary, csv_sample, klippy_issues)
    
    if not response:
        return 1
    
    if args.raw:
        print("\nRaw LLM Response:")
        print(response)
        return 0
    
    # Parse response
    analysis = parse_llm_response(response)
    
    if not analysis:
        return 1
    
    # Save analysis results
    provider_name = args.provider or config_provider or 'custom'
    model_name = CONFIG.get('model', 'unknown')
    text_file, json_file = save_analysis_results(analysis, summary_path, provider_name, model_name)
    
    # Count issues and suggestions for summary
    issues = analysis.get('issues', [])
    suggestions = analysis.get('suggestions', [])
    safe_suggestions = [s for s in suggestions if s.get('safe_to_auto_apply')]
    high_issues = [i for i in issues if i.get('severity', '').lower() == 'high']
    
    if args.verbose:
        # Full console output (original behavior)
        print("\n" + "=" * 60)
        print("ANALYSIS RESULTS")
        print("=" * 60)
        
        print(f"\n📊 Assessment: {analysis.get('assessment', 'N/A')}")
        print(f"🎯 Quality Prediction: {analysis.get('print_quality_prediction', 'N/A').upper()}")
        
        if issues:
            print(f"\n⚠️  Issues Found ({len(issues)}):")
            for issue in issues:
                severity = issue.get('severity', 'unknown').upper()
                icon = '🔴' if severity == 'HIGH' else '🟡' if severity == 'MEDIUM' else '🟢'
                print(f"  {icon} [{severity}] {issue.get('description', '')}")
        
        klippy_concerns = analysis.get('klippy_concerns', '')
        if klippy_concerns and klippy_concerns.lower() != 'none':
            print(f"\n🔧 Klipper Log Concerns: {klippy_concerns}")
        
        if suggestions:
            print(f"\n💡 Suggestions ({len(suggestions)}):")
            for i, sug in enumerate(suggestions, 1):
                safe = "✓ SAFE" if sug.get('safe_to_auto_apply') else "⚠ MANUAL"
                print(f"\n  {i}. {sug.get('parameter', 'unknown')}")
                print(f"     Current: {sug.get('current', '?')} → Suggested: {sug.get('suggested', '?')}")
                print(f"     Reason: {sug.get('reason', '')}")
                print(f"     [{safe}]")
        
        notes = analysis.get('notes', '')
        if notes:
            print(f"\n📝 Notes: {notes}")
    else:
        # Brief summary (default)
        print("\n" + "=" * 60)
        print("ANALYSIS COMPLETE")
        print("=" * 60)
        
        # One-line assessment
        quality = analysis.get('print_quality_prediction', 'unknown').upper()
        quality_icon = '✅' if quality in ['EXCELLENT', 'GOOD'] else '⚠️' if quality == 'FAIR' else '❌'
        print(f"\n{quality_icon} Quality: {quality}")
        
        # Brief assessment (truncated if needed)
        assessment = analysis.get('assessment', 'N/A')
        if len(assessment) > 100:
            assessment = assessment[:97] + "..."
        print(f"   {assessment}")
        
        # Summary counts
        print(f"\n📋 Summary:")
        if high_issues:
            print(f"   🔴 {len(high_issues)} critical issue(s)")
        if len(issues) > len(high_issues):
            print(f"   🟡 {len(issues) - len(high_issues)} other issue(s)")
        if not issues:
            print(f"   ✅ No issues detected")
        
        print(f"   💡 {len(suggestions)} suggestion(s) ({len(safe_suggestions)} safe to auto-apply)")
        
        # File location with Mainsail hint
        print(f"\n📄 Full report saved:")
        print(f"   {text_file}")
        
        # Show Mainsail path if in config folder
        if 'printer_data/config' in text_file:
            mainsail_path = text_file.split('printer_data/config/')[-1]
            print(f"\n🌐 View in Mainsail: Machine → {mainsail_path.replace('/', ' → ')}")
    
    # Auto-apply if requested
    if args.auto and suggestions:
        print("\n" + "-" * 60)
        print("AUTO-APPLYING SAFE SUGGESTIONS...")
        
        for sug in suggestions:
            if sug.get('safe_to_auto_apply'):
                apply_suggestion(sug, CONFIG['moonraker_url'])
        
        print("\nNote: Changes are temporary until Klipper restart.")
        print("To make permanent, edit auto_flow.cfg")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
