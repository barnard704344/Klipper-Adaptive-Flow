# Klipper Adaptive Flow

Automatic temperature control for E3D Revo hotends on Klipper. Dynamically boosts nozzle temperature during high-flow sections and maintains heat through corners — no manual tuning required.

## What It Does

- **Boosts temperature** when flow rate increases (fast infill, thick lines)
- **Maintains heat** through corners to prevent bulging
- **Pre-heats** before high-flow sections using G-code lookahead
- **Auto-detects material** from your slicer's temperature setting
- **Learns optimal values** over time for your printer

## Installation

```bash
# On your Klipper host (Pi, etc.)
cd ~
git clone https://github.com/barnard704344/Klipper-Adaptive-Flow.git
cd Klipper-Adaptive-Flow

# Install
cp gcode_interceptor.py extruder_monitor.py ~/klipper/klippy/extras/
cp auto_flow.cfg ~/printer_data/config/

# Restart Klipper
sudo systemctl restart klipper
```

Add to your `printer.cfg`:

```ini
[include auto_flow.cfg]
[gcode_interceptor]
[extruder_monitor]
```

## Usage

**OrcaSlicer/PrusaSlicer start G-code:**
```gcode
PRINT_START BED=[bed_temperature_initial_layer_single] EXTRUDER=[nozzle_temperature_initial_layer] MATERIAL=[filament_type]
```

**In printer.cfg:**
```ini
[gcode_macro PRINT_START]
gcode:
    {% set BED = params.BED|default(60)|int %}
    {% set EXTRUDER = params.EXTRUDER|default(200)|int %}
    {% set MATERIAL = params.MATERIAL|default("PLA")|string %}
    
    G28                      ; Home
    M190 S{BED}              ; Wait for bed
    M109 S{EXTRUDER}         ; Wait for hotend
    
    AT_START                 ; Enable adaptive flow (AFTER heating!)
```

**Slicer end G-code:**
```gcode
AT_END
```

## Configuration

Edit `auto_flow.cfg` — the only setting most users need to change:

```ini
variable_use_high_flow_nozzle: True   # False for standard Revo nozzles
```

### Material Detection

Based on your slicer temperature:

| Temperature | Material |
|-------------|----------|
| 280°C+ | PC |
| 240-280°C | ABS/ASA/NYLON |
| 220-240°C | PETG |
| 180-220°C | PLA |
| <180°C | TPU |

### Default Pressure Advance

Applied automatically if you haven't calibrated:

| Material | PA |
|----------|-----|
| PLA | 0.040 |
| PETG | 0.060 |
| ABS/ASA | 0.050 |
| TPU | 0.200 |

Save your own calibrated values:
```gcode
AT_SET_PA MATERIAL=PLA PA=0.045
```

## Commands

| Command | Description |
|---------|-------------|
| `AT_START` | Enable adaptive flow (after heating) |
| `AT_END` | Disable and save learned values |
| `AT_STATUS` | Show current state, flow rate, boost |
| `AT_SET_PA MATERIAL=X PA=Y` | Save calibrated PA |
| `AT_LIST_PA` | Show all saved PA values |

## How It Works

1. Monitors live extrusion velocity from Klipper
2. Parses upcoming G-code to predict flow changes
3. Calculates temperature boost: `flow_rate × material_k_value`
4. Only boosts above material-specific thresholds (flow gates)
5. Heats fast, cools slow to maintain heat through corners
6. Self-learns optimal K-values based on thermal response

## Requirements

- **E3D Revo** hotend (High Flow or Standard)
- **Klipper** firmware
- 40W or 60W heater core

No TMC StallGuard required — uses velocity data only.

## Files

| File | Purpose |
|------|---------|
| `auto_flow.cfg` | Klipper macros and settings |
| `extruder_monitor.py` | G-code parsing and lookahead |
| `gcode_interceptor.py` | Hooks into Klipper G-code stream |
| `community_defaults.json` | Shared material settings |

## License

MIT



