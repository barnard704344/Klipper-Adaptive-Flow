# Heater Capacity Management

Heater Capacity Management is a **flow-adaptive heater protection system** that dynamically limits temperature boost requests when the heater cannot sustain the demanded temperature. This prevents temperature instability at extreme flow rates, especially with high-power cooling fans.

## The Problem

When using a 40W heater cartridge with high-power CPAP fans at extreme flow rates (19+ mm³/s), the system can request temperatures that exceed the heater's physical capacity:

1. **Heater saturation** - Duty cycle stays at >95%, unable to reach target
2. **Temperature instability** - Fan oscillates up/down, fighting the heater
3. **Failed prints** - Temperature cannot stabilize at requested boost levels

### Example Without Heater Capacity Management

```
Flow: 19 mm³/s → Temp boost: +19°C → Target: 229°C
Heater: 95% duty, struggling
Fan: 70% → Smart Cooling reduces to 64% → heater still can't keep up
Result: Oscillation and instability
```

## The Solution

Heater Capacity Management automatically finds equilibrium between:

- **Heater duty cycle** (can it keep up?)
- **Fan speed** (cooling fighting heater)
- **Volumetric flow** (temperature demand)

When the heater struggles (duty cycle >85% or temperature deficit >8°C), the system:

1. Calculates the **maximum sustainable flow rate** based on heater wattage, base temperature, and fan speed
2. Caps the **effective flow** used for temperature calculations
3. Reduces **fan speed** to help the heater
4. Logs and displays the limiting status

### Example With Heater Capacity Management

```
Flow: 19 mm³/s actual
Heater struggling: 95% duty → System detects capacity limit
Max sustainable flow calculated: ~11 mm³/s (for 40W + 70% fan)
Effective flow capped: 11 mm³/s
New temp boost: +11°C → Target: 221°C
Fan also reduced: 70% → 64%
Result: Heater duty drops to 88%, temperature stable
```

## Configuration

Heater Capacity Management is **enabled by default** with conservative settings. To customize, edit `auto_flow_user.cfg`:

### Basic Configuration

```ini
[gcode_macro _AUTO_TEMP_CORE]
# Enable/disable heater capacity management
variable_heater_adaptive_flow: True

# Heater wattage profile (40, 60, or 0 for manual)
variable_heater_wattage: 40

# Manual sustainable flow limit (only used if heater_wattage = 0)
# Set to 0 for automatic calculation
variable_heater_max_sustainable_flow: 0
```

### Advanced Tuning

```ini
# Duty cycle threshold where flow limiting activates
variable_heater_flow_limit_duty: 0.85  # Start at 85% duty

# Flow backoff aggressiveness (0.5-2.0)
# Higher = more aggressive flow reduction when heater struggles
variable_heater_flow_backoff_k: 1.5  # 1.5 = moderate (recommended)

# Temperature deficit threshold (additional trigger)
# If actual temp is this many °C behind target, flow limiting activates
variable_heater_temp_deficit_threshold: 8.0
```

## Heater Wattage Profiles

### 40W Heater (Lower-Power)
```ini
variable_heater_wattage: 40
```

**Characteristics:**
- Base sustainable flow: 16 mm³/s (no fan, low temp)
- More aggressive limiting to prevent saturation
- Optimized for CPAP fans
- Works well up to ~12 mm³/s sustained flow with fans

**When to use:**
- Standard E3D 40W heater cartridge
- High-power cooling fans (CPAP)
- Printing above 220°C with PLA or PETG

### 60W Heater (Standard)
```ini
variable_heater_wattage: 60
```

**Characteristics:**
- Base sustainable flow: 20 mm³/s (no fan, low temp)
- Less aggressive limiting (more headroom)
- Handles higher sustained flow rates
- Works well up to ~16 mm³/s sustained flow with fans

**When to use:**
- E3D 60W heater cartridge
- Standard part cooling fans
- High-temperature materials (ABS, ASA, Nylon)

