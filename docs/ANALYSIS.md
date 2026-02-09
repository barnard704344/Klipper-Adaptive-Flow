# Print Analysis

The `analyze_print.py` tool provides two analysis modes:

1. **LLM Mode** - AI-powered tuning suggestions for single prints
2. **Banding Mode** - Multi-print aggregation to identify banding culprits

**Both are optional.** The core Adaptive Flow system works fine without analysis.

---

## Mode 1: LLM Analysis (Single Print)

Analyzes one print using an LLM (GitHub Models, OpenAI, or Anthropic) which reviews:

- Temperature stability (did heater keep up?)
- Heater duty cycle (was it working too hard?)
- Flow rates and speed
- Any errors in Klipper log during the print

It returns:
- **Issues**: Problems detected in the data (heater saturation, thermal lag, etc.)
- **Suggestions**: Specific parameter changes to try
- **Safety classification**: Each suggestion marked `[✓ SAFE]` or `[⚠ MANUAL]`

**Safe suggestions** are conservative changes (ramp rates, minor adjustments) that can be auto-applied.
**Manual suggestions** are significant changes (boost limits, PA values) that need your review.

## Quick Setup

### 1. Get an API Key

**Option A: GitHub Models (Free, Recommended)**

1. Go to https://github.com/settings/tokens
2. Click **Generate new token (classic)**
3. Name: "Adaptive Flow"
4. **Don't check any permission boxes** (no scopes needed)
5. Generate and copy the token (starts with `ghp_`)

**Option B: OpenAI (Paid)** - https://platform.openai.com/api-keys  
**Option C: Anthropic (Paid)** - https://console.anthropic.com/

### 2. Configure

Edit `analysis_config.cfg` in your Klipper-Adaptive-Flow folder:

```ini
[analysis]
provider: github
api_key: ghp_paste_your_token_here
```

### 3. Run Analysis

After a print completes:

```bash
cd ~/Klipper-Adaptive-Flow
python3 analyze_print.py
```

It automatically finds the most recent print log and analyzes it.

## Understanding the Output

**Brief mode (default)**:

```
✅ PRINT ANALYSIS: ALL GOOD!
==================================================

Quality: EXCELLENT
No issues detected. Nice print! 🎉

📄 Report: /home/pi/printer_data/config/adaptive_flow/reports/...
```

or if issues found:

```
⚠️ PRINT ANALYSIS: FAIR
==================================================

Print quality likely affected by heater saturation.

🔴 1 critical issue(s):
   • Heater saturated: avg PWM 87%, thermal lag 6.2°C

💡 2 suggestion(s) (1 safe to auto-apply)

📄 Full details: /home/pi/printer_data/...
```

**Verbose mode** (show full details):

```bash
python3 analyze_print.py --verbose
```

Shows the complete analysis with all issues, suggestions, and reasoning.

## Suggestion Types

### [✓ SAFE] - Auto-Apply Allowed

These are conservative adjustments:
- `ramp_rate_rise` / `ramp_rate_fall` - Temperature ramp speeds
- `sc_flow_k` - Smart Cooling flow sensitivity
- Minor value tweaks (<20% change)

**Safe to auto-apply** means the change won't cause print failures. Worst case: slightly different thermal behavior.

### [⚠ MANUAL] - Review Required

These need your judgment:
- `max_boost_limit` - Maximum temperature boost cap
- `dynz_accel_relief` - DynZ acceleration limit
- `sc_min_fan` / `sc_max_fan` - Fan speed limits
- Major value changes (>20%)

**Manual review** means the change could affect print quality or cause issues if incorrect for your setup.

## Auto-Apply Safe Suggestions

To automatically apply safe suggestions:

```bash
python3 analyze_print.py --auto
```

This sends `SET_GCODE_VARIABLE` commands to Klipper via Moonraker. Changes are **temporary** (lost on restart).

