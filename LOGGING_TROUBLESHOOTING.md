# Logging Troubleshooting Guide

## Symptom: CSV file only has header row, no data

If your CSV log file only contains:
```
elapsed_s,temp_actual,temp_target,boost,flow,speed,pwm,pa,z_height,predicted_flow,dynz_active,accel,fan_pct,pa_delta,accel_delta,temp_target_delta,temp_overshoot,dynz_transition,layer_transition,banding_risk,event_flags
```

And nothing below it, follow these steps:

## Step 1: Check Logging Status

Run this command in your Klipper console:
```
AT_LOG_STATUS
```

This will tell you:
- Is logging active?
- How many samples have been logged?
- What is the status of `at_enabled` and `base_print_temp`?

## Step 2: Verify AT_START Was Called

**Did you call `AT_START` in your print start macro?**

AT_START does TWO critical things:
1. Enables adaptive flow (`at_enabled = True`)
2. Sets `base_print_temp` to your current nozzle temperature
3. Starts the AUTO_TEMP_LOOP (which calls AT_LOG_DATA every second)

**Check your PRINT_START macro includes:**
```gcode
# Heat nozzle FIRST
M109 S{nozzle_temp}  ; Wait for nozzle to reach temperature

# THEN call AT_START (must come AFTER heating)
AT_START MATERIAL=PLA  ; or PETG, ABS, etc.
```

**Common mistake:** Calling AT_START before M109 (nozzle heating)
- Result: `base_print_temp` gets set to room temperature
- Fix: Always call AT_START AFTER nozzle is hot (>170°C)

## Step 3: Check Saved Variables

Run:
```
SAVE_CONFIG
```

Then check `~/printer_data/config/sfs_auto_flow_vars.cfg` contains:
```ini
[Variables]
at_enabled = True
base_print_temp = 200.0  # Or whatever your print temp is (must be >= 170)
```

If `at_enabled = False` or `base_print_temp < 170`, AT_LOG_DATA will not be called.

## Step 4: Verify Loop is Running

After calling AT_START, you should see messages like:
```
AT_LOG: Started logging to /path/to/file.csv
AT_LOG: Waiting for data... (AT_LOG_DATA will be called each loop cycle)
AT_LOG: First data point received - logging active
```

If you see "First data point received", logging is working!

If you DON'T see this message within 1-2 seconds, the loop isn't calling AT_LOG_DATA.

## Step 5: Check for Errors

Look in `/tmp/klippy.log` for errors:
```bash
tail -100 /tmp/klippy.log | grep -i "log\|extruder_monitor"
```

Common errors:
- `extruder_monitor not found` - module not loaded (check printer.cfg includes it)
- `Permission denied` - can't write to log directory
- `Failed to start logging` - check disk space

## Step 6: Manual Test

Try manually calling AT_LOG_DATA:
```gcode
AT_LOG_START MATERIAL=TEST FILE="manual_test"
AT_LOG_DATA TEMP=200 TARGET=200 BOOST=0 FLOW=10 SPEED=50 PWM=0.5 PA=0.04 Z=0.2 PREDICTED=12 DYNZ=0 ACCEL=3000 FAN=100
AT_LOG_STATUS
```

Check if the sample count increased. If yes, logging works but the loop isn't calling it.

## Root Causes and Fixes

### Cause 1: AT_START not called
**Symptom:** `at_enabled = False` in saved variables

**Fix:** Add `AT_START MATERIAL=PLA` to your PRINT_START macro AFTER heating the nozzle

### Cause 2: Nozzle wasn't hot when AT_START was called
**Symptom:** `base_print_temp = 0` or `< 170` in saved variables

**Fix:**
```gcode
# Wrong order:
AT_START MATERIAL=PLA
M109 S200  # Heats AFTER AT_START - base_print_temp will be cold!

# Correct order:
M109 S200  # Heat FIRST
AT_START MATERIAL=PLA  # Then initialize (captures hot temp)
```

### Cause 3: AUTO_TEMP_LOOP not running
**Symptom:** Logging starts but no "First data point received" message

**Check:**
```gcode
AT_ENABLE  # Make sure loop is enabled
```

If loop is disabled, manually start it or check if printer.cfg has the delayed_gcode configured.

### Cause 4: Loop running but condition not met
**Symptom:** Loop is running but _AUTO_TEMP_CORE logic skips AT_LOG_DATA

**The condition (line 304 of auto_flow_defaults.cfg):**
```jinja
{% if saved_base >= 170 and vars.at_enabled|default(False) %}
    # ... AT_LOG_DATA is called here
{% else %}
    # Loop runs but doesn't log
{% endif %}
```

Both conditions MUST be true:
- `saved_base >= 170` - Base temperature is set and hot enough
- `vars.at_enabled = True` - Adaptive flow is enabled

### Cause 5: Data being written but not flushed (FIXED)
**Symptom:** This was the original bug - data in buffer but not on disk

**Fix:** Already implemented in the code - first sample now flushes immediately

## Quick Diagnostic Checklist

Run these commands and check results:

```gcode
AT_LOG_STATUS          # Is logging active? Any samples?
AT_STATUS              # Is adaptive flow running?
```

Expected good output:
```
AT_LOG: ✓ Active - 15 samples over 15.2s
AT_LOG: File: /home/pi/printer_data/logs/adaptive_flow/20260213_153045_test.csv
AT_LOG: at_enabled=True, base_print_temp=205.0
```

Bad output examples:
```
AT_LOG: ✗ Not active - call AT_LOG_START first
# → You didn't call AT_START or it failed

AT_LOG: ✓ Active - 0 samples over 30.5s
# → Loop isn't calling AT_LOG_DATA (check at_enabled and base_temp)

AT_LOG: ⚠️ Adaptive Flow is DISABLED - call AT_START first!
# → at_enabled=False - run AT_START MATERIAL=PLA

AT_LOG: ⚠️ base_print_temp too low (0°C) - heat nozzle and call AT_START!
# → You called AT_START before heating - heat nozzle and call AT_START again
```

## Testing the Fix

1. Restart Klipper after updating the code
2. Heat your nozzle: `M109 S200`
3. Start adaptive flow: `AT_START MATERIAL=PLA`
4. Wait 2 seconds
5. Check status: `AT_LOG_STATUS`
6. Check the file:
   ```bash
   tail ~/printer_data/logs/adaptive_flow/*.csv
   ```

You should see data rows appearing (not just the header).

## Still Not Working?

If you've tried everything above and still only see the header:

1. **Enable debug logging:** Edit extruder_monitor.py and add print statements
2. **Check Klipper logs:** `/tmp/klippy.log` for errors
3. **Verify module loaded:** Run `FIRMWARE_RESTART` and check klippy.log for "ExtruderMonitor" initialization
4. **Check file permissions:** Make sure Klipper can write to `~/printer_data/logs/adaptive_flow/`
   ```bash
   ls -la ~/printer_data/logs/
   mkdir -p ~/printer_data/logs/adaptive_flow
   chmod 755 ~/printer_data/logs/adaptive_flow
   ```

## Report an Issue

If none of this helps, provide:
1. Output of `AT_LOG_STATUS`
2. Output of `AT_STATUS`
3. Your PRINT_START macro
4. Last 50 lines of `/tmp/klippy.log`
5. Contents of `~/printer_data/config/sfs_auto_flow_vars.cfg`