### Manual Mode
```ini
variable_heater_wattage: 0
variable_heater_max_sustainable_flow: 15.0  # Set your limit in mm³/s
```

Use manual mode for:
- Non-standard heater cartridges
- Fine-tuning for specific setups
- Testing and experimentation

## How It Works

### 1. Sustainable Flow Calculation

The system calculates the maximum sustainable flow rate based on:

```
Base sustainable flow = Profile-specific (40W → 16 mm³/s, 60W → 20 mm³/s)
Temperature factor = 1.0 - ((base_temp - 200) / 150)
Fan factor = 1.0 - (current_fan × 0.3)
Max sustainable flow = Base × Temperature factor × Fan factor
```

**Example:** 40W heater, 220°C base temp, 70% fan
```
Base = 16 mm³/s
Temp factor = 1.0 - ((220 - 200) / 150) = 0.867
Fan factor = 1.0 - (0.7 × 0.3) = 0.79
Max sustainable = 16 × 0.867 × 0.79 = 10.9 mm³/s
```

### 2. Heater Struggling Detection

The system activates flow limiting when **either** condition is met:

- **High duty cycle:** Heater PWM > `heater_flow_limit_duty` (default 85%)
- **Temperature deficit:** Actual temp is more than `heater_temp_deficit_threshold` (default 8°C) below target

### 3. Backoff Calculation

When limiting activates:

```
Duty excess = max(0, heater_duty - heater_flow_limit_duty)
Temp deficit factor = max(0, temp_deficit / heater_temp_deficit_threshold)
Backoff = min(1.0, (duty_excess × heater_flow_backoff_k) + (temp_deficit_factor × 0.5))
```

### 4. Flow Limiting

```
Effective flow = min(actual_flow, max_sustainable_flow × (1.0 - backoff))
Fan reduction = backoff × 0.4  # Up to 40% fan reduction
```

The **effective flow** is used for temperature boost calculations instead of actual flow.

### 5. Smart Cooling Integration

The calculated **fan reduction** is applied to Smart Cooling's fan control, reducing part cooling fan speed to help the heater reach target temperature.

## Status Display

Use the `AT_STATUS` command to check heater capacity status:

### Normal Operation
```
╠═══════════════════════════════════════════╣
║ HEATER CAPACITY                           ║
╠═══════════════════════════════════════════╣
║ Status:     ✓ OK                           ║
║ Sustainable:  11.2 mm³/s (calc)             ║
║ Profile:     40W heater                      ║
```

### Limited Operation
```
╠═══════════════════════════════════════════╣
║ HEATER CAPACITY                           ║
╠═══════════════════════════════════════════╣
║ Status:     ⚠ LIMITED (88% duty)       ║
║ Sustainable:  11.2 mm³/s (calc)             ║
║ Profile:     40W heater                      ║
```

Flow status also shows effective vs actual flow:
```
╠═══════════════════════════════════════════╣
║ FLOW & SPEED                              ║
╠═══════════════════════════════════════════╣
║ Actual Flow:  19.00 mm³/s               ║
║ Effective:    11.20 mm³/s (LIMITED)      ║
```

## Logging

All heater capacity management events are logged to CSV files in `~/printer_data/logs/adaptive_flow/`:

### New Log Columns

- **effective_flow** - Flow value used for temperature calculation (mm³/s)
- **flow_limited** - Boolean (0/1), was limiting active?
- **backoff_pct** - Percentage reduction applied (0-100)
- **sustainable_flow** - Calculated sustainable flow limit (mm³/s)

### Example Log Data

```csv
elapsed_s,temp_actual,temp_target,boost,flow,effective_flow,flow_limited,backoff_pct,sustainable_flow
120.5,228.3,229.0,19.0,19.23,11.18,1,42,11.47
```

Use these logs to:
- Analyze heater performance
- Tune sustainable flow limits
- Diagnose print issues
- Optimize material profiles

## Tuning Guidelines

### When to Adjust Settings

**Increase `heater_flow_limit_duty` (0.85 → 0.90):**
- You rarely see flow limiting activate
- Heater has more capacity than expected
- Using higher-wattage heater

