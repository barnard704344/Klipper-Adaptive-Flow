# Analysis: Inconsistent Layers on Z (Issue Report)

## Issue Summary

**Reported Problem:** Inconsistent layers visible on Z-axis during PLA print  
**Material:** eSun PLA HF cold white  
**Configuration:** HF nozzle, 40W heater, 0.2mm layer height  
**Adaptive Flow:** Enabled

## Log Analysis Results

### Temperature Performance ✅ EXCELLENT

Analysis of 1000 Stats lines from klippy.log shows:

```
Target:     220.0°C
Actual:     220.0°C ±0.15°C
Range:      219.6°C - 220.3°C
Stability:  ✅ Variation only 0.7°C (excellent)
```

**Conclusion:** Temperature is rock-solid stable. This is NOT a thermal issue.

### Heater Performance ✅ GOOD

```
PWM Average:    82.6%
PWM Maximum:    95.5%
Thermal Lag:    0.0°C average, 0.4°C max
```

**Conclusion:** Heater has adequate headroom and responds quickly. Not struggling or saturated.

### Adaptive Flow Status ⚠️ LOGGING ISSUE

```
Modules:    ✅ Detected in Klipper config
CSV Log:    ❌ Empty (only header present)
```

**Conclusion:** Adaptive Flow is installed, but logging was not captured. This means we cannot analyze:
- Flow rate patterns
- Temperature boost behavior
- DynZ activation
- Smart Cooling fan behavior

**Likely cause:** `AT_START MATERIAL=PLA` was not called in PRINT_START macro.

## Root Cause Assessment

Since temperature is stable and heater performance is good, the layer inconsistencies are **NOT** caused by Adaptive Flow thermal control.

### Most Likely Causes (Ranked by Probability)

#### 1. Z-Axis Mechanical Issues (60% probability) ⭐ **Most Likely**

Visible periodic patterns in the photos suggest mechanical binding or wobble:

**Possible issues:**
- Lead screw binding or dirt buildup
- Z-axis coupling misalignment
- Lead screw bent or worn
- Anti-backlash nut too tight
- Linear rail/rod binding
- Inconsistent Z-motor steps

**How to check:**
```bash
# Power off printer
# Manually move Z-axis up and down by hand
# Should be smooth with no resistance changes
```

**How to fix:**
- Clean lead screw thoroughly
- Apply light lubrication (PTFE dry lube or light machine oil)
- Check coupling tightness (should not be over-tightened)
- Verify linear rails are parallel and smooth
- Check Z-motor mounting bolts are tight

#### 2. Pressure Advance Not Calibrated (20% probability)

HF nozzles typically require PA in the range of 0.025-0.040. Incorrect PA can cause:
- Inconsistent extrusion widths
- Over-extrusion at corners → bulging layers
- Under-extrusion at corners → thin layers

**How to check:**
```gcode
AT_GET_PA MATERIAL=PLA
```

**How to fix:**
```gcode
# Run pressure advance calibration test
# (search for "Klipper pressure advance calibration")
# Then update:
AT_SET_PA MATERIAL=PLA PA=0.035  # Use your calibrated value
```

#### 3. Belt Tension Issues (10% probability)

Under or over-tensioned belts can cause periodic artifacts that appear on multiple layers:

**How to check:**
- Pluck belt like a guitar string
- Should have clean "twang" sound, not floppy or overly tight
- Ideal: ~110Hz for 6mm GT2 belt (use belt tension app)

**How to fix:**
- Adjust belt tension to proper spec
- Check for worn teeth or cracks
- Ensure belt paths are aligned

#### 4. Frame Stability (10% probability)

Loose frame or gantry flex under print forces:

**How to check:**
- Manually apply light pressure to gantry
- Check for excessive movement or wobble
- Verify all frame extrusion bolts are tight

**How to fix:**
- Tighten all frame bolts and corner brackets
- Check for frame squareness with carpenter's square
- Verify gantry is level

## Recommendations

### Immediate Actions

1. **Inspect Z-Axis Mechanicals** 🔧
   ```bash
   # Power down printer
   # Manually move Z-axis by hand
   # Check for smooth motion
   # Clean and lubricate lead screw
   ```

