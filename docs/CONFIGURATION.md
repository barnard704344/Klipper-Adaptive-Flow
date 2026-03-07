# Configuration Reference

Detailed configuration options for Klipper Adaptive Flow.

## Quick Start

Most users only need to set two options in `auto_flow_user.cfg`:

```ini
variable_use_high_flow_nozzle: True   # False for standard Revo nozzles
variable_sc_heater_wattage: 40         # 40W (stock) or 60W (upgrade)
```

Everything else auto-configures: PA values, smooth_time, thermal parameters, fan control, and HF melt zone compensation are all derived from your nozzle type and heater wattage.

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
variable_flow_k: 0.50           # Temp boost per mm³/s (40W base, auto-scaled)
variable_speed_boost_k: 0.06    # Temp boost per mm/s above 100mm/s
variable_max_boost: 15.0        # Maximum temp boost cap (°C, auto-scaled)
variable_max_temp: 280          # Absolute temp safety limit
variable_flow_gate: 12.0        # Flow threshold for HF nozzle (mm³/s)
variable_flow_gate_std: 8.0     # Flow threshold for std nozzle (mm³/s)
variable_pa_boost_k: 0.0008     # PA reduction per °C of boost
variable_ramp_rise: 3.0         # Heat up rate (°C/s, auto-scaled)
variable_ramp_fall: 1.5         # Cool down rate (°C/s)
variable_default_pa: 0.040      # Default PA (Standard nozzle; HF auto-scales)
variable_sc_flow_gate: 10.0     # Smart cooling flow threshold
variable_sc_flow_k: 0.02        # Fan reduction per mm³/s above gate
variable_sc_min_fan: 0.15       # Minimum fan speed
variable_sc_max_fan: 0.40       # Maximum fan speed (absolute ceiling)
gcode:
```

> **Note:** `default_pa` values are calibrated for Standard Revo nozzles. When `use_high_flow_nozzle` is True, PA is automatically scaled by `hf_pa_scale` (1.4×) — no manual override needed.

### Default Profiles

| Material | Flow K | Speed K | Max Boost | Max Temp | Ramp ↑/↓ | Default PA (Std) | PA with HF (auto) |
|----------|--------|---------|-----------|----------|----------|------------------|--------------------|
| **PLA** | 0.50 | 0.06 | 12°C | 245°C | 2.5/1.5 | 0.032 | 0.045 |
| **PETG** | 0.50 | 0.06 | 15°C | 280°C | 3.0/1.5 | 0.040 | 0.056 |
| **ABS** | 0.50 | 0.08 | 18°C | 290°C | 3.0/2.0 | 0.040 | 0.056 |
| **ASA** | 0.50 | 0.08 | 18°C | 295°C | 3.0/2.0 | 0.040 | 0.056 |
| **TPU** | 0.20 | 0.02 | 15°C | 240°C | 1.5/0.5 | 0.060 | 0.084 |
| **Nylon** | 0.50 | 0.06 | 18°C | 275°C | 2.5/1.5 | 0.040 | 0.056 |
| **PC** | 0.45 | 0.06 | 18°C | 310°C | 3.0/2.0 | 0.045 | 0.063 |
| **HIPS** | 0.50 | 0.06 | 18°C | 250°C | 3.0/1.5 | 0.045 | 0.063 |

> **Note:** Flow K, Ramp ↑, Max Boost, and Speed K are 40W base values — automatically scaled up for 60W+ heaters. PA with HF column shows the auto-computed value when `use_high_flow_nozzle: True` (hf_pa_scale × default_pa).

### Recommended Base Temperatures

Set these start temperatures in your slicer. The system will automatically boost temperature during high-flow sections.

| Material | Low Flow<br>(<10mm³/s) | Medium Flow<br>(10-15mm³/s) | High Flow<br>(15-20mm³/s) | Special Considerations |
|----------|----------------------|---------------------------|-------------------------|------------------------|
| **PLA** | 205-210°C | 215-220°C | 220-225°C | High-flow variants (PLA+, PLA HF). Standard PLA: use 200-210°C |
| **PETG** | — | 240°C | — | Start at 240°C for most printing. Boost handles high flow automatically |
| **ABS** | 235-240°C | 245-250°C | 250-255°C | Requires heated chamber/enclosure. Keep consistent for layer adhesion |
| **ASA** | 240-245°C | 250-255°C | 255-260°C | Similar to ABS but more UV-resistant. Needs enclosure |
| **TPU** | 215-220°C | 220-225°C | 225-230°C | Keep print speeds low (20-40mm/s). Gentle ramps prevent degradation |
| **Nylon** | 240-245°C | 250-255°C | 255-260°C | MUST be dried thoroughly. Hygroscopic - absorbs moisture quickly |
| **PC** | 265-270°C | 275-280°C | 280-285°C | Requires all-metal hotend, high-temp thermistor, and enclosure |
| **HIPS** | 220-225°C | 230-235°C | 235-240°C | Commonly used as support for ABS. Dissolves in limonene |

**Flow Rate Guidelines:**
- **Low flow** (<10mm³/s): Detailed prints, fine features, slower speeds (30-80mm/s)
- **Medium flow** (10-15mm³/s): General purpose printing, balanced speed and quality (80-150mm/s)
- **High flow** (15-20mm³/s): Speed-focused printing with high-flow filament and nozzles (150-300mm/s)

**Temperature Boost Examples (40W heater):**
- PLA at 16mm³/s: 215°C base + (16−12) × 0.50 = 217°C final
- PETG at 16mm³/s: 240°C base + (16−12) × 0.50 = 242°C final
- ABS at 18mm³/s: 245°C base + (18−14) × 0.50 = 247°C final

Boosts are modest by design for 40W heaters — only demanding what the heater can actually deliver. With a 60W heater, flow_k auto-scales to ~0.65, giving larger boosts.

### Additional PLA Temperature Details

For PLA specifically, more granular temperature recommendations based on exact flow rates:

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
| `default_pa` | PA value if user hasn't calibrated (Std nozzle base) |
| `sc_flow_gate` | Smart cooling: flow threshold for fan reduction |
| `sc_flow_k` | Smart cooling: fan reduction per mm³/s above gate |
| `sc_min_fan` | Smart cooling: minimum fan speed (0.0-1.0) |
| `sc_max_fan` | Smart cooling: maximum fan speed — absolute ceiling |

### Adding Custom Materials

1. Copy any profile in `material_profiles.cfg`
2. Rename to `[gcode_macro _AF_PROFILE_YOURMATERIAL]`
3. Adjust the values
4. Use: `AT_START MATERIAL=YOURMATERIAL`

---

## Advanced Configuration

Edit these variables in `auto_flow_user.cfg` if needed.

### HF Melt Zone Compensation

The Revo HF has ~2.3× more melt zone volume than the Revo Standard. This stored molten filament acts as a pressure reservoir — after speed changes at feature transitions, the HF responds slower, causing visible artifacts that the Standard handles cleanly.

When `use_high_flow_nozzle: True`, the system automatically:
- **Scales PA** by `hf_pa_scale` (default 1.4×) so the extruder can overcome the HF's larger melt zone
- **Sets smooth_time** to `hf_smooth_time` (0.060s vs Klipper's default 0.040s) for a wider PA smoothing window that matches the HF's slower pressure response
- **Adds temp offset** of `hf_temp_offset` (5°C) since the HF melt zone benefits from extra heat
- **Warns** at print start if extruder microsteps < 32 (recommended for sufficient PA resolution)

```ini
variable_hf_pa_scale: 1.40       # PA multiplier for HF nozzles (1.0 = no scaling)
variable_hf_smooth_time: 0.060   # PA smooth_time for HF (seconds)
variable_sf_smooth_time: 0.040   # PA smooth_time for Standard (seconds)
variable_hf_temp_offset: 5.0     # Extra temp offset for HF nozzles (°C)
```

All compensation is restored to defaults when the print ends (`AT_END`).

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
variable_flow_smoothing: 0.35          # 0.0-1.0, lower = faster response
```

