# Troubleshooting Z-Banding and Layer Inconsistencies

This guide helps diagnose and resolve layer inconsistencies, Z-banding, and other periodic artifacts in 3D prints when using Klipper Adaptive Flow.

## What is Z-Banding?

Z-banding appears as regular or irregular patterns, ridges, or inconsistencies across layers in the Z-direction. These artifacts can have multiple causes, both mechanical and software-related.

## Quick Diagnostic Checklist

When you encounter layer inconsistencies:

- [ ] Check if Adaptive Flow was actually enabled during the print
- [ ] Verify temperature stability in klippy.log
- [ ] Check heater PWM saturation
- [ ] Review mechanical components (Z-axis, frame, belts)
- [ ] Analyze pressure advance settings
- [ ] Check cooling fan behavior
- [ ] Review DynZ activation patterns

## Step 1: Verify Adaptive Flow Was Running

### Check Klipper Log

Look for these lines in `/tmp/klippy.log`:

```
// Adaptive Flow: ON (PLA -> _AF_PROFILE_PLA_HF profile)
```

Or check if the CSV log file was created:
```bash
ls -lh ~/printer_data/logs/adaptive_flow/
```

### If Adaptive Flow Wasn't Running

The issue is likely **not** related to Adaptive Flow. Common causes:

1. **Mechanical Issues** (most common):
   - Z-axis binding or wobble
   - Loose belts
   - Frame flex or misalignment
   - Lead screw binding
   - Inconsistent bed springs/tramming

2. **Traditional Slicer Issues**:
   - Inconsistent extrusion multiplier
   - Temperature oscillation (check if heater is tuned)
   - Incorrect pressure advance
   - Over/under extrusion

3. **Electrical Issues**:
   - Stepper motor resonance
   - EMI interference
   - Insufficient stepper current

**Skip to the "Non-Adaptive Flow Issues" section below.**

---

## Step 2: Analyze Temperature Stability

### Extract Temperature Data from Klippy Log

```bash
grep "^Stats" /tmp/klippy.log | tail -1000 | grep "extruder: target" > /tmp/temp_analysis.txt
```

### What to Look For

**Good (Stable Temperature):**
```
extruder: target=220 temp=219.9 pwm=0.858
extruder: target=220 temp=220.0 pwm=0.859
extruder: target=220 temp=220.1 pwm=0.855
```
- Temperature stays within ±0.2°C of target
- PWM between 0.3-0.9 (heater has headroom)

**Bad (Temperature Oscillation):**
```
extruder: target=220 temp=218.5 pwm=1.000
extruder: target=220 temp=221.8 pwm=0.000
extruder: target=220 temp=219.2 pwm=1.000
```
- Temperature swings >1°C
- PWM oscillates between 0.0 and 1.0

### If Temperature Is Unstable

**Solution:** Re-tune your heater PID:
```gcode
PID_CALIBRATE HEATER=extruder TARGET=220
SAVE_CONFIG
```

Run this for each material's typical printing temperature.

---

## Step 3: Check Heater Saturation

### What Is Heater Saturation?

When PWM averages >85% and temperature lags behind target by >3°C, your heater can't keep up with the thermal demand.

### Symptoms

- Visible under-extrusion or inconsistent extrusion
- Temperature creeping down during high-flow sections
- Temperature boost doesn't work as expected

### Solutions

1. **Reduce Maximum Flow Rate** (if using Adaptive Flow):
   ```gcode
   AT_SET_FLOW_K K=0.8  # Reduce from default
   ```

2. **Lower Print Speed**:
   - Reduce maximum print speeds in your slicer
   - Reduce volumetric flow limits

3. **Increase Max Boost Limit** (carefully):
   ```
   # In material_profiles.cfg
   variable_max_boost: 40.0  # Increase from 35.0
   ```

4. **Check Heater Hardware**:
   - Verify you're using a 40W or 60W heater cartridge
   - Check heater connections for resistance
   - Ensure proper thermal paste on thermistor

---

## Step 4: Mechanical Inspection

### Z-Axis Components

1. **Check Z-axis Smoothness**:
   ```gcode
   G28  # Home
   G1 Z50 F300  # Move up slowly
   G1 Z10 F300  # Move down slowly
   ```
   - Motion should be smooth and quiet
   - No binding, grinding, or irregular sounds

2. **Lead Screw Inspection**:
   - Check for dirt or debris
   - Verify proper lubrication
   - Ensure coupling is tight
   - Check for bent lead screw

3. **Z-axis Alignment**:
   - Verify gantry is level
   - Check for frame squareness
   - Ensure linear rails/rods are parallel

### Belt Tension

Under-tensioned or over-tensioned belts can cause periodic artifacts:

1. **Check Belt Tension**:
   - Belts should have a "guitar string" feel
   - No sagging or excessive tightness
   - Use a belt tension meter if available (110Hz typical for 6mm GT2)

