# Commands

Complete reference for all Klipper Adaptive Flow commands.

## Table of Contents

- [Core Commands](#core-commands) - `AT_START`, `AT_END`
- [Status Commands](#status-commands) - `AT_STATUS`, `AT_THERMAL_STATUS`, `AT_DYNZ_STATUS`, `AT_SC_STATUS`
- [Configuration Commands](#configuration-commands) - `AT_SET_PA`, `AT_GET_PA`, `AT_LIST_PA`, etc.
- [Advanced Commands](#advanced-commands) - `AT_ENABLE`, `AT_DISABLE`, `AT_RESET_STATE`
- [Quick Reference](#quick-reference) - Command summary table
- [Common Workflows](#common-workflows) - Usage examples
- [Troubleshooting](#troubleshooting) - Problem solving guide

---

## Core Commands

### `AT_START`

Start adaptive temperature control. Call this after heating in your `PRINT_START` macro.

**Parameters:**
- `MATERIAL=` - Material type (e.g., PLA, PETG, ABS). If omitted, auto-detects from extruder temperature.

**Example:**
```gcode
AT_START MATERIAL=PLA
```

**Notes:**
- Must be called before printing begins
- Enables dynamic temperature and PA control
- Automatically loads the appropriate material profile

---

### `AT_END`

Stop adaptive temperature control. Call this before `TURN_OFF_HEATERS` in your `PRINT_END` macro.

**Example:**
```gcode
AT_END
TURN_OFF_HEATERS
```

**Notes:**
- Must be called **before** `TURN_OFF_HEATERS` to properly shut down the control loop
- Resets temperature to base value
- Restores base Pressure Advance

---

## Status Commands

### `AT_STATUS`

Display full system status including current flow, temperature boost, and PA adjustments.

**Example output:**
```
Adaptive Flow Status
--------------------
Enabled: True
Material: PLA
Base temp: 215°C | Current target: 233°C | Boost: +18°C
Current flow: 18.5 mm³/s | Speed: 150 mm/s
Base PA: 0.060 | Adjusted PA: 0.042
Flow gate: 10.0 mm³/s | Flow K: 1.00
Max boost: 30°C | Max temp: 245°C
```

**Notes:**
- Use during printing to monitor system behavior
- Shows real-time flow calculations and adjustments
- Displays all material profile parameters

---

### `AT_THERMAL_STATUS`

Display thermal safety status and monitor heater performance.

**Example output:**
```
Thermal Safety Status
--------------------
Current temp: 233.4°C
Target temp: 235.0°C
Base temp: 215°C
Current boost: +18°C
Thermal faults: 0
```

**Notes:**
- Shows if the heater is keeping up with demands
- Tracks thermal fault count (heater too slow warnings)
- Use to diagnose heating issues

---

### `AT_DYNZ_STATUS`

Display Dynamic Z-Window (DynZ) learning status.

**Example output:**
```
Dynamic Z-Window Status
-----------------------
Enabled: True
Active: True (stress detected)
Current Z: 15.2mm (bin 30)
Bin height: 0.5mm
Stress bins: 8, 15, 22, 29, 30
Accel reduction: 30%
```

**Notes:**
- Shows learned stress zones
- Displays current acceleration reduction
- Use to monitor dome/sphere detection

**See also:** [DYNZ.md](DYNZ.md) for full documentation

---

### `AT_SC_STATUS`

Display Smart Cooling status and fan adjustments.

**Example output:**
```
Smart Cooling Status
--------------------
Enabled: True
Current fan: 35%
Base fan: 80% (from slicer)
Flow adjustment: -45% (high flow detected)
Target fan: 35%
Current flow: 18.5 mm³/s
Layer time: 12.3s
```

**Notes:**
- Shows real-time fan calculations
- Displays flow-based and layer-time adjustments
- Use to verify Smart Cooling behavior

**See also:** [SMART_COOLING.md](SMART_COOLING.md) for full documentation

---

## Configuration Commands

### `AT_SET_PA`

Save a calibrated Pressure Advance value for a specific material.

**Parameters:**
- `MATERIAL=` - Material name (required)
- `PA=` - Pressure Advance value (required)

**Example:**
```gcode
AT_SET_PA MATERIAL=PLA PA=0.055
AT_SET_PA MATERIAL=PETG PA=0.065
```

**Notes:**
- Values are saved persistently in `~/printer_data/config/variables.cfg`
- Overrides default PA from material profile
- Use after running PA calibration for your specific filament

---

### `AT_GET_PA`

Show the saved or default PA value for a material.

**Parameters:**
- `MATERIAL=` - Material name (default: PLA)

**Example:**
```gcode
AT_GET_PA MATERIAL=PETG
```

**Example output:**
```
PA for PETG: 0.065 (custom)
```

---

### `AT_LIST_PA`

List all saved PA values and defaults for all materials.

**Example output:**
```
Saved Pressure Advance Values:
  PLA: 0.055 (custom)
  PETG: 0.065 (custom)
  ABS: 0.040 (default)
  ASA: 0.040 (default)
  TPU: 0.025 (default)
  NYLON: 0.045 (default)
  PC: 0.050 (default)
  HIPS: 0.035 (default)
```

---

### `AT_SET_FLOW_K`

Temporarily adjust the flow-based temperature boost coefficient.

**Parameters:**
- `K=` - New flow_k value (required)

**Example:**
```gcode
AT_SET_FLOW_K K=1.2
```

**Notes:**
- Changes take effect immediately during the current print
- Does **not** persist after print ends
- To make permanent changes, edit `material_profiles.cfg`

---

### `AT_SET_FLOW_GATE`

Temporarily adjust the minimum flow threshold for temperature boost.

**Parameters:**
- `GATE=` - Flow rate in mm³/s (required)

**Example:**
```gcode
AT_SET_FLOW_GATE GATE=12.0
```

**Notes:**
- Temperature boost only applies above this flow rate
- Changes take effect immediately
- Does **not** persist after print ends
- To make permanent changes, edit `material_profiles.cfg`

---

### `AT_SET_MAX`

Set the absolute maximum temperature limit.

**Parameters:**
- `MAX=` - Temperature in °C (required)

**Example:**
```gcode
AT_SET_MAX MAX=260
```

**Notes:**
- Safety limit to prevent exceeding material capabilities
- Saved persistently in `~/printer_data/config/variables.cfg`
- Should match or be lower than `max_temp` in your material profile

---

### `AT_INIT_MATERIAL`

Manually load a material profile.

**Parameters:**
- `MATERIAL=` - Material name (required)

**Example:**
```gcode
AT_INIT_MATERIAL MATERIAL=PETG
```

**Notes:**
- Normally called automatically by `AT_START`
- Useful for testing material profiles
- Loads all profile parameters (flow_k, max_boost, PA, etc.)

---

## Advanced Commands

### `AT_ENABLE`

Manually enable adaptive temperature control.

**Notes:**
- Normally called automatically by `AT_START`
- Use only if you need to manually control the system
- Requires extruder to already be at target temperature

---

### `AT_DISABLE`

Manually disable adaptive temperature control.

**Notes:**
- Normally called automatically by `AT_END`
- Returns to base temperature and PA
- Control loop stops immediately

---

### `AT_RESET_STATE`

Reset all runtime state variables to defaults.

**Notes:**
- Clears DynZ learning data for current print
- Resets thermal fault counters
- Does **not** clear saved PA values
- Use if the system gets into an unexpected state

---

### `AT_WAIT_TEMP`

Wait for nozzle temperature to stabilize (internal use).

**Notes:**
- Used internally by multi-object temperature management
- Automatically called when switching between sequential objects
- Waits until temperature is within tolerance of target

---

## Legacy/Internal Commands

### `EXCLUDE_OBJECT_START`

Internal command - do not call directly.

**Notes:**
- Automatically triggered by modern slicers (OrcaSlicer, PrusaSlicer)
- Manages temperature transitions between sequential objects

---

### `M486`

Legacy G-code command for object labeling - handled natively by Klipper.

**Notes:**
- Automatically triggered by legacy slicers
- Handled by Klipper's exclude_object module
- Internally calls EXCLUDE_OBJECT_START, which manages temperature transitions

---

## Python Module Commands

These commands are provided by the Python modules (`extruder_monitor.py`, `gcode_interceptor.py`) and are used internally. You generally don't need to call these directly.

### `EXTRUDER_MONITOR_STATUS`

Show extruder monitoring statistics.

**Example output:**
```
Extruder Monitor Status
-----------------------
Lookahead enabled: True
Buffer size: 5.2 seconds
Current flow: 18.5 mm³/s
Peak flow: 22.3 mm³/s
Average flow: 14.2 mm³/s
```

---

### `EXTRUDER_MONITOR_RESET`

Reset extruder monitoring statistics.

---

## Material Profiles

Material profiles define the boost curves and safety limits for each filament type. They are **not** commands, but are loaded automatically by `AT_START` or `AT_INIT_MATERIAL`.

Available profiles:
- `_AF_PROFILE_PLA` - Default/high-flow PLA variants
- `_AF_PROFILE_PETG` - PETG
- `_AF_PROFILE_ABS` - ABS
- `_AF_PROFILE_ASA` - ASA
- `_AF_PROFILE_TPU` - Flexible TPU
- `_AF_PROFILE_NYLON` - Nylon (PA, PA-CF)
- `_AF_PROFILE_PC` - Polycarbonate
- `_AF_PROFILE_HIPS` - High-impact polystyrene

**See also:** [CONFIGURATION.md](CONFIGURATION.md) for material profile parameters

---

## Quick Reference

| Command | Purpose | When to Use |
|---------|---------|-------------|
| `AT_START` | Enable system | In `PRINT_START` macro after heating |
| `AT_END` | Disable system | In `PRINT_END` macro before `TURN_OFF_HEATERS` |
| `AT_STATUS` | Show full status | During print to monitor behavior |
| `AT_DYNZ_STATUS` | Show DynZ status | Check dome/sphere detection |
| `AT_SC_STATUS` | Show fan status | Verify Smart Cooling behavior |
| `AT_SET_PA` | Save PA value | After PA calibration |
| `AT_LIST_PA` | List all PA values | Check saved calibrations |
| `AT_SET_FLOW_K` | Adjust boost | Test different boost curves |
| `AT_SET_FLOW_GATE` | Adjust threshold | Test flow activation point |

---

## Common Workflows

### Initial Setup
```gcode
# In your PRINT_START macro
AT_START MATERIAL={params.MATERIAL|default("PLA")}
```

### Calibrating Pressure Advance
```gcode
# After running PA calibration test
AT_SET_PA MATERIAL=PLA PA=0.055
AT_LIST_PA  # Verify it was saved
```

### Monitoring a Print
```gcode
# During print, in console
AT_STATUS        # Check overall behavior
AT_DYNZ_STATUS   # Check if DynZ is learning
AT_SC_STATUS     # Check fan adjustments
```

### Testing Material Profiles
```gcode
# Test a profile before printing
AT_INIT_MATERIAL MATERIAL=PETG
AT_STATUS  # Verify settings loaded correctly
```

### Tuning Temperature Boost
```gcode
# During a test print
AT_SET_FLOW_K K=1.2        # Increase boost
# ... observe print quality ...
AT_SET_FLOW_K K=0.8        # Reduce boost
# If happy, edit material_profiles.cfg to make permanent
```

---

## Troubleshooting

### System Not Activating
```gcode
AT_STATUS  # Check if enabled
# If disabled:
AT_START MATERIAL=PLA
```

### Temperature Not Boosting
```gcode
AT_STATUS  # Check current flow rate
# If flow is below flow_gate:
AT_SET_FLOW_GATE GATE=5.0  # Lower threshold for testing
```

### Heater Can't Keep Up
```gcode
AT_THERMAL_STATUS  # Check thermal faults
# If faults > 0:
AT_SET_FLOW_K K=0.8  # Reduce boost demand
```

### PA Issues
```gcode
AT_LIST_PA  # Check saved values
AT_GET_PA MATERIAL=PLA  # Check specific material
# If value is wrong:
AT_SET_PA MATERIAL=PLA PA=0.050  # Correct it
```

---

## See Also

- [README.md](../README.md) - Installation and quick start
- [CONFIGURATION.md](CONFIGURATION.md) - Detailed configuration reference
- [DYNZ.md](DYNZ.md) - Dynamic Z-Window documentation
- [SMART_COOLING.md](SMART_COOLING.md) - Smart Cooling documentation
- [ANALYSIS.md](ANALYSIS.md) - Print analysis and AI tuning
