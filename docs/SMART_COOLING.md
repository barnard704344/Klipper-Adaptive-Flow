# Smart Cooling

Smart Cooling takes full control of the part cooling fan based on flow rate, layer time, and heater performance. It calculates the optimal fan speed from each material's profile and overrides slicer fan commands — except for bridges and overhangs, which are always respected.

## How It Works

1. **Override mode**: SC intercepts slicer `M106` commands and calculates absolute fan speed from the material profile's `sc_max_fan` ceiling. No more fighting between SC and slicer fan curves.

2. **Flow-based reduction**: At high flow rates, the fast-moving plastic creates its own airflow and needs less fan cooling. SC reduces fan speed proportionally from the material ceiling.

3. **Layer time boost**: Short layers (fast prints or small features) don't have enough time to cool between layers. SC increases fan speed for these quick layers.

4. **Bridge/overhang passthrough**: SC intercepts M106 commands from the slicer. If the slicer requests MORE fan than SC calculates (e.g., bridge at 100%), the slicer value wins. This means bridge and overhang cooling always works.

5. **Lookahead**: Uses the same 5-second lookahead as temperature control to pre-adjust the fan before high-flow sections arrive.

6. **Material awareness**: Each material profile defines its own fan range (`sc_min_fan` to `sc_max_fan`). PLA gets 50-100%, ABS gets 0-40%, etc.

7. **Heater-adaptive feedback**: When the heater struggles to reach target temperature (>85-90% duty cycle), SC automatically reduces fan speed to help the heater. This prevents high-power CPAP fans from overwhelming the heater at high temps.

## Configuration

Smart Cooling is enabled by default. To customize, edit `auto_flow.cfg`:

```ini
# Enable/disable Smart Cooling
variable_sc_enable: True

# Base fan speed (0-255). If 0, uses current slicer setting as base
variable_sc_base_fan: 0

# Flow threshold where cooling reduction starts (mm³/s)
variable_sc_flow_gate: 8.0

# Fan reduction per mm³/s above flow_gate (0-1 scale)
# E.g., 0.03 = 3% reduction per mm³/s extra flow
variable_sc_flow_k: 0.03

# Layer time threshold - layers faster than this get extra cooling
variable_sc_short_layer_time: 15.0

# Extra fan % per second below threshold (0-1 scale)
variable_sc_layer_time_k: 0.02

# Min/max fan limits (0.0-1.0 = 0-100%)
variable_sc_min_fan: 0.20
variable_sc_max_fan: 1.00

# First layer fan (usually 0 for bed adhesion)
variable_sc_first_layer_fan: 0.0

# Heater-adaptive fan control (enabled by default)
variable_sc_heater_adaptive: True

# Heater wattage profile (NEW - recommended)
variable_sc_heater_wattage: 40  # Set to 40 for 40W heater, 60 for 60W heater, 0 for manual

# Manual settings (only used when sc_heater_wattage = 0)
variable_sc_heater_duty_threshold: 0.90  # Start reducing fan at 90% duty
variable_sc_heater_duty_k: 1.0           # 1.0 = match duty excess 1:1
```

### Parameter Guide

| Parameter | Description | Default |
|-----------|-------------|---------|
| `sc_enable` | Master enable/disable | True |
| `sc_base_fan` | Base fan speed (0-255). 0 = use slicer's M106 value | 0 |
| `sc_flow_gate` | Flow rate (mm³/s) where reduction starts | 8.0 |
| `sc_flow_k` | Fan reduction per mm³/s above gate (0.03 = 3%) | 0.03 |
| `sc_short_layer_time` | Layers faster than this get boosted cooling (seconds) | 15.0 |
| `sc_layer_time_k` | Extra fan per second below threshold (0.02 = 2%) | 0.02 |
| `sc_min_fan` | Minimum fan speed (0.0-1.0) | 0.20 |
| `sc_max_fan` | Maximum fan speed (0.0-1.0) | 1.00 |
| `sc_first_layer_fan` | First layer fan override (0.0-1.0) | 0.0 |
| `sc_heater_adaptive` | Enable heater-adaptive fan reduction | True |
| `sc_heater_wattage` | Heater profile: 40 (40W), 60 (60W), 0 (manual) | 0 |
| `sc_heater_duty_threshold` | Heater duty % where fan reduction starts (manual mode) | 0.90 |
| `sc_heater_duty_k` | Fan reduction multiplier (manual mode) | 1.0 |

