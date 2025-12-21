# Klipper Adaptive Flow

Automatic temperature control for E3D Revo hotends on Klipper. Dynamically boosts nozzle temperature during high-flow sections — no manual tuning required.

## Features

- **Flow-based boost** — increases temp for fast infill/thick lines
- **Speed-based boost** — increases temp for fast thin walls
- **First layer skip** — consistent squish on layer 1
- **Heater monitoring** — won't request more than your heater can deliver
- **Self-learning** — optimizes over time

## Installation

```bash
cd ~ && git clone https://github.com/barnard704344/Klipper-Adaptive-Flow.git
cd Klipper-Adaptive-Flow
cp gcode_interceptor.py extruder_monitor.py ~/klipper/klippy/extras/
cp auto_flow.cfg ~/printer_data/config/
sudo systemctl restart klipper
```

Add to `printer.cfg`:
```ini
[include auto_flow.cfg]
[gcode_interceptor]
[extruder_monitor]
```

## Slicer Setup

**Start G-code:**
```gcode
PRINT_START BED=[bed_temperature_initial_layer_single] EXTRUDER=[nozzle_temperature_initial_layer] MATERIAL=[filament_type]
```

**End G-code:**
```gcode
PRINT_END
```

See [PRINT_START.example](PRINT_START.example) or [PRINT_START_VORON24.example](PRINT_START_VORON24.example).

## Configuration

One setting for most users — edit `auto_flow.cfg`:
```ini
variable_use_high_flow_nozzle: True   # False for standard Revo
```

**[Full configuration reference →](docs/CONFIGURATION.md)**

## Commands

| Command | Description |
|---------|-------------|
| `AT_STATUS` | Show current state, flow, boost |
| `AT_SET_PA MATERIAL=X PA=Y` | Save calibrated PA |
| `AT_LIST_PA` | Show all PA values |

## Requirements

- E3D Revo hotend (HF or Standard)
- Klipper firmware
- 40W or 60W heater

## License

MIT



