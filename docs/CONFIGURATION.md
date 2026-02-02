# Configuration Reference

Detailed configuration options for Klipper Adaptive Flow.

## Quick Start

Most users only need to set one option in `auto_flow.cfg`:

```ini
variable_use_high_flow_nozzle: True   # False for standard Revo nozzles
```

Everything else auto-configures based on your material.

---

## How It Works

### Temperature Control
- **Flow boost**: Temperature increases with volumetric flow (mm³/s)
- **Speed boost**: Extra heating for high-speed thin walls (>100mm/s)
- **Acceleration boost**: Detects flow changes via motion analysis
- **Lookahead**: 5-second prediction buffer for pre-heating

### Dynamic Pressure Advance
PA automatically scales with temperature boost:
```
PA_adjusted = PA_base - (boost × pa_boost_k)
```

Example: Base PA 0.060, +20°C boost, pa_boost_k 0.001 → PA becomes 0.040

Higher temperature = lower filament viscosity = less PA needed.

---

## Material Profiles

### User-Editable Profiles

Material profiles are defined in `material_profiles.cfg`. Edit this file to customize boost curves for your filaments.

Each profile is a Klipper macro:
```ini
[gcode_macro _AF_PROFILE_PETG]
variable_flow_k: 1.20           # Temp boost per mm³/s of flow
variable_speed_boost_k: 0.08    # Temp boost per mm/s above 100mm/s
variable_max_boost: 40.0        # Maximum temp boost cap (°C)
variable_max_temp: 280          # Absolute temp safety limit
variable_flow_gate: 14.0        # Flow threshold for HF nozzle (mm³/s)
variable_flow_gate_std: 10.0    # Flow threshold for std nozzle (mm³/s)
variable_pa_boost_k: 0.0012     # PA reduction per °C of boost
variable_ramp_rise: 4.0         # Heat up rate (°C/s)
variable_ramp_fall: 1.5         # Cool down rate (°C/s)
variable_default_pa: 0.060      # Default PA if not calibrated
gcode:
```

### Default Profiles

| Material | Flow K | Speed K | Max Boost | Max Temp | Ramp ↑/↓ | Default PA |
|----------|--------|---------|-----------|----------|----------|------------|
| **PLA** | 1.00 | 0.08 | 30°C | 245°C | 5.0/2.5 | 0.035 |
| **PETG** | 1.40 | 0.08 | 40°C | 280°C | 4.0/1.5 | 0.060 |
| **ABS** | 0.80 | 0.10 | 50°C | 290°C | 5.0/3.0 | 0.050 |
| **ASA** | 0.80 | 0.10 | 50°C | 295°C | 5.0/3.0 | 0.050 |
| **TPU** | 0.20 | 0.02 | 15°C | 240°C | 1.5/0.5 | 0.200 |
| **Nylon** | 0.90 | 0.07 | 35°C | 275°C | 3.0/1.5 | 0.055 |
| **PC** | 0.70 | 0.10 | 50°C | 310°C | 5.0/2.5 | 0.045 |

> **Note:** The PLA profile is tuned for high-flow variants (PLA+, PLA HF) commonly used with Revo HF nozzles. At 18mm³/s with a 215°C base temp, the system will boost to ~233°C. See recommended base temps below.

### Recommended PLA Base Temperatures

| Flow Rate | Slicer Temp | Why |
|-----------|-------------|-----|
| Low (<10mm³/s) | 205-210°C | Standard printing |
| Medium (10-15mm³/s) | 215-220°C | Fast perimeters/infill |
| High (15-20mm³/s) | 220-225°C | Speed printing with HF filament |

### What Each Parameter Does

| Parameter | Effect |
|-----------|--------|
| `flow_k` | °C boost per mm³/s of volumetric flow above gate |
| `speed_boost_k` | °C boost per mm/s of linear speed above 100mm/s |
| `max_boost` | Hard cap on total temperature boost |
| `max_temp` | Absolute max temperature (safety limit) |
| `flow_gate` | Minimum flow to trigger boost (HF nozzle) |
| `flow_gate_std` | Minimum flow to trigger boost (standard nozzle) |
| `pa_boost_k` | PA reduction per °C of boost |
| `ramp_rise` | How fast temp can increase (°C/s) |
| `ramp_fall` | How fast temp can decrease (°C/s) |
| `default_pa` | PA value if user hasn't calibrated |

### Adding Custom Materials

1. Copy any profile in `material_profiles.cfg`
2. Rename to `[gcode_macro _AF_PROFILE_YOURMATERIAL]`
3. Adjust the values
4. Use: `AT_START MATERIAL=YOURMATERIAL`

---

## Advanced Configuration

Edit these variables in `auto_flow.cfg` if needed:

### Global Limits

```ini
variable_max_boost_limit: 50.0        # Global max boost (°C) - overridden by material
variable_ramp_rate_rise: 4.0          # Default heat up speed (°C/s)
variable_ramp_rate_fall: 1.0          # Default cool down speed (°C/s)
```

### Speed Boost

For high-speed thin walls that don't trigger flow-based boost:

```ini
variable_speed_boost_threshold: 100.0  # Linear speed (mm/s) to trigger boost
variable_speed_boost_k: 0.08           # Default °C per mm/s above threshold
```

Example at 300mm/s: `(300-100) × 0.08 = +16°C` boost

### Flow Smoothing

```ini
variable_flow_smoothing: 0.15          # 0.0-1.0, lower = faster response
```

### First Layer Mode

```ini
variable_first_layer_skip: True        # Disable boost on first layer
variable_first_layer_height: 0.3       # Z height considered "first layer"
```

### Thermal Safety

```ini
variable_thermal_runaway_threshold: 15.0   # Max overshoot before emergency
variable_thermal_undertemp_threshold: 10.0 # Max undershoot before warning
```

### Multi-Object Temperature Management

Prevents thermal runaway when printing multiple objects sequentially:

```ini
variable_multi_object_temp_wait: True      # Enable automatic temp stabilization
variable_temp_wait_tolerance: 5.0          # Temperature tolerance (°C)
```

**How it works:**
- When starting a new object, checks if current temperature differs from target by more than tolerance
- If yes, pauses and waits for temperature to stabilize within tolerance range
- Prevents thermal runaway shutdowns when previous object ended at higher temperature
- Works automatically with EXCLUDE_OBJECT (OrcaSlicer, PrusaSlicer) and M486 (legacy)
- Waits indefinitely until temperature stabilizes (safer than timing out)

**Example scenario:**
1. Object 1 finishes at 253°C (boosted from 220°C base)
2. Object 2 starts with 220°C target
3. System detects 33°C difference (> 5°C tolerance)
4. Pauses and waits for cooldown to 215-225°C range (220°C ± 5°C)
5. Continues printing once temperature stabilizes

---

## Commands Reference

### Core Commands

| Command | Description |
|---------|-------------|
| `AT_START MATERIAL=X` | Enable adaptive flow with material profile |
| `AT_END` | Stop adaptive flow loop |
| `AT_STATUS` | Show current state, flow, boost, PA, PWM |

### PA Commands

| Command | Description |
|---------|-------------|
| `AT_SET_PA MATERIAL=X PA=Y` | Save calibrated PA for a material |
| `AT_GET_PA MATERIAL=X` | Show PA for a material |
| `AT_LIST_PA` | List all PA values |

### Manual Override

| Command | Description |
|---------|-------------|
| `AT_SET_FLOW_K K=X` | Set flow boost multiplier |
| `AT_SET_FLOW_GATE GATE=X` | Set minimum flow threshold |
| `AT_SET_MAX MAX=X` | Set max temperature limit |
| `AT_ENABLE` | Enable the system |
| `AT_DISABLE` | Disable the system |

---

## Slicer Setup

### Material Parameter (Optional)

Pass the material from your slicer for accurate profile selection. If omitted, the system auto-detects from extruder temperature.

**OrcaSlicer / PrusaSlicer / SuperSlicer:**
```gcode
PRINT_START ... MATERIAL={filament_type[0]}
```

**Cura:**
```gcode
PRINT_START ... MATERIAL={material_type}
```

### Disable Slicer Pressure Advance

**Important:** This system handles Pressure Advance dynamically. Disable PA in your slicer to avoid conflicts:

| Slicer | Setting |
|--------|---------|
| OrcaSlicer | Printer Settings → Advanced → Enable pressure advance = OFF |
| PrusaSlicer | Not applicable (PA is in firmware) |
| Cura | Disable any PA plugin |
| SuperSlicer | Printer Settings → Extruder → Pressure Advance = 0 |

The system uses `default_pa` from the material profile (or your calibrated value via `AT_SET_PA`).
```

The system normalizes variations like `PLA+`, `PETG-CF`, `ABS-GF` to their base profiles.

---

## Troubleshooting

### No boost happening
- Check `AT_STATUS` — is system ENABLED?
- Is flow above the gate? (e.g., 14 mm³/s for PETG HF)
- Is it first layer? (boost disabled)
- Is heater at 95%+ PWM? (boost frozen)

### Corner bulging
- Increase `ramp_fall` in material profile (faster cooldown)
- PETG defaults to 1.5°C/s, try 2.0°C/s
- Check PA is being applied (`AT_STATUS` shows current PA)

### Under-extrusion at high speed
- Speed boost should help (PETG: +16°C at 300mm/s)
- Increase `speed_boost_k` in material profile
- Check heater isn't saturated (PWM < 95%)

### Stringing on PETG
- Decrease `ramp_fall` (slower cooldown prevents ooze)
- Increase `speed_boost_k` for more heat during fast moves

### Heaters stay on after print
- Ensure `AT_END` is called before `TURN_OFF_HEATERS` in PRINT_END

---

## Data Sources

### Native Klipper
- `printer.motion_report.live_extruder_velocity` → filament speed
- `printer.motion_report.live_velocity` → toolhead speed
- `printer.extruder.power` → heater PWM %
- `printer.toolhead.position.z` → Z height

### Python Extras
- `extruder_monitor.py` → 5-second lookahead, logging
- `gcode_interceptor.py` → G-code stream parsing
