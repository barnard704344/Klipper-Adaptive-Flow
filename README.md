# Klipper Adaptive Flow

Automatic temperature and pressure advance control for E3D Revo hotends on Klipper. Dynamically boosts nozzle temperature during high-flow sections and adjusts PA in real-time — no manual tuning required.

## Features

- **Dynamic temperature** — flow/speed/acceleration-based boost
- **Dynamic PA** — scales with temperature boost automatically
- **Smart Cooling** — adjusts part fan based on flow rate and layer time
- **5-second lookahead** — pre-heats before flow spikes
- **Dynamic Z-Window (DynZ)** — learns and adapts to convex surfaces
- **Multi-object temperature management** — prevents thermal runaway between sequential objects
- **Per-material profiles** — PLA (tuned for HF), PETG, ABS, ASA, TPU, Nylon, PC, HIPS (user-editable)
- **First layer skip** — consistent squish on layer 1
- **Heater monitoring** — won't request more than your heater can deliver
- **Print analysis** — AI-powered tuning suggestions (optional)

## Installation

```bash
cd ~ && git clone https://github.com/barnard704344/Klipper-Adaptive-Flow.git
cd Klipper-Adaptive-Flow
cp gcode_interceptor.py extruder_monitor.py ~/klipper/klippy/extras/
cp auto_flow.cfg material_profiles.cfg ~/printer_data/config/
sudo systemctl restart klipper
```

Add to `printer.cfg`:
```ini
[include auto_flow.cfg]
[gcode_interceptor]
[extruder_monitor]
```

## Slicer Setup

**Start G-code** (OrcaSlicer/PrusaSlicer/SuperSlicer):
```gcode
PRINT_START BED=[bed_temperature_initial_layer_single] EXTRUDER=[nozzle_temperature_initial_layer] MATERIAL={filament_type[0]}
```

`{filament_type[0]}` is a built-in slicer variable that automatically passes the material type (PLA, PETG, ABS, etc.) from your filament profile. No manual setup needed — it just works.

**End G-code:**
```gcode
PRINT_END
```

**Important:** Disable Pressure Advance in your slicer — this system handles PA dynamically.

See [PRINT_START.example](PRINT_START.example) for a complete example.

## Printer Macros

Add `AT_START` after heating and `AT_END` at print end:

```ini
[gcode_macro PRINT_START]
gcode:
    # ... your heating, homing, leveling ...
    AT_START MATERIAL={params.MATERIAL|default("PLA")}   # Enable adaptive flow

[gcode_macro PRINT_END]
gcode:
    AT_END                                # Disable adaptive flow
    TURN_OFF_HEATERS                      # Must come AFTER AT_END
    # ... your cooldown, park, etc ...
```

The `MATERIAL` parameter is passed from your slicer's start G-code (see above). If omitted, material is auto-detected from extruder temperature.

> **Important:** Call `AT_END` *before* `TURN_OFF_HEATERS` to ensure the control loop stops first.

## Configuration

### Basic Setup (most users)

Edit `auto_flow.cfg`:
```ini
variable_use_high_flow_nozzle: True   # False for standard Revo
```

### Material Profiles (optional customization)

Edit `material_profiles.cfg` to customize per-material boost curves:
```ini
[gcode_macro _AF_PROFILE_PLA]
variable_flow_k: 1.00           # Temp boost per mm³/s flow
variable_speed_boost_k: 0.08    # Temp boost per mm/s above 100
variable_max_boost: 30.0        # Max temp increase cap (°C)
variable_max_temp: 245          # Absolute max (HF PLA tolerates higher)
variable_ramp_rise: 5.0         # Heat up rate (°C/s)
variable_ramp_fall: 2.5         # Cool down rate (°C/s)
...
```

### Recommended Start Temperatures

Set these base temperatures in your slicer. The system will automatically boost during high-flow sections.

| Material | Low Flow<br>(<10mm³/s) | Medium Flow<br>(10-15mm³/s) | High Flow<br>(15-20mm³/s) | Notes |
|----------|----------------------|---------------------------|-------------------------|-------|
| **PLA** | 205-210°C | 215-220°C | 220-225°C | Tuned for high-flow variants (PLA+, PLA HF) |
| **PETG** | — | 240°C | — | Start at 240°C for most use cases |
| **ABS** | 235-240°C | 245-250°C | 250-255°C | Requires enclosure for best results |
| **ASA** | 240-245°C | 250-255°C | 255-260°C | Similar to ABS, slightly higher temps |
| **TPU** | 215-220°C | 220-225°C | 225-230°C | Keep speeds low, gentle ramps |
| **Nylon** | 240-245°C | 250-255°C | 255-260°C | Dry filament thoroughly before printing |
| **PC** | 265-270°C | 275-280°C | 280-285°C | Requires high-temp hotend and enclosure |
| **HIPS** | 220-225°C | 230-235°C | 235-240°C | Support material, similar to ABS |