2. **Check Belt Condition**:
   - Look for worn teeth
   - Check for cracks or fraying
   - Verify belt alignment on pulleys

### Frame Stability

1. **Check Frame Bolts**:
   - Tighten all frame extrusion bolts
   - Check corner brackets
   - Verify no loose components

2. **Test for Flex**:
   - Manually apply pressure to gantry
   - Check for excessive movement
   - Verify bed mounting is solid

---

## Step 5: Pressure Advance Analysis

### Check Current PA Setting

```gcode
AT_GET_PA MATERIAL=PLA
```

Or check your Klipper config:
```
[extruder]
pressure_advance: 0.055
```

### Symptoms of Incorrect PA

**Too High PA:**
- Gaps at corners
- Under-extrusion at direction changes
- Thin first layers
- Inconsistent extrusion

**Too Low PA:**
- Bulging corners
- Over-extrusion at direction changes
- Thick walls
- Stringing

### Calibrate PA

1. **Print PA Calibration Pattern**:
   - Use Klipper's pressure advance tower
   - Or Ellis' PA calibration print
   - URL: https://ellis3dp.com/Print-Tuning-Guide/articles/pressure_advance.html

2. **Update PA in Adaptive Flow**:
   ```gcode
   AT_SET_PA MATERIAL=PLA PA=0.055
   ```

3. **Test Print**:
   - Run a small test print
   - Verify consistent extrusion

---

## Step 6: Cooling Fan Behavior

### Check Smart Cooling Status

```gcode
AT_SC_STATUS
```

### Verify Fan Isn't Oscillating

Look at the CSV log:
```bash
cd ~/printer_data/logs/adaptive_flow/
tail -100 <your_log_file>.csv | awk -F',' '{print $13}'  # fan_pct column
```

**Good:** Fan percentage changes gradually (50%, 52%, 55%, etc.)  
**Bad:** Fan oscillates rapidly (50%, 70%, 50%, 70%, etc.)

### If Fan Is Oscillating

This can cause thermal cycling and layer inconsistencies:

1. **Disable Smart Cooling** (temporarily):
   ```
   # In auto_flow.cfg
   variable_sc_enabled: False
   ```

2. **Use Fixed Fan Speed**:
   - Set constant fan speed in your slicer
   - E.g., 70% for PLA throughout the print

3. **Tune Smart Cooling** (if you want to keep it):
   ```
   # In material_profiles.cfg
   variable_sc_flow_gate: 10.0  # Increase to reduce sensitivity
   variable_sc_short_layer_time: 15  # Adjust for your print
   ```

---

## Step 7: DynZ Analysis

### Check DynZ Status

```gcode
AT_DYNZ_STATUS
```

### Review DynZ Activation in CSV Log

```bash
cd ~/printer_data/logs/adaptive_flow/
grep "DYNZ:ON" <your_log_file>.csv | wc -l
```

### If DynZ Is Activating Frequently

DynZ activates when it detects stress (high speed + low flow + heater demand). Excessive activation can indicate:

1. **Heater struggling** → Reduce flow_k or print speed
2. **Geometry with many transitions** → Normal for domes, spheres
3. **False positives** → Increase activation threshold

### Tune DynZ (if needed)

```
# In auto_flow.cfg
variable_dynz_activate_score: 1.5  # Increase from 1.0 (less sensitive)
variable_dynz_accel_reduction: 0.6  # Adjust relief strength (0.5-0.8)
```

---

## Non-Adaptive Flow Issues

If Adaptive Flow wasn't running, these are the most common causes:

### 1. Z-Axis Mechanical Issues (60% of cases)

- **Lead screw binding** → Clean and lubricate
- **Z-wobble** → Replace bent lead screw, check coupling
- **Anti-backlash nut too tight** → Loosen slightly
- **Linear rail binding** → Check alignment, clean, lubricate

### 2. Inconsistent Extrusion (20% of cases)

- **Partial nozzle clog** → Cold pull or replace nozzle
- **Extruder gear slipping** → Increase tension, clean gear
- **Inconsistent filament diameter** → Measure with calipers, change brand
- **Heat creep** → Check hotend cooling fan

### 3. Electrical/Resonance (10% of cases)

- **Stepper resonance** → Enable Input Shaper
- **Stepper skipping** → Increase motor current (carefully)
- **EMI interference** → Check wiring, shielding

### 4. Incorrect Slicer Settings (10% of cases)

- **Layer height too aggressive** → Max 75% of nozzle diameter
- **Extrusion multiplier incorrect** → Calibrate e-steps and flow
- **Inconsistent line width** → Match to nozzle size

---

## Using the Print Analyzer

If you have CSV logs from Adaptive Flow:

```bash
cd ~/Klipper-Adaptive-Flow
python3 analyze_print.py
```

The analyzer will:
- Check for heater saturation
- Identify thermal lag issues
- Detect excessive DynZ activation
- Suggest configuration changes

### Example Output