**Decrease `heater_flow_limit_duty` (0.85 → 0.80):**
- Heater often runs at 95%+ duty
- Temperature instability even with limiting
- Using lower-wattage heater

**Increase `heater_flow_backoff_k` (1.5 → 2.0):**
- More aggressive flow limiting needed
- Temperature still unstable during limiting
- Need faster response to heater saturation

**Decrease `heater_flow_backoff_k` (1.5 → 1.0):**
- Limiting is too aggressive
- Temperature drops unnecessarily
- Want gentler response

### Material-Specific Considerations

**High-temperature materials (ABS, ASA, Nylon, PC):**
- Heater has less headroom at 240-280°C
- Sustainable flow rates are lower
- Consider increasing base temperature instead of relying on high boost

**Low-temperature materials (PLA, PETG):**
- Heater has more headroom at 200-230°C
- Sustainable flow rates are higher
- Flow limiting less likely to activate

**High-flow prints:**
- If frequently hitting flow limits, consider:
  - Upgrading to 60W heater
  - Reducing print speed
  - Increasing nozzle size
  - Lowering acceleration

## Troubleshooting

### Flow limiting activates too often

**Possible causes:**
- Heater wattage profile too conservative
- Fan speed too high
- Base temperature too high
- Flow rates exceed hotend capacity

**Solutions:**
1. Check heater wattage setting matches your hardware
2. Reduce fan speed in material profile
3. Lower base print temperature
4. Reduce print speed or increase nozzle size

### Flow limiting not activating when needed

**Possible causes:**
- Duty threshold too high
- Temperature deficit threshold too high
- Heater wattage profile not configured

**Solutions:**
1. Lower `heater_flow_limit_duty` to 0.80
2. Lower `heater_temp_deficit_threshold` to 5.0
3. Set correct `heater_wattage` value

### Temperature still unstable with limiting

**Possible causes:**
- Backoff too gentle
- Fan reduction insufficient
- Heater or thermistor issue

**Solutions:**
1. Increase `heater_flow_backoff_k` to 2.0
2. Check heater wiring and cartridge
3. Verify thermistor is properly seated
4. Consider Smart Cooling tuning

### Unwanted temperature drops

**Possible causes:**
- Limiting too aggressive
- False positives triggering limiting

**Solutions:**
1. Decrease `heater_flow_backoff_k` to 1.0
2. Increase `heater_flow_limit_duty` to 0.90
3. Check logs for unnecessary limiting events

## Integration with Other Features

### Smart Cooling
Heater Capacity Management works alongside Smart Cooling's heater-adaptive feedback:
- Smart Cooling reduces fan based on duty cycle
- Heater Capacity Management reduces fan **and** temperature demand
- Both systems work together for maximum stability

### Dynamic Pressure Advance
- Flow limiting affects the flow value used for PA scaling
- Effective flow (not actual flow) determines PA adjustments
- Prevents excessive PA reduction during limiting

### Dynamic Z-Window (DynZ)
- Flow limiting is independent of DynZ stress detection
- Both can be active simultaneously
- DynZ handles geometry stress, Heater Capacity handles thermal limits

## When to Disable

Disable Heater Capacity Management if:
- You have a high-wattage heater (80W+)
- You use low fan speeds (<50%)
- You print at low flow rates (<10 mm³/s)
- You want maximum responsiveness (accepting instability risk)

To disable:
```ini
variable_heater_adaptive_flow: False
```

## Summary

Heater Capacity Management is a **safety and stability feature** that prevents temperature instability when the heater cannot keep up with requested boost levels. It's enabled by default with sensible settings and requires minimal tuning for most users.

**Key benefits:**
- Prevents heater saturation and temperature oscillation
- Automatically adapts to heater capacity and conditions
- Works seamlessly with existing Smart Cooling
- Provides detailed logging for analysis
- No breaking changes to existing functionality

For most users, simply setting the correct `heater_wattage` value (40 or 60) is sufficient.