## Material Profile Overrides

Each material in `material_profiles.cfg` has its own cooling settings that override the defaults:

```ini
[gcode_macro _AF_PROFILE_PLA]
# ... temperature settings ...
# Smart Cooling: PLA likes high cooling with heater-adaptive feedback
variable_sc_flow_gate: 8.0
variable_sc_flow_k: 0.02
variable_sc_min_fan: 0.20
variable_sc_max_fan: 1.00
```

### Default Material Settings

| Material | Min Fan | Max Fan | Flow Gate | Notes |
|----------|---------|---------|-----------|-------|
| PLA | 20% | 100% | 8.0 | Heater-adaptive feedback enabled for CPAP compatibility |
| PETG | 30% | 70% | 10.0 | Too much causes layer adhesion issues |
| ABS | 0% | 40% | 15.0 | Minimal cooling to prevent warping |
| ASA | 0% | 40% | 15.0 | Same as ABS |
| TPU | 10% | 50% | 5.0 | Low cooling for layer adhesion |
| Nylon | 10% | 50% | 10.0 | Warps easily with too much cooling |
| PC | 0% | 30% | 15.0 | Very low cooling, high temps |
| HIPS | 0% | 40% | 12.0 | Like ABS |

## Monitoring

Check Smart Cooling status during a print:
```
AT_SC_STATUS
```

