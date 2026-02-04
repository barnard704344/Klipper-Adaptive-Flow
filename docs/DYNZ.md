# Dynamic Z-Window (DynZ)

DynZ is an intelligent learning system that detects and adapts to challenging print geometries like convex surfaces, domes, and spheres.

## The Problem

Convex surfaces create "stress zones" where:
- High toolhead speed (rapid direction changes)
- Low volumetric flow (short segments)
- High heater demand (constant temp adjustments)

This combination can cause thermal lag, inconsistent extrusion, and surface artifacts.

## How DynZ Works

1. **Learning**: DynZ divides Z-height into bins (default 1mm) and tracks stress conditions in each
2. **Detection**: When speed is high, flow is low, and heater PWM is high simultaneously, it's flagged as stress
3. **Scoring**: Each Z bin accumulates a stress score over time (scores decay when conditions improve)
4. **Relief**: When a bin's score exceeds the threshold, DynZ reduces acceleration to ease thermal demand
5. **Memory**: Stress patterns persist across layers, so the system "remembers" where domes start

## Configuration

DynZ is enabled by default. To customize, edit `auto_flow_user.cfg`:

```ini
# Enable/disable DynZ
variable_dynz_enable: True

# Z bin size (1.0mm = stable, 0.5mm = more sensitive)
variable_dynz_bin_height: 1.0

# Stress detection thresholds
variable_dynz_speed_thresh: 80.0      # mm/s toolhead speed
variable_dynz_flow_max: 8.0           # mm³/s volumetric flow
variable_dynz_pwm_thresh: 0.70        # heater duty cycle (0-1)

# Score thresholds
variable_dynz_activate_score: 4.0     # score to trigger relief
variable_dynz_deactivate_score: 1.5   # score to exit relief

# Acceleration during stress relief
variable_dynz_accel_relief: 3200      # mm/s² (lower = gentler moves)
```

### Parameter Guide

| Parameter | Description | Default |
|-----------|-------------|---------|
| `dynz_enable` | Master enable/disable | True |
| `dynz_bin_height` | Height of each Z bin in mm. Smaller = more granular, larger = more stable | 1.0 |
| `dynz_speed_thresh` | Toolhead speed (mm/s) above which stress is considered | 80.0 |
| `dynz_flow_max` | Flow rate (mm³/s) below which stress is considered | 8.0 |
| `dynz_pwm_thresh` | Heater PWM (0-1) above which stress is considered | 0.70 |
| `dynz_score_inc` | Score added per stress detection | 1.0 |
| `dynz_score_decay` | Score multiplier when no stress (0.9 = 10% decay) | 0.90 |
| `dynz_activate_score` | Score threshold to enable accel relief | 4.0 |
| `dynz_deactivate_score` | Score threshold to disable accel relief | 1.5 |
| `dynz_accel_relief` | Acceleration limit during stress relief (mm/s²) | 3200 |

## Monitoring

Check DynZ status during a print:
```
AT_DYNZ_STATUS
```

> See [COMMANDS.md](COMMANDS.md#at_dynz_status) for full command documentation.

Output:
```
===== DYNZ STATUS =====
DynZ: ENABLED
State: ACTIVE (accel relief applied)
Z Height: 45.20 mm
Z Bin: 45 (bin height 1.0 mm)
Bin Score: 5.23
Activate ≥ 4.0
Deactivate ≤ 1.5
Accel (current): 3200 mm/s²
Accel (base):    5000 mm/s²
Accel (relief):  3200 mm/s²
Mode: CLAMPING (convex stress detected)
=======================
```

### Status Modes

| Mode | Description |
|------|-------------|
| **IDLE** | No stress detected, bin score is 0 |
| **LEARNING** | Stress detected, accumulating score (not yet at threshold) |
| **CLAMPING** | Score exceeded threshold, acceleration relief active |

## How Stress Detection Works

DynZ considers a moment "stressed" when ALL three conditions are true simultaneously:

```
Speed > dynz_speed_thresh (80 mm/s)
  AND
Flow < dynz_flow_max (8 mm³/s)
  AND
Heater PWM > dynz_pwm_thresh (0.70)
```

This combination typically occurs on:
- Dome tops (rapid small movements)
- Sphere surfaces (constant direction changes)
- Small circular features at height
- Any geometry with lots of short, fast segments

## Tuning Tips

### For More Aggressive Detection
```ini
variable_dynz_speed_thresh: 60.0      # Lower = catches slower moves
variable_dynz_flow_max: 10.0          # Higher = catches higher flows
variable_dynz_pwm_thresh: 0.60        # Lower = catches less heater stress
variable_dynz_activate_score: 3.0     # Lower = activates sooner
```

### For Less Aggressive Detection
```ini
variable_dynz_speed_thresh: 100.0     # Higher = only catches very fast moves
variable_dynz_flow_max: 6.0           # Lower = only catches very low flows
variable_dynz_pwm_thresh: 0.80        # Higher = only catches heavy heater use
variable_dynz_activate_score: 6.0     # Higher = needs more stress to activate
```

### For Smoother Transitions
```ini
variable_dynz_score_decay: 0.95       # Slower decay = longer memory
variable_dynz_deactivate_score: 2.5   # Higher = stays active longer
```

## Clearing Learned Data

DynZ stores bin scores in Klipper's save_variables. To clear all learned stress zones:

```bash
# SSH into your printer
cd ~/printer_data/config
# Edit your save_variables file and remove lines starting with "dynz_bin_"
```

Or create a macro:
```ini
[gcode_macro AT_DYNZ_CLEAR]
gcode:
    # Note: This requires manual editing of variables.cfg
    RESPOND MSG="DynZ: Clear bin scores by editing variables.cfg"
    RESPOND MSG="Remove all lines starting with dynz_bin_"
```

## Logging

When print logging is enabled, DynZ state is captured in the CSV:
- `dynz_active`: 1 if relief is active, 0 if not
- `accel`: Current acceleration value

This allows post-print analysis to see when and where DynZ activated.