0.35 is recommended for quality-focused prints (less jitter). Use 0.15 for faster response if needed.

### First Layer Mode

```ini
variable_first_layer_skip: True        # Disable boost on first layer
variable_first_layer_height: 0.3       # Z height considered "first layer"
```

### Filament Cross-Section

```ini
variable_filament_cross_section: 2.405  # mm² for 1.75mm filament (π × 0.875²)
```

Change this if using 2.85mm filament (`6.382`) or another non-standard diameter.

### Dynamic Pressure Advance

```ini
variable_pa_enable: True               # Enable dynamic PA adjustment
variable_pa_deadband: 0.003            # Min PA change before issuing command
variable_pa_min_value: 0.010           # Absolute minimum PA allowed
variable_pa_max_reduction: 0.020       # Max PA reduction from base value
```

The `pa_deadband` prevents unnecessary `SET_PRESSURE_ADVANCE` commands for tiny fluctuations.

PA values come from material profiles (`default_pa`) and are automatically scaled for HF nozzles. Users can override with `AT_SET_PA MATERIAL=X PA=Y`, which takes priority over auto-computed values.

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
- `printer.motion_report.live_extruder_velocity` → filament speed (mm/s)
- `printer.motion_report.live_velocity` → toolhead speed (mm/s)
- `printer.extruder.power` → heater PWM (0.0–1.0)
- `printer.toolhead.position.z` → Z height (mm)

### Python Extras
- `extruder_monitor.py` → 5-second lookahead buffer, predicted extrusion rate and volumetric flow, print session logging with banding risk analysis
- `gcode_interceptor.py` → G-code stream interception and broadcast to subscribers