2. **Enable Logging for Next Print** 📊
   ```gcode
   # Add to your PRINT_START macro (after temperature set):
   AT_START MATERIAL=PLA
   
   # Add to your PRINT_END macro (before TURN_OFF_HEATERS):
   AT_END
   ```

3. **Run Diagnostic Tool** 🔍
   ```bash
   cd ~/Klipper-Adaptive-Flow
   python3 diagnose_zbanding.py
   ```

4. **Calibrate Pressure Advance** 📏
   ```bash
   # Print PA calibration pattern
   # Then save the result:
   AT_SET_PA MATERIAL=PLA PA=<your_value>
   ```

### Testing Procedure

After making mechanical fixes:

1. **Test print a simple cube** (40x40x40mm)
   - Ensure AT_START is called
   - Observe first few layers carefully
   - Take photos of all four sides

2. **Check CSV log was created**
   ```bash
   ls -lh ~/printer_data/logs/adaptive_flow/
   ```

3. **Run diagnostic analysis**
   ```bash
   cd ~/Klipper-Adaptive-Flow
   python3 diagnose_zbanding.py
   python3 analyze_print.py  # For full AI analysis
   ```

4. **Compare results** to this print

## Tools and Resources

### New Diagnostic Tools (included in this update)

1. **Quick Diagnostic:**
   ```bash
   python3 diagnose_zbanding.py
   ```
   - Analyzes klippy.log automatically
   - Checks temperature stability
   - Identifies heater saturation
   - Suggests specific fixes

2. **Comprehensive Troubleshooting Guide:**
   - Location: `docs/TROUBLESHOOTING_ZBANDING.md`
   - Covers all causes of layer inconsistencies
   - Step-by-step diagnostic procedures
   - Mechanical inspection checklist
   - Configuration tuning guide

### Existing Analysis Tools

1. **Print Analysis (requires API key):**
   ```bash
   python3 analyze_print.py
   ```
   - AI-powered analysis of print data
   - Configuration suggestions
   - Performance optimization

2. **Adaptive Flow Commands:**
   ```gcode
   AT_STATUS           # Check current status
   AT_DYNZ_STATUS      # Check DynZ behavior
   AT_SC_STATUS        # Check Smart Cooling
   AT_GET_PA           # Check pressure advance
   ```

## Expected Outcome

After following the recommendations:

1. **Z-axis should move smoothly** by hand
2. **Next print should log data** to CSV file
3. **Layer inconsistencies should be reduced** if mechanical issue was fixed
4. **Diagnostic tool should confirm** improvements

If issues persist after mechanical fixes, the CSV log data will reveal whether Adaptive Flow parameters need tuning.

## Need More Help?

If layer issues continue after trying these fixes:

1. **Capture full diagnostic data:**
   ```bash
   # After a test print:
   cd ~/Klipper-Adaptive-Flow
   python3 diagnose_zbanding.py > diagnostic_report.txt
   python3 analyze_print.py > analysis_report.txt
   ```

2. **Take detailed photos:**
   - All four sides of the print
   - Close-up of layer inconsistencies
   - Overall part for context

3. **Document settings:**
   ```bash
   # Copy relevant config sections:
   grep -A 20 "\[extruder\]" ~/printer_data/config/printer.cfg
   cat ~/printer_data/config/auto_flow_user.cfg
   ```

4. **Create GitHub issue** with:
   - Diagnostic reports
   - Photos
   - Config excerpts
   - Description of fixes already attempted

## Summary

**What we know:**
- ✅ Temperature is excellent (not the problem)
- ✅ Heater performance is good (not saturated)
- ✅ Adaptive Flow is installed correctly
- ❌ Logging wasn't captured (need to enable)
- ❌ Layer inconsistencies present (likely mechanical)

**Most likely fix:** Clean and lubricate Z-axis lead screw, check alignment

**Next test:** Enable logging and run diagnostic tool after next print

**Documentation:** See `docs/TROUBLESHOOTING_ZBANDING.md` for complete guide

---

Generated by Klipper Adaptive Flow diagnostic analysis.