> See [COMMANDS.md](COMMANDS.md#at_sc_status) for full command documentation.

Output:
```
===== SMART COOLING STATUS =====
Smart Cooling: ENABLED (Override Mode)
Current Fan: 65%
Slicer Request: 0% (bridges/overhangs)
Layer Time: 12.3s
Fan Range: 30% - 70%
Flow Gate: 10.0 mm³/s
Short Layer Threshold: 12.0s
Heater Duty: 72%
=================================
```

## Slicer Integration

### OrcaSlicer / PrusaSlicer / SuperSlicer

Smart Cooling takes full control of the fan. Set your slicer's fan speeds to **0%** and let SC handle everything. Bridge and overhang fan commands from the slicer are still respected — SC intercepts `M106` and allows the slicer to **boost** fan speed above SC's calculated value.

#### Filament Cooling Settings

| Setting | Change To | Why |
|---------|-----------|-----|
| **Min fan speed** | **0%** | SC controls fan speed |
| **Max fan speed** | **0%** | SC controls fan speed |
| **First layer fan** | **0%** | SC handles first layer |
| **Keep fan always on** | ☑️ **ON** | Allows SC to set fan at any time |
| **Full fan speed at layer** | **1** | SC takes over immediately |
| **Slow printing down for better layer cooling** | ☐ **OFF** | SC boosts fan instead of slowing |

#### Keep These Bridge/Overhang Settings

| Setting | Value | Why |
|---------|-------|-----|
| **Force cooling for overhangs and bridges** | ☑️ **ON** | SC doesn't detect geometry |
| **Bridge fan speed** | **100%** | SC will let this through |
| **Overhang fan speed** | **100%** (or your preference) | SC will let this through |

> **How it works**: SC intercepts all `M106` commands. When the slicer sends a high fan value for bridges/overhangs, SC sees it and uses `max(SC_calculated, slicer_value)`. So the bridge fan always wins if it’s higher than SC's current target.

#### Settings That No Longer Matter (SC overrides these)
- Layer time thresholds
- Fan speed curves
- Min/Max fan speed ramps
- Auxiliary part cooling fan is separate (not controlled by SC)
- Ironing fan speed (SC doesn't run during ironing)

## How the Algorithm Works

Every 1 second, Smart Cooling calculates the optimal fan speed:

```
1. Start from material ceiling: base = sc_max_fan
2. Get effective flow = max(current_flow, predicted_flow_5s_ahead)
3. Calculate flow reduction = (effective_flow - flow_gate) * flow_k
4. Calculate layer boost = (short_layer_time - actual_layer_time) * layer_time_k
5. Calculate heater reduction = (heater_duty - duty_threshold) * duty_k  [if heater_adaptive enabled]
6. sc_target = base - flow_reduction + layer_boost - heater_reduction
7. Clamp to [sc_min_fan, sc_max_fan]
8. final_fan = max(sc_target, slicer_fan)  [slicer can boost for bridges/overhangs]
9. Apply if changed by more than 1%
```

### Heater-Adaptive Feedback

When `sc_heater_adaptive` is enabled (default), Smart Cooling monitors the heater's duty cycle and automatically reduces fan speed if the heater is struggling.

#### Heater Wattage Profiles (Recommended - NEW)

The easiest way to configure heater-adaptive settings is using the `sc_heater_wattage` profile:

**40W Heater Profile** (for CPAP fans):
```ini
variable_sc_heater_wattage: 40
```
- Threshold: 85% duty (starts reducing earlier)
- Multiplier: 2.0 (more aggressive reduction)
- Perfect for: Revo HF 40W + CPAP fans at high speeds
- At 95% duty: (0.95-0.85)×2.0 = 20% fan reduction

**60W Heater Profile** (standard):
```ini
variable_sc_heater_wattage: 60
```
- Threshold: 90% duty (standard)
- Multiplier: 1.0 (balanced reduction)
- Perfect for: Revo 60W + standard fans
- At 95% duty: (0.95-0.90)×1.0 = 5% fan reduction

**Manual/Custom** (advanced users):
```ini
variable_sc_heater_wattage: 0
variable_sc_heater_duty_threshold: 0.90
variable_sc_heater_duty_k: 1.0
```
- Use when you want full control over settings
- Profile is ignored, uses manual values below it

#### How It Works

This feedback loop helps the heater reach target temperature even with high-power CPAP fans:
1. CPAP fan runs at 70% → heater struggles at 95% duty
2. Smart Cooling detects high duty → reduces fan (5-20% depending on profile)
3. Lower fan speed → heater reaches target → duty drops
4. Once duty drops below threshold → fan returns to normal

> **💡 TIP for CPAP users:** Simply set `sc_heater_wattage: 40` and you're done! No need to manually tune threshold and multiplier values.

### Example Calculation

Settings: sc_max_fan=70%, sc_min_fan=30%, flow_gate=10, flow_k=0.02 (PETG profile)

| Current Flow | Predicted Flow | Layer Time | SC Calculation | Slicer | Result |
|--------------|----------------|------------|----------------|--------|---------|
| 5 mm³/s | 5 mm³/s | 20s | 70% - 0% + 0% = 70% | 0% | **70%** |
| 12 mm³/s | 15 mm³/s | 20s | 70% - (15-10)×2% = 60% | 0% | **60%** |
| 18 mm³/s | 18 mm³/s | 20s | 70% - (18-10)×2% = 54% | 0% | **54%** |
| 6 mm³/s | 6 mm³/s | 8s | 70% - 0% + (12-8)×1% = 74% → clamped | 0% | **70%** |
| 12 mm³/s | 12 mm³/s | 10s | 70% - 4% + 2% = 68% | 0% | **68%** |
| 5 mm³/s | 5 mm³/s | 20s | 70% - 0% + 0% = 70% | 100% (bridge) | **100%** |

## Tuning Tips

### For More Aggressive Cooling Reduction
```ini
variable_sc_flow_k: 0.05           # 5% reduction per mm³/s (was 3%)
variable_sc_flow_gate: 6.0         # Start reducing at lower flow
```

### For More Layer Time Sensitivity
```ini
variable_sc_short_layer_time: 20.0  # Consider layers under 20s as "short"
variable_sc_layer_time_k: 0.03      # 3% boost per second (was 2%)
```

### For Tighter Control Range
```ini
variable_sc_min_fan: 0.40          # Never below 40%
variable_sc_max_fan: 0.80          # Never above 80%
```

### For Heater-Adaptive Control

**Easiest: Use heater wattage profile** (recommended):
```ini
# For 40W heater + CPAP (aggressive)
variable_sc_heater_wattage: 40

# For 60W heater + standard fan (balanced)
variable_sc_heater_wattage: 60
```

**Advanced: Manual tuning** (if profiles don't work for you):
```ini
# Manual control - set profile to 0
variable_sc_heater_wattage: 0
variable_sc_heater_duty_threshold: 0.85  # Start reducing at 85% duty
variable_sc_heater_duty_k: 2.0           # Double the duty excess (at 95% duty: 20% reduction)
```

**To disable heater-adaptive control**:
```ini
# Or disable if you don't have high-power fans
variable_sc_heater_adaptive: False
```

## Disabling Smart Cooling

To disable Smart Cooling while keeping other Adaptive Flow features:

```ini
variable_sc_enable: False
```

When SC is disabled, all `M106`/`M107` commands pass through directly to the fan hardware, giving the slicer full control.
