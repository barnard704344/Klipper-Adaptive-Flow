# Speed Guard

Speed Guard automatically slows down tricky sections of your print to prevent ringing and surface artifacts. It watches every layer and temporarily reduces acceleration when it detects stress — then restores full speed when conditions improve.

> Internally known as "DynZ" (Dynamic Z-Window). Config variables still use the `dynz_` prefix.

## The Problem

Certain geometries create "stress zones" where:
- The toolhead makes rapid direction changes (high speed)
- Extrusion segments are very short (low volumetric flow)
- The heater is already working hard (high PWM duty cycle)

This combination — common on domes, spheres, overhangs, and small circular features — can cause thermal lag, inconsistent extrusion, and visible surface artifacts.

## How Speed Guard Works

1. **Monitoring**: Speed Guard divides the print into Z-height bins (default 2mm) and monitors stress conditions in each
2. **Detection**: When speed is high, flow is low, and heater PWM is high simultaneously, the layer is flagged as stressed
3. **Scoring**: Each Z bin accumulates a stress score over time (scores decay when conditions improve)
4. **Slowing down**: When a bin's score exceeds the threshold, Speed Guard reduces acceleration to ease thermal demand
5. **Memory**: Stress scores persist across prints via Klipper's `[save_variables]`, so the system remembers where problem areas start — even between separate print jobs

## Configuration

Speed Guard is enabled by default. To customize, edit `~/printer_data/config/auto_flow_user.cfg`:

```ini
# Enable/disable Speed Guard
variable_dynz_enable: True

# Z bin size (2.0mm = stable, 0.5mm = more sensitive)
variable_dynz_bin_height: 2.0

# Stress detection thresholds
variable_dynz_speed_thresh: 80.0      # mm/s toolhead speed
variable_dynz_flow_max: 8.0           # mm³/s volumetric flow
variable_dynz_pwm_thresh: 0.70        # heater duty cycle (0-1)

# Score thresholds
variable_dynz_activate_score: 6.0     # score to trigger slowdown
variable_dynz_deactivate_score: 1.5   # score to restore full speed

# Relief method: 'temp_reduction' (recommended) or 'accel_limit' (legacy)
variable_dynz_relief_method: 'temp_reduction'

# Temperature reduction during stress (used with temp_reduction method)
variable_dynz_temp_reduction: 8.0     # °C to reduce boost by

# Acceleration during stress (used with accel_limit method)
variable_dynz_accel_relief: 3200      # mm/s² (lower = gentler moves)
```

### Relief Methods

| Method | What it does | Recommended? |
|--------|-------------|:---:|
| `temp_reduction` | Reduces temperature boost to ease thermal demand. Smoother, prevents banding. | ✅ |
| `accel_limit` | Directly limits toolhead acceleration. Legacy approach, may cause visible banding. | ❌ |

### Parameter Guide

| Parameter | What it does | Default |
|-----------|-------------|---------|
| `dynz_enable` | Turn Speed Guard on/off | True |
| `dynz_bin_height` | Height of each Z bin in mm. Smaller = more granular, larger = more stable | 2.0 |
| `dynz_speed_thresh` | Toolhead speed (mm/s) above which stress is considered | 80.0 |
| `dynz_flow_max` | Flow rate (mm³/s) below which stress is considered | 8.0 |
| `dynz_pwm_thresh` | Heater PWM (0-1) above which stress is considered | 0.70 |
| `dynz_score_inc` | Score added per stress detection | 1.0 |
| `dynz_score_decay` | Score multiplier when no stress (0.9 = 10% decay) | 0.90 |
| `dynz_activate_score` | Score threshold to start slowing down | 6.0 |
| `dynz_deactivate_score` | Score threshold to restore full speed | 1.5 |
| `dynz_relief_method` | How to relieve stress: `temp_reduction` or `accel_limit` | `temp_reduction` |
| `dynz_temp_reduction` | °C to reduce boost by (temp_reduction method) | 8.0 |
| `dynz_accel_relief` | Acceleration limit when slowed (accel_limit method) | 3200 |
| `dynz_base_accel` | Stored base accel before slowdown is applied (internal) | 0 |

> **Note:** Stress scores are only saved to disk when the score changes by more than 0.5, reducing SD card wear while maintaining history.

## Monitoring

Check Speed Guard status during a print:
```
AT_DYNZ_STATUS
```

> See [COMMANDS.md](COMMANDS.md#at_dynz_status) for full command documentation.

Example output:
```
===== SPEED GUARD STATUS =====
Speed Guard: ENABLED
State: ACTIVE (slowed down)
Z Height: 45.20 mm
Z Bin: 22 (bin height 2.0 mm)
Bin Score: 7.23
Slowdown at ≥ 6.0
Restore at ≤ 1.5
Relief Method: temp_reduction
Temp Reduction: 8.0°C
Accel (current): 5000 mm/s²
Accel (base):    5000 mm/s²
Accel (relief):  3200 mm/s²
Mode: SLOWING (stress detected)
===============================
```

### Status Modes

| Mode | What it means |
|------|-------------|
| **IDLE** | No stress detected — printing at full speed |
| **TRACKING** | Stress detected, accumulating score (not yet at threshold) |
| **SLOWING** | Score exceeded threshold — acceleration is reduced to protect quality |

## How Stress Detection Works

Speed Guard considers a moment "stressed" when ALL three conditions are true simultaneously:

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

### Slow down more aggressively (for problem prints)
```ini
variable_dynz_speed_thresh: 60.0      # Lower = catches slower moves
variable_dynz_flow_max: 10.0          # Higher = catches higher flows
variable_dynz_pwm_thresh: 0.60        # Lower = catches less heater stress
variable_dynz_activate_score: 3.0     # Lower = activates sooner
```

### Slow down less (if prints are already clean)
```ini
variable_dynz_speed_thresh: 100.0     # Higher = only catches very fast moves
variable_dynz_flow_max: 6.0           # Lower = only catches very low flows
variable_dynz_pwm_thresh: 0.80        # Higher = only catches heavy heater use
variable_dynz_activate_score: 6.0     # Higher = needs more stress to activate
```

### Smoother transitions
```ini
variable_dynz_score_decay: 0.95       # Slower decay = longer memory
variable_dynz_deactivate_score: 2.5   # Higher = stays slowed longer
```

## Clearing Learned Data

Speed Guard stores stress scores in Klipper's save_variables. To clear all learned data:

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
    RESPOND MSG="Speed Guard: Clear bin scores by editing variables.cfg"
    RESPOND MSG="Remove all lines starting with dynz_bin_"
```

## Dashboard

The **Speed Guard** tab on the dashboard shows:
- **Yellow bars**: % of time each layer was slowed down
- **Red bars**: How often speed switched between fast and slow at each height — frequent switching can itself cause visible lines

The Speed Guard card on the main dashboard shows the overall percentage of layers that were slowed, and clicking it gives a detailed breakdown.

See [ANALYSIS.md](ANALYSIS.md) for more on the dashboard.

## Logging

When print logging is enabled, Speed Guard state is captured in the CSV:
- `dynz_active`: 1 if currently slowed, 0 if at full speed
- `accel`: Current acceleration value
- `banding_risk`: Includes speed transitions in the risk score
