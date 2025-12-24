# Klipper Adaptive Flow

Automatic temperature and pressure advance control for E3D Revo hotends on Klipper. Dynamically boosts nozzle temperature during high-flow sections and adjusts PA in real-time — no manual tuning required.

## Features

- **Dynamic temperature** — flow/speed/acceleration-based boost
- **Dynamic PA** — scales with temperature boost automatically
- **5-second lookahead** — pre-heats before flow spikes
- **Per-material profiles** — PLA, PETG, ABS, ASA, TPU, Nylon, PC, HIPS (user-editable)
- **First layer skip** — consistent squish on layer 1
- **Heater monitoring** — won't request more than your heater can deliver
- **Self-learning** — optimizes K-values and PA over time
- **Print analysis** — LLM-powered tuning suggestions (optional)

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
    AT_START                              # Enable adaptive flow

[gcode_macro PRINT_END]
gcode:
    AT_END                                # Stop loop, save learned values
    TURN_OFF_HEATERS                      # Must come AFTER AT_END
    # ... your cooldown, park, etc ...
```

MATERIAL is detected automatically from extruder temperature. For better accuracy, pass it from your slicer's start g-code (see Slicer Setup above).

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
[gcode_macro _AF_PROFILE_PETG]
variable_flow_k: 1.20           # Temp boost per mm³/s flow
variable_speed_boost_k: 0.08    # Temp boost per mm/s above 100
variable_max_boost: 40.0        # Max temp increase cap (°C)
variable_ramp_rise: 4.0         # Heat up rate (°C/s)
variable_ramp_fall: 1.5         # Cool down rate (°C/s)
...
```

**[Full configuration reference →](docs/CONFIGURATION.md)**

## Commands

| Command | Description |
|---------|-------------|
| `AT_STATUS` | Show current state, flow, boost, PA |
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



