# Print Analysis (LLM-Powered)

Analyze your print sessions using AI to get tuning suggestions for Adaptive Flow parameters.

## Quick Start

```bash
cd ~/Klipper-Adaptive-Flow
python3 analyze_print.py --provider github   # Uses GitHub Models (free)
```

---

## Supported LLM Providers

| Provider | Cost | API Key Environment Variable |
|----------|------|------------------------------|
| **GitHub Models** | Free | `GITHUB_TOKEN` |
| **Ollama** | Free (local) | None required |
| **OpenAI** | Paid | `OPENAI_API_KEY` |
| **Anthropic** | Paid | `ANTHROPIC_API_KEY` |
| **Google Gemini** | Free tier | `GOOGLE_API_KEY` or `GEMINI_API_KEY` |
| **OpenRouter** | Pay-per-use | `OPENROUTER_API_KEY` |

### Setting API Keys

```bash
# Add to ~/.bashrc or ~/.zshrc
export GITHUB_TOKEN="ghp_your_token_here"
export OPENAI_API_KEY="sk-your-key-here"
```

Or set before running:
```bash
GITHUB_TOKEN=ghp_xxx python3 analyze_print.py --provider github
```

---

## Usage

### Analyze Most Recent Print

```bash
python3 analyze_print.py
python3 analyze_print.py --provider github
python3 analyze_print.py --provider ollama   # Local, no API key
```

### Analyze Specific Print

```bash
python3 analyze_print.py ~/printer_data/logs/adaptive_flow/20231215_143022_benchy_summary.json
```

### Auto-Apply Safe Suggestions

```bash
python3 analyze_print.py --auto
```

Only applies changes marked `safe_to_auto_apply: true` by the LLM.

---

## What Gets Analyzed

The system analyzes:

1. **Print Session Summary** (`*_summary.json`)
   - Duration, material, temperatures
   - Average/max boost, PWM, flow
   - Thermal lag measurements

2. **Detailed CSV Data** (`*.csv`)
   - Per-second: temp, boost, flow, speed, PWM, PA, Z
   - Last 100 rows sampled for LLM context

3. **Klipper Log** (`klippy.log`)
   - Thermal warnings
   - Timer/MCU errors
   - Driver issues
   - Any `!!` error lines

---

## Example Output

```
╔═══════════════════════════════════════════════════════╗
║          ADAPTIVE FLOW PRINT ANALYSIS                 ║
╠═══════════════════════════════════════════════════════╣
║ Provider: GitHub Models (gpt-4o-mini)                 ║
║ Print: benchy.gcode                                   ║
║ Duration: 45.2 min                                    ║
╠═══════════════════════════════════════════════════════╣

📊 ASSESSMENT
Good thermal response. Heater kept up well with avg 68% PWM.
Minor improvements possible for high-speed sections.

⚠️ ISSUES FOUND
[medium] Speed boost may be insufficient at 250mm/s moves
[low] Slight thermal lag on rapid flow transitions

💡 SUGGESTIONS
┌─────────────────┬─────────┬───────────┬──────────────────────────┐
│ Parameter       │ Current │ Suggested │ Reason                   │
├─────────────────┼─────────┼───────────┼──────────────────────────┤
│ speed_boost_k   │ 0.08    │ 0.10      │ Better heat at 250mm/s   │
│ ramp_rate_rise  │ 4.0     │ 5.0       │ Faster response to flow  │
└─────────────────┴─────────┴───────────┴──────────────────────────┘

🎯 QUALITY PREDICTION: good

💾 Analysis saved to: ~/printer_data/logs/adaptive_flow/analysis_20231215_143022_benchy.json
```

---

## Auto-Analysis After Prints

Use `moonraker_hook.py` to automatically analyze every print.

### Option 1: Polling Mode (Simplest)

```bash
python3 moonraker_hook.py --provider github
```

Runs in foreground, polls Moonraker for print completion.

### Option 2: Systemd Service (Recommended)

Create `/etc/systemd/system/adaptive-flow-hook.service`:

```ini
[Unit]
Description=Adaptive Flow Print Analyzer Hook
After=moonraker.service
Requires=moonraker.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/Klipper-Adaptive-Flow
Environment=GITHUB_TOKEN=ghp_your_token_here
ExecStart=/usr/bin/python3 /home/pi/Klipper-Adaptive-Flow/moonraker_hook.py --provider github
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable adaptive-flow-hook
sudo systemctl start adaptive-flow-hook
```

Check status:
```bash
sudo systemctl status adaptive-flow-hook
journalctl -u adaptive-flow-hook -f
```

### Option 3: Webhook Mode

If you prefer webhooks over polling, add to `moonraker.conf`:

```ini
[notifier print_complete]
url: json://localhost:7126/server/custom/adaptive_flow_analyze
events: complete
body: {"filename": "{event_args[0].filename}", "status": "{event_args[0].state}"}
```

Then run:
```bash
python3 moonraker_hook.py --mode webhook --port 7126
```

---

## Moonraker Hook Options

```bash
python3 moonraker_hook.py --help
```

| Flag | Description |
|------|-------------|
| `--provider X` | LLM provider (github, ollama, openai, etc.) |
| `--mode poll` | Poll Moonraker for print state (default) |
| `--mode webhook` | Listen for Moonraker notifications |
| `--port N` | Port for webhook listener (default: 7126) |
| `--auto-apply` | Auto-apply safe suggestions |

---

## Analysis Results

Results are saved as JSON alongside print logs:

```
~/printer_data/logs/adaptive_flow/
├── 20231215_143022_benchy.csv           # Raw data
├── 20231215_143022_benchy_summary.json  # Print summary
└── analysis_20231215_143022_benchy.json # LLM analysis
```

### Analysis JSON Format

```json
{
  "analyzed_at": "2023-12-15T15:30:22",
  "provider": "github",
  "model": "gpt-4o-mini",
  "source_file": "20231215_143022_benchy_summary.json",
  "analysis": {
    "assessment": "Good thermal response overall...",
    "issues": [
      {"severity": "medium", "description": "Speed boost insufficient at 250mm/s"}
    ],
    "suggestions": [
      {
        "parameter": "speed_boost_k",
        "current": "0.08",
        "suggested": "0.10",
        "reason": "Better heating for high-speed moves",
        "safe_to_auto_apply": true
      }
    ],
    "print_quality_prediction": "good",
    "klippy_concerns": "none",
    "notes": "Consider increasing ramp rate for faster flow transitions"
  }
}
```

---

## Tunable Parameters

The LLM can suggest changes to these `auto_flow.cfg` variables:

| Parameter | Description | Default |
|-----------|-------------|---------|
| `speed_boost_k` | °C per mm/s above 100mm/s | 0.08 |
| `speed_boost_threshold` | Speed to trigger boost | 100.0 |
| `ramp_rate_rise` | Heat-up rate (°C/s) | 4.0 |
| `ramp_rate_fall` | Cool-down rate (°C/s) | 1.0 |
| `max_boost_limit` | Maximum temp boost | 50.0 |
| `flow_smoothing` | Flow EMA factor | 0.15 |

Material-specific parameters in `material_profiles.cfg`:

| Parameter | Description |
|-----------|-------------|
| `flow_k` | °C per mm³/s of flow |
| `max_boost` | Per-material boost cap |
| `pa_boost_k` | PA reduction per °C |
| `ramp_rise` / `ramp_fall` | Material ramp rates |

---

## Troubleshooting

### No logs found
Check that logging is working:
```bash
ls -la ~/printer_data/logs/adaptive_flow/
```

If empty, ensure `AT_LOG_START` is being called (happens automatically in `AT_START`).

### API key errors
```bash
# Test your key
echo $GITHUB_TOKEN
python3 -c "import os; print(os.environ.get('GITHUB_TOKEN', 'NOT SET'))"
```

### Ollama not responding
```bash
# Check Ollama is running
curl http://localhost:11434/api/tags

# Pull a model
ollama pull llama3.1
```

### Hook not detecting print completion
```bash
# Test Moonraker connection
curl http://localhost:7125/printer/objects/query?print_stats
```
