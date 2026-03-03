# Hardware-Aware Config Parsing — Implementation Plan

**Date:** 2026-03-03  
**Status:** Ready to implement  
**Context:** Klipper-Adaptive-Flow `analyze_print.py` (~5235 lines)

---

## What the Script Currently Analyzes

### Data sources it reads today

| Source | What it extracts |
|---|---|
| **CSV log** (per-print) | Flow rate, temperature, fan %, PA value, accel events, speed, Z height — sampled every ~1s during print |
| **G-code comments** | Slicer settings (accel, speed, layer height, line width, fan, PA, etc.) via `extract_slicer_settings()` |
| `auto_flow_user.cfg` / `auto_flow_defaults.cfg` | `use_high_flow_nozzle`, `max_safe_flow_hf/std`, `sc_heater_wattage` — 3 values total |
| `material_profiles_*.cfg` | Material-specific flow/temp targets |
| **E3D Revo lookup table** (hardcoded) | Published flow limits by nozzle/variant/material |

### Analysis functions (20+)

- `analyze_csv_for_banding` — acceleration event banding detection
- `analyze_z_banding` — Z height periodic artifacts
- `analyze_thermal_lag` — heater response lag
- `analyze_heater_headroom` — heater wattage headroom
- `analyze_pa_stability` — pressure advance consistency
- `analyze_dynz_zones` — dynamic Z compensation zones
- `analyze_speed_flow_distribution` — speed/flow histogram
- `analyze_slicer_vs_banding` — cross-references slicer settings with banding events (HTML issues 1-6)
- `generate_slicer_profile_advice` — comprehensive per-setting advisor (accel/speed/quality)
- `generate_recommendations` — actionable recommendations

---

## What it Does NOT Read (But Should)

All of these files exist in `~/printer_data/config/` and contain parseable hardware data:

| Config File | Available Data | Recommendation Impact |
|---|---|---|
| **printer.cfg** `[printer]` | `kinematics: corexy`, `max_velocity: 500`, `max_accel: 20000`, `square_corner_velocity: 10` | Validate slicer accel/speed against firmware limits; corexy vs cartesian changes accel recommendations |
| **printer.cfg** `[stepper_x/y]` | `rotation_distance: 40`, `microsteps: 32`, `full_steps_per_rotation: 200`, `position_max: 350` | Build volume (350mm), step resolution → min layer height/line width sanity |
| **printer.cfg** `[stepper_z*]` | 4 Z steppers (z, z1, z2, z3) | Quad gantry → Z tilt type, explains Z banding patterns |
| **printer.cfg** `[tmc2209 stepper_x/y]` | `run_current: 0.8`, `stealthchop_threshold: 0`, `interpolate: false`, sensorless homing | Motor current → torque limits at speed; stealthchop off = good for speed |
| **printer.cfg** `[input_shaper]` | `mzv@60.4Hz/37.4Hz`, damping ratios | Flag if slicer accel exceeds recommended for shaper type |
| **ebbcan.cfg** `[extruder]` | `rotation_distance: 4.5`, `nozzle_diameter: 0.4`, `sensor_type: ATC Semitec 104NT-4`, `max_temp: 300` | Direct drive detection (4.5 vs 33.5 = bowden), nozzle size auto-detect, thermistor type → temp accuracy |
| **ebbcan.cfg** `[tmc2209 extruder]` | `run_current: 0.650` | Extruder torque limit → max back-pressure → flow ceiling |
| **ebbcan.cfg** `[autotune_tmc extruder]` | `motor: ldo-36sth20-1004ahg`, `tuning_goal: performance` | Exact motor model → look up stall torque, max RPM from motor database |
| **ebbcan.cfg** `[heater_fan hotend_fan]` | `heater_temp: 120` (on at 120°C) | Hotend fan threshold |
| **btt.cfg** `[fan]` | `max_power: 0.4`, `cycle_time: 0.02`, `hardware_pwm: False` | **Part cooling fan capped at 40%!** Critical — script should flag it and adjust fan recommendations |
| **eddy.cfg** `[probe_eddy_current]` | Eddy probe type, offsets | Probe type affects first layer advice |
| **mmu/** | Happy Hare MMU config present | MMU present → tip-shaping, purge tower, retraction advice |

---

## The Big Wins

1. **Auto-detect nozzle diameter** from `[extruder]` instead of trusting the slicer G-code
2. **Auto-detect direct drive vs bowden** from `rotation_distance` (4.5 = direct, 33.5 = bowden) → completely different retraction/PA advice
3. **Flag the 40% fan cap** in btt.cfg — this has been a known issue and explains why smart cooling struggles
4. **Validate slicer accel against firmware `max_accel`** and input shaper limits
5. **Detect kinematics** (corexy vs cartesian) → different accel/jerk recommendations
6. **Build volume** from `position_max` → travel speed sanity
7. **Motor model lookup** from `autotune_tmc` → stall limits
8. **Thermistor type** → flag if it supports high-temp materials
9. **Quad gantry detection** from 4 Z steppers → Z banding context
10. **MMU presence** → multi-material specific advice

---

## Proposed Architecture

### New function: `collect_printer_hardware(config_dir)`

- Insert after line ~308 in analyze_print.py (after config helpers, before SINGLE-PRINT STATS)
- Reads `printer.cfg`, follows all `[include ...]` directives
- Parses all `[section]` blocks into a structured dict
- Returns a normalized `printer_hw` dict:

```python
{
    'kinematics': 'corexy',
    'build_volume': (350, 350, 350),
    'firmware_max_accel': 20000,
    'firmware_max_velocity': 500,
    'square_corner_velocity': 10,
    'extruder': {
        'drive_type': 'direct',        # from rotation_distance (<=8 = direct, >8 = bowden)
        'rotation_distance': 4.5,
        'nozzle_diameter': 0.4,
        'thermistor': 'ATC Semitec 104NT-4',
        'max_temp': 300,
        'motor': 'ldo-36sth20-1004ahg',   # from [autotune_tmc extruder]
        'tmc_driver': 'tmc2209',
        'run_current': 0.650,
    },
    'part_fan': {
        'max_power': 0.4,              # THE 40% cap from btt.cfg!
        'hardware_pwm': False,
    },
    'input_shaper': {
        'x': {'type': 'mzv', 'freq': 60.4, 'damping': 0.035},
        'y': {'type': 'mzv', 'freq': 37.4, 'damping': 0.068},
    },
    'z_steppers': 4,                    # quad gantry
    'probe_type': 'eddy',
    'mmu_present': True,
    'xy_tmc': {'driver': 'tmc2209', 'run_current': 0.8, 'stealthchop': False},
}
```

### Integration points (5 edits)

1. **Insert `collect_printer_hardware()` function** (~line 308, before SINGLE-PRINT STATS section)
   - Parse `printer.cfg` and all `[include ...]` files
   - Extract all hardware values into normalized dict
   - Robust error handling — returns empty dict on any failure

2. **Wire into `collect_dashboard_data()`** (~line 3950)
   - Call `collect_printer_hardware(CONFIG_DIR)`
   - Store as `data['printer_hw']`
   - Use `printer_hw['extruder']['nozzle_diameter']` as source of truth for nozzle dia (override slicer if available)
   - Use `printer_hw['part_fan']['max_power']` to adjust smart cooling recommendations

3. **Update `generate_slicer_profile_advice()` signature** (~line 718)
   - Add `printer_hw=None` parameter
   - Use `printer_hw['firmware_max_accel']` to validate slicer accel settings
   - Use input shaper data to compute recommended max accel per shaper type
   - Use `printer_hw['extruder']['drive_type']` for retraction/PA advice
   - Use `printer_hw['part_fan']['max_power']` to adjust fan verdicts

4. **Add hardware-aware entries to `generate_recommendations()`** (~line 2863)
   - **Fan cap warning**: If `part_fan.max_power < 1.0`, warn that cooling is limited
   - **Accel vs firmware**: If slicer accel > `firmware_max_accel`, flag it
   - **Input shaper limit**: If accel exceeds safe range for shaper type/freq
   - **Bowden detection**: Different PA/retraction advice for bowden vs direct
   - **Quad gantry**: Context for Z banding analysis
   - **MMU**: Flag if MMU detected but no purge/tip-shaping in slicer

5. **Add hardware info panel to dashboard HTML** (~line 4816 in `generate_dashboard_html()`)
   - New "Printer Hardware" card showing detected hardware
   - Color-coded warnings (e.g., fan cap in red)
   - Shows kinematics, build volume, extruder type, fan config, input shaper, probe

### Input shaper accel limits (reference)

For the input shaper → max accel recommendation:
- **MZV**: `recommended_max_accel ≈ shaper_freq² × 0.56` → 60.4² → ~2043 (X), 37.4² → ~783 (Y)
  - Note: These are *quality* limits; the machine can handle more with some ringing
- **EI**: `recommended_max_accel ≈ shaper_freq² × 0.42`
- **ZV**: `recommended_max_accel ≈ shaper_freq² × 0.84`
- Klipper docs recommend up to `shaper_freq × 100` as "safe" for each axis

Practical approach: use `shaper_freq × 100` as the "safe ceiling" and flag anything above as "may cause visible ringing".

---

## Config Parsing Strategy

The Klipper config format is INI-like:
```ini
[section_name]
key: value
key = value    # both separators valid
# comment lines
; comment lines (rare)
```

The existing `_parse_config_variables()` already handles this for `[gcode_macro ...]` sections. The new parser needs to:

1. Read `printer.cfg`
2. Find all `[include ...]` directives
3. Read each included file
4. Parse all sections into `{section_name: {key: value, ...}}`
5. Handle the fact that sections from different files merge (Klipper does this)

We can reuse/extend `_parse_config_variables()` or write a lightweight version since we just need key-value pairs, not gcode macro variables.

---

## Files to Edit

- `analyze_print.py` — all 5 integration points above
- No config files need changing
- No new dependencies

## KISS Principle

- All detection is fully automatic — zero user configuration needed
- If any config file is missing or unparseable, gracefully degrade (return empty dict)
- Hardware panel shows what was detected so users can verify
- Recommendations use detected hardware but never fail if hardware is unknown

---

## Pending: Uncommitted Changes

Before starting this work, commit the defaults.cfg changes from last session:
```bash
cd ~/Klipper-Adaptive-Flow
git add auto_flow_defaults.cfg
git commit -m "tune: boost limit 50→4, fan floor 0.20→0.50"
```

Also copy from config:
```bash
cp ~/printer_data/config/auto_flow_defaults.cfg ~/Klipper-Adaptive-Flow/auto_flow_defaults.cfg
```
