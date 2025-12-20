# Klipper Adaptive Flow

Dynamic temperature control for E3D Revo hotends. Boosts temp automatically during high-flow sections.

## Install

```bash
cp gcode_interceptor.py extruder_monitor.py ~/klipper/klippy/extras/
cp auto_flow.cfg ~/printer_data/config/
```

Add to `printer.cfg`:
```ini
[include auto_flow.cfg]
[gcode_interceptor]
[extruder_monitor]
```

## Use

**Start G-code** (after heating):
```gcode
AT_START
```

**End G-code**:
```gcode
AT_END
```

## Configure

In `auto_flow.cfg`:
```ini
variable_use_high_flow_nozzle: True   # False for standard Revo
```

Everything else is automatic — material detection, PA, K-values.

## Commands

| Command | What it does |
|---------|--------------|
| `AT_START` | Enable (call after M109) |
| `AT_END` | Disable and save learned values |
| `AT_STATUS` | Show current state |
| `AT_SET_PA MATERIAL=PLA PA=0.04` | Save calibrated PA |

## Default PA Values

| Material | PA |
|----------|-----|
| PLA | 0.040 |
| PETG | 0.060 |
| ABS/ASA | 0.050 |
| TPU | 0.200 |

## Requirements

- E3D Revo hotend (HF or Standard)
- Klipper firmware



