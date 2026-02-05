# Smart Cooling

Smart Cooling automatically adjusts the part cooling fan based on flow rate, layer time, and heater performance, optimizing print quality without manual fan speed management.

## How It Works

1. **Flow-based reduction**: At high flow rates, the fast-moving plastic creates its own airflow and needs less fan cooling. Smart Cooling reduces fan speed proportionally.

2. **Layer time boost**: Short layers (fast prints or small features) don't have enough time to cool between layers. Smart Cooling increases fan speed for these quick layers.

3. **Lookahead**: Uses the same 5-second lookahead as temperature control to pre-adjust the fan before high-flow sections arrive.

4. **Material awareness**: Each material profile has its own cooling preferences (PLA wants high cooling, ABS wants minimal).

5. **Heater-adaptive feedback** (NEW): When the heater struggles to reach target temperature (>90% duty cycle), Smart Cooling automatically reduces fan speed to help the heater. This prevents high-power CPAP fans from overwhelming the heater at high temps.

## Configuration

Smart Cooling is enabled by default. To customize, edit `auto_flow_user.cfg`:

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
Smart Cooling: ENABLED
Current Fan: 65%
Layer Time: 12.3s
Fan Range: 30% - 70%
Flow Gate: 10.0 mm³/s
Short Layer Threshold: 15.0s
=================================
```

## Slicer Integration

### OrcaSlicer / PrusaSlicer / SuperSlicer

With Smart Cooling enabled, simplify your filament cooling settings:

#### Keep These Settings
| Setting | Value | Why |
|---------|-------|-----|
| No cooling for the first | **1 layer** | SC also skips layer 1 |
| Keep fan always on | ☑️ **ON** | Let SC have control |
| Force cooling for overhangs and bridges | ☑️ **ON** | SC doesn't detect overhangs |
| Overhangs/bridges fan speed | **100%** | SC won't override bridge moves |

#### Change These Settings
| Setting | Change To | Why |
|---------|-----------|-----|
| Min fan speed threshold | **100%** | Let SC handle reduction |
| Max fan speed threshold | **100%** | Same as min (constant base) |
| Full fan speed at layer | **2** | SC takes over after layer 1 |
| Slow printing down for better layer cooling | ☐ **OFF** | SC boosts fan instead of slowing |

#### Doesn't Matter (SC overrides these)
- Layer time thresholds
- Fan speed curves

#### Don't Change
- Auxiliary part cooling fan (separate fan, not controlled by SC)
- Ironing fan speed (SC doesn't run during ironing)

### Option 1: Let Smart Cooling Handle Everything (Recommended)

Set a **single constant fan speed** in your slicer as the baseline:

| Setting | Value |
|---------|-------|
| **Fan speed** | 100% (for PLA) or your material's max |
| **First layer fan** | 0% |
| **Disable fan for first N layers** | 1 |
| **Enable cooling for bridges** | Can leave ON |

Smart Cooling uses your slicer's fan speed as the "base" and adjusts from there.

### Option 2: Define Base in Config

In OrcaSlicer/PrusaSlicer, set fan speed to 0%, then define the baseline in `auto_flow_user.cfg`:

```ini
variable_sc_base_fan: 255   # 255 = 100% as base
```

### What to Keep in Slicer

| Keep This | Why |
|-----------|-----|
| First layer fan = 0% | Redundant but harmless (SC also does this) |
| Bridge fan speed | SC doesn't detect bridges (slicer is better) |
| Overhang fan speed | SC doesn't detect overhangs (slicer is better) |

### What Becomes Redundant

| Slicer Setting | Smart Cooling Equivalent |
|----------------|-------------------------|
| Fan speed based on layer time | `sc_short_layer_time` + `sc_layer_time_k` |
| Min/Max fan speed | `sc_min_fan` / `sc_max_fan` (in material profile) |
| Slow down if layer print time is below X | SC handles this with increased cooling instead |

## How the Algorithm Works

Every 1 second, Smart Cooling calculates the optimal fan speed:

```
1. Get base fan (from config or slicer's current setting)
2. Get effective flow = max(current_flow, predicted_flow_5s_ahead)
3. Calculate flow reduction = (effective_flow - flow_gate) * flow_k
4. Calculate layer boost = (short_layer_time - actual_layer_time) * layer_time_k
5. Calculate heater reduction = (heater_duty - duty_threshold) * duty_k  [if heater_adaptive enabled]
6. target_fan = base_fan - flow_reduction + layer_boost - heater_reduction
7. Clamp to [min_fan, max_fan]
8. Apply if changed by more than 3%
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

Settings: base_fan=100%, flow_gate=8, flow_k=0.03, min=30%, max=70% (PETG)

| Current Flow | Predicted Flow | Layer Time | Calculation | Result |
|--------------|----------------|------------|-------------|--------|
| 5 mm³/s | 5 mm³/s | 20s | 100% - 0% + 0% = 100% → clamped | **70%** |
| 12 mm³/s | 15 mm³/s | 20s | 100% - (15-8)×3% = 79% → clamped | **70%** |
| 12 mm³/s | 12 mm³/s | 8s | 100% - 12% + 14% = 102% → clamped | **70%** |
| 6 mm³/s | 6 mm³/s | 8s | 100% - 0% + 14% = 114% → clamped | **70%** |
| 6 mm³/s | 6 mm³/s | 20s | 100% - 0% + 0% = 100% → clamped | **70%** |

(In this PETG example, the 70% max cap is often the limiting factor)

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

## Integration with Heater Capacity Management

Smart Cooling works seamlessly with the **Heater Capacity Management** system (NEW feature):

### How They Work Together

1. **Smart Cooling's heater-adaptive feedback**: Reduces fan when heater duty exceeds threshold (typically 85-90%)
2. **Heater Capacity Management**: Also reduces fan **and** limits temperature demand when heater struggles

Both systems work together to maintain temperature stability:

- **Normal operation**: Only Smart Cooling adjusts fan based on duty cycle
- **Extreme conditions**: Heater Capacity Management activates flow limiting and adds additional fan reduction
- **Result**: Maximum stability with dual-layer protection

### Example Scenario

**Without Heater Capacity Management:**
```
Flow: 19 mm³/s → Boost: +19°C → Target: 229°C
Heater: 95% duty, struggling
Smart Cooling reduces fan: 70% → 64% (helps, but not enough)
Result: Temperature still unstable
```

**With Heater Capacity Management:**
```
Flow: 19 mm³/s actual
Heater Capacity Management detects struggle
Effective flow limited: 11 mm³/s
Boost reduced: +11°C → Target: 221°C
Smart Cooling reduces fan: 70% → 64%
Heater Capacity adds: -10% fan → Final: 54%
Result: Heater duty drops to 88%, stable temperature
```

### Configuration

The two systems use separate settings:

**Smart Cooling heater-adaptive (basic duty-based reduction):**
```ini
variable_sc_heater_adaptive: True
variable_sc_heater_wattage: 40  # or 60
```

**Heater Capacity Management (flow limiting + fan reduction):**
```ini
variable_heater_adaptive_flow: True
variable_heater_wattage: 40  # or 60
```

> **Note:** Both use the same `heater_wattage` value. Set it once and both systems adapt accordingly.

### When to Use Each

**Smart Cooling heater-adaptive alone:**
- Mild heater saturation (90-95% duty)
- Standard flow rates (<15 mm³/s)
- Want basic fan reduction only

**Heater Capacity Management (includes Smart Cooling benefits):**
- Severe heater saturation (>95% duty)
- Extreme flow rates (15-20+ mm³/s)
- CPAP fans overwhelming heater
- Want both flow limiting and fan reduction

**Recommendation:** Enable both (default) for maximum stability.

### Monitoring

Check combined status with `AT_STATUS`:

```
╠═══════════════════════════════════════════╣
║ HEATER CAPACITY                           ║
╠═══════════════════════════════════════════╣
║ Status:     ⚠ LIMITED (88% duty)       ║
║ Sustainable:  11.2 mm³/s (calc)             ║
║ Profile:     40W heater                      ║
```

**[Full Heater Capacity Management documentation →](HEATER_CAPACITY.md)**

## Disabling Smart Cooling

To disable Smart Cooling while keeping other Adaptive Flow features:

```ini
variable_sc_enable: False
```

Or for a specific print, set your slicer's fan speed and Smart Cooling won't override it (since `sc_enable: False` in config).
