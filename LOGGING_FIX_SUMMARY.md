# Logging Fix Summary

## Issue Fixed
CSV log files were only showing the header row (1KB file) with no data rows, making print analysis impossible.

## Root Cause
**The `auto_flow_user.cfg` file contained an empty `gcode:` line that silently replaced the entire core logic.**

When Klipper's configparser processes duplicate `[gcode_macro _AUTO_TEMP_CORE]` sections from multiple included files, it **merges** them. Variable overrides from the user file correctly override defaults — but the empty `gcode:` line **replaced** the 700-line `_AUTO_TEMP_CORE` logic with nothing.

**Result:** `AUTO_TEMP_LOOP` fired every second, called `_AUTO_TEMP_CORE`, which did absolutely nothing — no temperature adjustments, no `AT_LOG_DATA` calls, no CSV data.

### Secondary Issue (also fixed)
The flush condition `_log_sample_count % 10 == 0` in `extruder_monitor.py` never flushed samples 1-9, so even if data was being written, short prints would produce empty files.

## The Fix

### Part 1: Remove empty gcode: from user config (Critical)
Changed [auto_flow_user.cfg.example](auto_flow_user.cfg.example):

```cfg
# BEFORE (broken) — empty gcode: replaces the entire core loop!
[gcode_macro _AUTO_TEMP_CORE]
variable_use_high_flow_nozzle: True
gcode:

# AFTER (fixed) — only variable overrides, no gcode: line
[gcode_macro _AUTO_TEMP_CORE]
variable_use_high_flow_nozzle: True
# IMPORTANT: Do NOT add a "gcode:" line here!
```

The `update.sh` script now auto-detects and removes this empty `gcode:` line from existing user configs.

### Part 2: Flush Logic Fix
Changed flush condition in [extruder_monitor.py](extruder_monitor.py#L668):

```python
# BEFORE (broken)
if self._log_sample_count % 10 == 0:

# AFTER (fixed) — flush first sample immediately
if self._log_sample_count == 1 or self._log_sample_count % 10 == 0:
```

### Part 3: Diagnostic Improvements  
- **AT_LOG_STATUS command** — check logging health at runtime
- **First data point confirmation** — console message confirms data flow

## How To Apply

### Quick fix (on the printer):
```bash
cd ~/Klipper-Adaptive-Flow
git pull
bash update.sh
```

The `update.sh` script will automatically:
1. Remove the empty `gcode:` from your `auto_flow_user.cfg`
2. Update `extruder_monitor.py` with the flush fix
3. Restart Klipper

### Manual fix (if not using update.sh):
Remove the `gcode:` line from `~/printer_data/config/auto_flow_user.cfg`:
```bash
sed -i '/^gcode:[[:space:]]*$/d' ~/printer_data/config/auto_flow_user.cfg
sudo systemctl restart klipper
```