To make changes permanent, manually edit `auto_flow_user.cfg` with the suggested values.

## Common Suggestions

| Parameter | What It Does | Why LLM Suggests Increase | Why LLM Suggests Decrease |
|-----------|--------------|---------------------------|---------------------------|
| `flow_k` | Temp boost per mm³/s flow | Heater can't keep up at high flow | Overshooting temp during infill |
| `speed_boost_k` | Temp boost per mm/s speed | Under-extrusion on fast perimeters | Too much heat on thin walls |
| `ramp_rate_rise` | How fast temp increases | Slow response to flow spikes | Overshooting target temp |
| `ramp_rate_fall` | How fast temp decreases | Overshooting on slowdowns | Under-temp after flow drops |
| `max_boost_limit` | Maximum extra temperature | Need more headroom for high flow | Hitting safety limits unnecessarily |

The LLM provides reasoning for each suggestion based on your actual print data.

## Advanced Usage

### Analyze Specific Print

```bash
python3 analyze_print.py /path/to/print_summary.json
```

### Use Different Provider

```bash
python3 analyze_print.py --provider openai
python3 analyze_print.py --provider anthropic
```

### Show Raw LLM Response

```bash
python3 analyze_print.py --raw
```

Useful for debugging or seeing the complete JSON response.

## Auto-Analysis Service (Optional)

Want analysis to run automatically after every print?

### Setup

1. **Configure API key** in `analysis_config.cfg` (see above)

2. **Create systemd service**:

```bash
sudo nano /etc/systemd/system/adaptive-flow-hook.service
```

Paste this (change `pi` to your username if different):

```ini
[Unit]
Description=Adaptive Flow Print Analyzer
After=moonraker.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/Klipper-Adaptive-Flow
ExecStart=/usr/bin/python3 /home/pi/Klipper-Adaptive-Flow/moonraker_hook.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Save with Ctrl+O, Enter, Ctrl+X.

### Step 3: Start It

```bash
sudo systemctl daemon-reload
sudo systemctl enable adaptive-flow-hook
sudo systemctl start adaptive-flow-hook
```

Now every print will be analyzed automatically!

### Check If It's Running

```bash
sudo systemctl status adaptive-flow-hook
```

---

## What Does the AI Analyze?

The AI looks at data from your print:

- **Temperature**: Did the heater keep up? Was it working too hard?
- **Flow Rate**: How much plastic was being pushed through
- **Print Speed**: How fast the toolhead was moving
- **Any Errors**: Problems in the Klipper log

Based on this, it suggests changes to make your prints better.

---

## Understanding the Suggestions

The AI might suggest changing values in your config. Here's what they mean:

| Setting | What It Does | When to Increase |
|---------|--------------|------------------|
| `flow_k` | Adds heat when pushing more plastic | Heater can't keep up during infill |
| `speed_boost_k` | Adds heat at high speeds | Underextrusion on fast moves |
| `ramp_rate_rise` | How fast temp goes up | Slow response to speed changes |
| `max_boost` | Maximum extra temperature | Need more heat headroom |

**Don't worry about memorizing these** - the AI explains why it's suggesting each change.

---

## Configuration Options

All settings are in `analysis_config.cfg`:

```ini
[analysis]
# Which AI service to use: github (FREE), openai, anthropic
provider: github

# Your API key
api_key: ghp_your_token

# Model to use (optional - uses provider default if blank)
model: 

# Automatically apply safe suggestions
auto_apply: false

# Show results in Klipper console
notify_console: true
```

---

## Mode 2: Banding Analysis (Multi-Print)

Aggregates data across multiple prints to identify banding culprits through pattern detection.

### Usage

```bash
# Analyze last 10 prints for banding patterns
python3 analyze_print.py --count 10

# Filter by material
python3 analyze_print.py --count 10 --material PLA