```
Print Duration: 8h 54m
Avg Flow: 6.2 mm³/s, Max: 15.3 mm³/s
Avg Boost: 3.2°C, Max: 12.5°C
Avg PWM: 62%, Max: 98%

DynZ Active: 3% of print
Klippy Issues: None

✓ Print quality: GOOD
✓ Heater performance: GOOD (headroom available)
✓ Thermal response: GOOD (avg lag 1.2°C)
```

---

## Case Study: The Issue from the Bug Report

### Symptoms
- Inconsistent layers visible on Z-axis
- PLA eSun HF cold white
- HF nozzle, 40W heater, 0.2mm layer height
- Adaptive Flow enabled

### Log Analysis

**Temperature (from klippy.log):**
```
extruder: target=220 temp=219.9 pwm=0.858
extruder: target=220 temp=220.0 pwm=0.859
extruder: target=220 temp=220.1 pwm=0.855
```
✓ Temperature is **highly stable** (±0.1°C)  
✓ PWM ~85% shows heater has adequate headroom  
✓ **Not a temperature issue**

**CSV Log:**
- Only header present, no data rows
- Indicates extruder_monitor didn't log data
- Possible causes:
  1. Logging not started with `AT_START`
  2. Extruder_monitor not properly loaded
  3. Print macro issue

### Likely Root Cause

With temperature ruled out, the most likely causes are:

1. **Mechanical Z-binding** (most likely):
   - Lead screw needs cleaning/lubrication
   - Z-axis alignment issue
   - Check for frame flex

2. **Pressure Advance tuning**:
   - May need calibration for this specific filament
   - HF nozzle PA typically 0.025-0.040

3. **Cooling fan oscillation**:
   - If Smart Cooling was too aggressive
   - But no data to confirm this

### Recommended Actions

1. **Mechanical inspection first**:
   ```bash
   # Power off printer
   # Manually move Z-axis up and down
   # Should be smooth with no resistance changes
   ```

2. **Calibrate PA for eSun PLA**:
   ```gcode
   # Print PA calibration pattern
   AT_SET_PA MATERIAL=PLA PA=0.035  # Example value
   ```

3. **Fix logging for future prints**:
   ```gcode
   # In PRINT_START macro, ensure you have:
   AT_START MATERIAL=PLA
   ```

4. **Run a test print with logging enabled**:
   - Enable logging
   - Print same model again
   - Analyze the CSV log with `analyze_print.py`

---

## Prevention Tips

### For Future Prints

1. **Always enable logging**:
   ```gcode
   # In PRINT_START macro
   AT_START MATERIAL={params.MATERIAL|default("PLA")}
   ```

2. **Monitor first few layers**:
   - Watch for consistent extrusion
   - Check temperature stability
   - Verify first layer adhesion

3. **Regular maintenance**:
   - Clean and lubricate Z-axis monthly
   - Check belt tension every few weeks
   - Verify frame bolts are tight
   - Inspect nozzle for wear

4. **Keep calibration up to date**:
   - Re-run PID_CALIBRATE when changing materials
   - Calibrate PA for each new filament
   - Update flow_k if you notice under/over extrusion

---

## Getting Help

### If This Guide Doesn't Solve Your Issue

1. **Capture diagnostic data**:
   ```bash
   # Save temperature stats
   grep "^Stats" /tmp/klippy.log | tail -1000 > ~/temp_stats.txt
   
   # Copy Adaptive Flow logs
   cp ~/printer_data/logs/adaptive_flow/*.csv ~/
   ```

2. **Take photos**:
   - Side view showing layer lines
   - Close-up of problem area
   - Full part for context

3. **Document settings**:
   - Material and profile used
   - Print speed and temperatures
   - PA value
   - Adaptive Flow settings

4. **Create GitHub Issue**:
   - Include all diagnostic data
   - Attach logs and photos
   - Describe what you've already tried

---

## Quick Reference

### Essential Commands

```gcode
AT_STATUS          # Check Adaptive Flow status
AT_DYNZ_STATUS     # Check DynZ behavior
AT_SC_STATUS       # Check Smart Cooling
AT_GET_PA          # Check pressure advance
PID_CALIBRATE HEATER=extruder TARGET=220  # Tune heater
```

### Essential Log Files

```bash
/tmp/klippy.log                           # Klipper system log
~/printer_data/logs/adaptive_flow/*.csv   # Print session data
~/printer_data/config/adaptive_flow_vars.cfg  # Saved settings
```

### Analysis Tool

```bash
cd ~/Klipper-Adaptive-Flow
python3 analyze_print.py  # Analyze most recent print
python3 analyze_print.py --auto  # Auto-apply safe suggestions
```

---

## Conclusion

Layer inconsistencies can have many causes. By systematically working through this guide, you can identify and resolve the issue. Remember:

- **Temperature stability is critical** → Check first
- **Mechanical issues are most common** → Inspect Z-axis
- **Use data to guide diagnosis** → Enable logging
- **One change at a time** → Easier to identify what worked

Happy printing! 🎉