**Flow Rate Guide:**
- **Low flow** (<10mm³/s): Standard detail prints, slower speeds
- **Medium flow** (10-15mm³/s): General purpose, balanced speed/quality
- **High flow** (15-20mm³/s): Speed printing with high-flow filaments

> **PLA Tuned for High Flow:** The default PLA profile is optimized for high-flow variants (PLA+, PLA HF). At 18mm³/s with 215°C base → boosts to 233°C. Use 215-220°C base temp in your slicer for high-speed printing.

**[Full configuration reference →](docs/CONFIGURATION.md)**

## Commands

| Command | Description |
|---------|-------------|
| `AT_STATUS` | Show current state, flow, boost, PA |
| `AT_DYNZ_STATUS` | Show Dynamic Z-Window learning state |
| `AT_SC_STATUS` | Show Smart Cooling status |
| `AT_SET_PA MATERIAL=X PA=Y` | Save calibrated PA |
| `AT_LIST_PA` | Show all PA values |

## How It Works

1. **Flow boost**: Temperature increases with volumetric flow rate
2. **Speed boost**: Extra heating for high-speed thin walls (>100mm/s)
3. **Lookahead**: Predicts flow 5 seconds ahead for pre-heating
4. **Dynamic PA**: Automatically reduces PA as temperature increases

Example during a print:
```
Base temp: 230°C, Base PA: 0.060
High flow detected → Boost +20°C → Temp 250°C, PA 0.048
```

## Dynamic Z-Window (DynZ)

DynZ is an intelligent learning system that detects and adapts to challenging print geometries like convex surfaces, domes, and spheres.

- **Learning**: Divides Z-height into bins and tracks stress conditions
- **Detection**: Flags when speed is high + flow is low + heater is working hard
- **Relief**: Reduces acceleration to ease thermal demand in stress zones
- **Memory**: Stress patterns persist, so it "remembers" where domes start

Check status during a print:
```
AT_DYNZ_STATUS
```

**[Full DynZ documentation →](docs/DYNZ.md)**

## Smart Cooling

Smart Cooling automatically adjusts the part cooling fan based on flow rate and layer time.

- **Flow-based**: Reduces fan at high flow (fast-moving plastic creates its own airflow)
- **Layer time**: Boosts fan for short/fast layers (prevents heat buildup)
- **Lookahead**: Pre-adjusts fan 5 seconds ahead of flow changes
- **Material-aware**: Each profile has its own min/max fan limits

Check status during a print:
```
AT_SC_STATUS
```

**[Full Smart Cooling documentation →](docs/SMART_COOLING.md)**

## Multi-Object Temperature Management

When printing multiple objects sequentially, the nozzle temperature from the first object may be higher than the target for the next object, potentially triggering Klipper's thermal runaway protection. This feature automatically pauses between objects to allow temperature stabilization.

- **Automatic**: Works with EXCLUDE_OBJECT (modern slicers) and M486 (legacy)
- **Smart waiting**: Only pauses if temperature difference exceeds tolerance (default ±5°C)
- **Safe**: Waits until temperature stabilizes (no timeout risk)
- **Zero config**: Enabled by default, works with OrcaSlicer, PrusaSlicer, SuperSlicer

### Configuration

Edit `auto_flow.cfg` to customize:

```ini
variable_multi_object_temp_wait: True     # Enable/disable feature
variable_temp_wait_tolerance: 5.0         # Temperature tolerance (°C)
```

### Slicer Setup

Enable object labeling in your slicer:

**OrcaSlicer/PrusaSlicer**: Print Settings → Output options → Label objects
**SuperSlicer**: Print Settings → Output options → Exclude objects

This feature works automatically—no G-code changes needed.

## Optional: Print Analysis

After printing, get AI-powered suggestions to improve your settings:

```bash
cd ~/Klipper-Adaptive-Flow
python3 analyze_print.py
```

The AI analyzes your print data and suggests parameter adjustments:
- Suggestions marked **[✓ SAFE]** can be auto-applied
- Suggestions marked **[⚠ MANUAL]** require your review

**Providers:** GitHub Models (free), OpenAI, Anthropic

**[Setup guide →](docs/ANALYSIS.md)**

## Requirements

- E3D Revo hotend (HF or Standard)
- Klipper firmware
- 40W or 60W heater

## File Structure

| File | Purpose |
|------|---------|
| `auto_flow.cfg` | Main control logic |
| `material_profiles.cfg` | User-editable material profiles |
| `extruder_monitor.py` | Lookahead + logging (Klipper extra) |
| `gcode_interceptor.py` | G-code parsing (Klipper extra) |
| `analyze_print.py` | Post-print LLM analysis (optional) |
| `moonraker_hook.py` | Auto-analysis after print (optional) |

## License

MIT