# Analyze last 20 prints
python3 analyze_print.py --count 20
```

### What It Detects

The logging system tracks state transitions that cause banding:

| Event Type | What It Detects |
|------------|-----------------|
| **Accel changes** | Mid-layer acceleration switching (banding) |
| **PA changes** | PA oscillation causing ribbing |
| **DynZ transitions** | DynZ activation causing accel changes |
| **Temp overshoots** | Temperature instability |

Each print is diagnosed with a likely culprit. Multi-print analysis confirms patterns.

### Example Output

```
======================================================================
  BANDING ANALYSIS (10 prints)
======================================================================

Total printing time: 187.3 minutes
Materials: {'PLA': 10}

──────────────────────────────────────────────────────────────────────
  BANDING RISK OVERVIEW
──────────────────────────────────────────────────────────────────────
High-risk events: 423 (avg 42.3/print)
Accel changes: 387 (avg 38.7/print)
PA changes: 108 (avg 10.8/print)
DynZ transitions: 241 (avg 24.1/print)

──────────────────────────────────────────────────────────────────────
  DIAGNOSIS
──────────────────────────────────────────────────────────────────────
Most common culprit: dynz_accel_switching
Breakdown:
  - dynz_accel_switching: 9 prints
  - pa_oscillation: 1 print

──────────────────────────────────────────────────────────────────────
  RECOMMENDED FIX
──────────────────────────────────────────────────────────────────────
⚠️  DynZ changing acceleration causes banding

FIX: Set variable_dynz_relief_method: 'temp_reduction'
```

### Banding Culprits

| Culprit | Cause | Fix |
|---------|-------|-----|
| `dynz_accel_switching` | DynZ changing acceleration | `dynz_relief_method: 'temp_reduction'` |
| `pa_oscillation` | PA changing too much | Lower `pa_boost_k` |
| `temp_instability` | Temperature oscillating | Lower ramp rates, check PID |
| `slicer_accel_control` | Slicer inserting accel commands | Disable firmware accel in slicer |
| `no_obvious_culprit` | Low event counts | Check mechanical (Z-wobble, filament) |

### CSV Logging Reference

Enhanced logging tracks these columns for banding analysis:

| Column | Description |
|--------|-------------|
| `pa_delta` | PA change from last sample |
| `accel_delta` | Acceleration change |
| `temp_target_delta` | Target temp change |
| `temp_overshoot` | Actual - Target temp |
| `dynz_transition` | DynZ state change (1=ON, -1=OFF) |
| `layer_transition` | Layer change detected |
| `banding_risk` | Risk score 0-10 |
| `event_flags` | Human-readable events (e.g., "ACCEL_CHG:+1200") |

**Banding Risk Score (0-10):**
- +3: Accel change >500 mm/s²
- +2: PA change >0.005
- +2: Temp change >3°C
- +2: DynZ state transition
- +1: Temp overshoot >5°C

Score ≥5 = high risk event (likely visible artifact)

### Debugging Workflow

1. **Print 5-10 test cubes** with logging enabled (already happens automatically)
2. **Run banding analysis:**
   ```bash
   python3 analyze_print.py --count 10
   ```
3. **Check consistency**: If 8+ prints show same culprit → confirmed diagnosis
4. **Apply fix** from recommendations
5. **Verify**: Print one more cube, check if high-risk events drop to near zero

---

## Troubleshooting

### "No logs found"

The analysis needs print data. Make sure you've completed at least one print with Adaptive Flow enabled.

Check if logs exist:
```bash
ls ~/printer_data/logs/adaptive_flow/
```

### "API key error" or "Unauthorized"

Your API key might be wrong. Double-check:
1. No extra spaces in `analysis_config.cfg`
2. Token hasn't expired (GitHub tokens can expire)
3. Token was copied completely

### Need More Help?

Open an issue on GitHub with:
1. The error message you see
2. Which provider you're using
3. Your `analysis_config.cfg` (remove your API key first!)
