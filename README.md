# Klipper Adaptive Flow

**Stop calibrating. Start printing.**

Automatic temperature, pressure advance, and fan control for **E3D Revo** hotends on Klipper. Tell it which Revo nozzle and heater you have, and it handles the rest. Every material, every print, automatically.

Think of it as Bambu Lab's auto-calibration, but for your Revo.

## The Problem

Every time you switch materials or try a new filament brand, you're supposed to:
- Print PA calibration patterns and squint at lines
- Run flow tests and measure walls with calipers
- Tune fan speeds per material, per feature, per layer time
- Figure out what temperature to print at for your specific speed and flow rate
- Redo everything when you change your nozzle or heater

Most people don't do any of this. They use slicer defaults and live with mediocre prints. The few who do calibrate spend hours on it — and it's only valid for that one filament on that one printer at that one speed.

## The Solution

Tell it which Revo you have:
```ini
variable_use_high_flow_nozzle: True    # True = Revo HF, False = Revo Standard/Micro
variable_heater_wattage: 40            # Revo heater: 40W (stock) or 60W (upgrade)
```

That's it. Adaptive Flow handles:

| What | How |
|------|-----|
| **Temperature** | Dynamically boosts during high-flow moves, pre-heats 5 seconds ahead |
| **Pressure Advance** | Sets correct PA for your material and nozzle, adjusts in real-time as temp changes |
| **HF nozzle compensation** | Auto-detects HF melt zone, scales PA and smooth_time — no calibration needed |
| **Fan speed** | Adapts to flow rate, layer time, and heater capacity |
| **Heater limits** | Won't demand more than your heater can deliver — automatically scales to your wattage |
| **Complex geometry** | Learns where domes and overhangs cause trouble, adapts on future prints |
| **Slicer analysis** | Reads your G-code, maps acceleration values to slicer features, recommends specific settings |

Your slicer just sends `MATERIAL=PETG` and the system does the rest.

## What Makes This Different

- **No calibration prints.** PA, flow, and thermal values are derived from E3D's published Revo specifications and validated across direct-drive CoreXY setups. The Revo's standardised melt zone means these values are consistent across every Revo hotend — including automatic HF compensation for the larger melt zone.
- **Revo-native.** The system knows the thermal characteristics of every Revo nozzle (HF vs Standard) and heater (40W vs 60W+). HF nozzles get auto-scaled PA (1.4×), wider smooth_time, and temp offset. It auto-scales every material profile to your specific Revo configuration — not generic values that work for no printer in particular.
- **Learns from every print.** The analysis dashboard tracks trends across prints, diagnoses slicer settings from your G-code, and recommends improvements. The more you print, the better it gets.
- **Zero maintenance.** Updates preserve your settings. Defaults improve over time. You don't need to re-tune anything.

## Quick Start

### 1. Install (2 minutes)
```bash
cd ~ && git clone https://github.com/barnard704344/Klipper-Adaptive-Flow.git
cd Klipper-Adaptive-Flow
./update.sh
```

The script handles everything: copies files, configures `printer.cfg`, starts services, restarts Klipper.

### 2. Set Your Hardware

Edit `~/printer_data/config/auto_flow_user.cfg`:
```ini
variable_use_high_flow_nozzle: True    # True = Revo HF, False = Revo Standard/Micro
variable_heater_wattage: 40            # Revo heater wattage (40 = stock, 60 = upgrade)
```

### 3. Set Your Slicer Start G-code

**OrcaSlicer / PrusaSlicer / SuperSlicer:**
```gcode
PRINT_START BED=[bed_temperature_initial_layer_single] EXTRUDER=[nozzle_temperature_initial_layer] MATERIAL={filament_type[0]}
```

`{filament_type[0]}` is a built-in slicer variable — it automatically passes PLA, PETG, ABS, etc. No manual setup.

### 4. Add Macros to printer.cfg

```ini
[gcode_macro PRINT_START]
gcode:
    # ... your heating, homing, leveling ...
    AT_START MATERIAL={params.MATERIAL|default("PLA")}

[gcode_macro PRINT_END]
gcode:
    AT_END
    TURN_OFF_HEATERS
    # ... your cooldown, park, etc ...
```

See [PRINT_START.example](PRINT_START.example) for a complete example.

### 5. Print

Set your slicer temperature to the filament's recommended base temp, slice, and print. The system handles dynamic adjustments during the print.

**Important:** Disable Pressure Advance in your slicer — this system handles PA dynamically.

## Heater Auto-Scaling

Material profiles are tuned for the stock 40W Revo heater. If you've upgraded to a higher-wattage cartridge, the system **automatically scales** all thermal parameters — no per-material overrides needed.

| Heater | PETG boost at 10mm³/s | Behaviour |
|--------|----------------------|-----------|
| 40W | 5.0°C | Modest, achievable boosts |
| 60W | 6.5°C | Larger boosts, faster response |

Upgrading your heater? Change one number:
```ini
variable_heater_wattage: 60
```
Restart Klipper. Every material automatically gets appropriate scaling.

## Supported Materials

All materials work out of the box with hardware-appropriate defaults:

| Material | Default PA (Std) | PA with HF | Base Temp | Notes |
|----------|-----------------|------------|-----------|-------|
| PLA | 0.032 | 0.045 | 210–215°C | Tuned for high-flow variants |
| PETG | 0.040 | 0.056 | 240–245°C | Conservative for 40W, scales up for 60W+ |
| ABS | 0.040 | 0.056 | 245–250°C | Requires enclosure |
| ASA | 0.040 | 0.056 | 250–255°C | Similar to ABS |
| TPU | 0.060 | 0.084 | 220–225°C | Gentle ramps, slow speeds |
| Nylon | 0.040 | 0.056 | 250–255°C | Dry filament before printing |
| PC | 0.045 | 0.063 | 275–280°C | High-temp hotend + enclosure |
| HIPS | 0.045 | 0.063 | 230–235°C | Support material |

> PA with HF is auto-computed (default_pa × 1.4) when `use_high_flow_nozzle: True`. No configuration needed.

Custom materials: copy any profile to `material_profiles_user.cfg` and adjust.

## Analysis Dashboard

Open `http://<printer-ip>:7127` in your browser. No SSH, no terminal.

The dashboard shows:
- **Live print monitoring** — temperature, flow, PA, fan in real-time
- **Per-material history** — track how each material performs across prints
- **Recommendations** — actionable suggestions with one-click Apply buttons
- **Slicer diagnostics** — extracts settings from your G-code, cross-references acceleration values with banding data, and recommends specific slicer changes
- **Banding analysis** — identifies what's causing print artifacts
- **Thermal headroom** — shows if your heater is the bottleneck
Every chart has tooltips explaining what you're looking at and what "good" looks like. The more you print, the smarter the recommendations get.

## How It Works

During a print, the system continuously:

1. **Measures** volumetric flow rate from extruder velocity
2. **Predicts** upcoming flow changes 5 seconds ahead (lookahead)
3. **Adjusts** nozzle temperature proportional to flow demand
4. **Scales** PA as temperature changes (hotter = less viscous = less PA needed)
5. **Controls** fan speed based on flow, layer time, and heater duty cycle
6. **Learns** problem zones (DynZ) and adapts on future layers

All adjustments stay within safe limits defined by your hardware.

## What You Don't Need To Do

- ~~Print PA calibration patterns~~
- ~~Run flow tests with calipers~~
- ~~Create per-material fan profiles~~
- ~~Calculate volumetric flow limits~~
- ~~Tune temperature for different speeds~~
- ~~Adjust settings when switching nozzles~~
- ~~Manually override anything for heater upgrades~~

## Advanced (Optional)

Most users never need to touch these. They exist for edge cases and experimentation.

<details>
<summary>Configuration files</summary>

| File | Purpose |
|------|---------|
| `auto_flow_user.cfg` | Your hardware settings (never overwritten by updates) |
| `material_profiles_user.cfg` | Custom material overrides (never overwritten by updates) |
| `auto_flow_defaults.cfg` | System defaults (updated by git) |
| `material_profiles_defaults.cfg` | Material profiles (updated by git) |

</details>

<details>
<summary>Commands</summary>

| Command | Description |
|---------|-------------|
| `AT_START MATERIAL=X` | Enable (call in PRINT_START) |
| `AT_END` | Disable (call in PRINT_END) |
| `AT_STATUS` | Show current state |
| `AT_DYNZ_STATUS` | Show DynZ learning state |
| `AT_SET_PA MATERIAL=X PA=Y` | Override PA for a material |
| `AT_LIST_PA` | Show all PA values |

</details>

<details>
<summary>Features in detail</summary>

- **Dynamic Temperature** — Flow, speed, and acceleration-based boost with soft gating
- **Dynamic PA** — Scales with temperature boost, auto-compensates HF melt zone (1.4× PA, wider smooth_time)
- **5-Second Lookahead** — Pre-heats before flow spikes arrive
- **Dynamic Z-Window (DynZ)** — Learns convex surfaces, reduces demand on problem layers
- **Slicer Diagnostics** — Parses G-code footer, maps accel values to slicer features, recommends specific settings
- **Multi-Object Temp Management** — Prevents thermal runaway between sequential objects
- **Heater Duty Capping** — Won't request boost if heater is already at 95%+ PWM
- **First Layer Skip** — No boost on layer 1 for consistent squish

[Full configuration reference →](docs/CONFIGURATION.md) · [Analysis dashboard →](docs/ANALYSIS.md) · [DynZ docs →](docs/DYNZ.md)

</details>

<details>
<summary>Updating</summary>

```bash
cd ~/Klipper-Adaptive-Flow && ./update.sh
```

The update script:
- Updates system files, preserves your settings
- Auto-configures `printer.cfg` if includes are missing
- Creates backups before any changes
- Restarts Klipper and dashboard automatically
- Handles migration from older versions

</details>

## Requirements

- **E3D Revo hotend** — Revo Six, Revo Micro, or Revo Voron (HF or Standard nozzle)
- **Klipper firmware** — any recent version
- **Stock or upgraded heater** — 40W (stock) or 60W upgrade cartridge
- **Direct-drive extruder** — PA defaults are tuned for direct-drive (Bowden users may need to override PA values)
## License

MIT