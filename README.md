# Klipper Adaptive Flow

**Stop calibrating. Start printing.**

Automatic **Pressure Advance**, **temperature**, and **flow control** for **E3D Revo** hotends on Klipper. The system sets your PA to a proven baseline for your exact nozzle and material, then adjusts it in real-time as temperature and viscosity change during the print. Temperature tracks volumetric flow — boosting heat before high-flow moves and backing off during fine detail. A built-in slicer analyser reads your G-code settings and tells you exactly what to change for better results.

Tell it which Revo nozzle and heater you have, and it handles the rest. Every material, every print, automatically.

Think of it as Bambu Lab's auto-calibration, but for your Revo.

## The Problem

Every time you switch materials or try a new filament brand, you're supposed to:
- **Calibrate Pressure Advance** — print test patterns, squint at lines, pick a value. But PA depends on temperature, nozzle geometry, and filament viscosity. The "correct" value at 210°C is wrong at 230°C — and your printer hits both temps in the same print when flow changes
- **Tune flow and temperature together** — slicer defaults pick one temperature for the whole print, but a 5mm³/s detail move and a 15mm³/s infill move need very different nozzle temperatures. Too cold = under-extrusion on fast moves. Too hot = stringing on slow ones
- **Run flow tests** — print cubes, measure walls with calipers, calculate volumetric limits. Repeat for every nozzle/heater/material combination
- **Guess at slicer settings** — inner wall accel vs outer wall accel, speed limits, flow limits. Most users copy someone else's profile and hope for the best, with no way to know if their settings are actually matched to their hardware

Most people don't do any of this. They use slicer defaults and live with mediocre prints — corner bulging from wrong PA, rough overhangs from insufficient heat, and stringing from excessive heat. The few who do calibrate spend hours on it — and it's only valid for that one filament on that one printer at that one speed.

## The Solution

Tell it which Revo you have:
```ini
variable_use_high_flow_nozzle: True    # True = Revo HF, False = Revo Standard/Micro
variable_heater_wattage: 40            # Revo heater: 40W (stock) or 60W (upgrade)
```

That's it. Adaptive Flow handles:

| What | How |
|------|-----|
| **Pressure Advance** | Sets a proven PA baseline for your nozzle type and material (Standard vs HF). Then dynamically lowers PA as temperature rises — because hotter filament is less viscous and needs less pressure correction. No test patterns, no guessing |
| **Temperature** | Tracks volumetric flow in real-time and boosts nozzle temperature proportionally — more heat for fast infill, less for fine detail. A 5-second lookahead pre-heats before flow spikes arrive, so the nozzle is ready before it needs to be |
| **Flow management** | Knows the safe flow limits of your exact Revo nozzle and heater wattage. Caps temperature boost when the heater is near its limit, so the system never demands more than your hardware can deliver |
| **HF nozzle compensation** | The Revo HF has 2.3× more melt zone than the Standard — it needs different PA (auto-scaled 1.4×), wider smooth_time, and a temperature offset. All applied automatically when you set `use_high_flow_nozzle: True` |
| **Slicer analysis** | Reads your G-code settings, cross-references acceleration and speed values with actual print data, and tells you exactly which slicer setting to change and what value to use — no guessing, no forum-trawling |
| **Complex geometry** | Speed Guard auto-slows acceleration at tricky layers (domes, overhangs, curves) to prevent ringing and artifacts |

Your slicer just sends `MATERIAL=PETG` and the system does the rest.

## What Makes This Different

- **Sensible defaults, not magic calibration.** PA, flow, and thermal values are derived from E3D's published Revo specifications. The Revo's standardised melt zone makes these values more consistent than generic Klipper defaults, but they are still starting points. For best results on a Voron or other precision build, calibrate your extruder `rotation_distance` and store a printer-specific PA baseline with `AT_SET_PA`.
- **PA that actually works across a whole print.** Static PA is a compromise — it's tuned for one temperature, but your nozzle temperature shifts throughout the print as flow changes. Adaptive Flow tracks the temperature boost and scales PA in real-time: hotter filament is less viscous, so PA decreases. The result is consistent corners and fine detail whether the printer is crawling at 30mm/s or blasting infill at 300mm/s.
- **Revo-native.** The system knows the thermal characteristics of every Revo nozzle (HF vs Standard) and heater (40W vs 60W+). HF nozzles get auto-scaled PA (1.4×), wider smooth_time, and temp offset. It auto-scales every material profile to your specific Revo configuration — not generic values that work for no printer in particular.
- **Slicer-aware diagnostics.** The analysis dashboard extracts your slicer settings directly from G-code, maps acceleration values to specific slicer features, and shows you exactly what to change for better print quality. It calculates the volumetric flow rate for each speed setting and flags when settings exceed your nozzle's safe flow limit.
- **Zero maintenance.** Updates preserve your settings. Defaults improve over time. You don't need to re-tune anything after initial setup.
- **Scope:** Adaptive Flow solves *thermal* print quality issues (temperature swings, PA drift with viscosity, flow spikes). Mechanical issues like Z-wobble, belt tension, or frame resonance require mechanical fixes and Klipper's `SHAPER_CALIBRATE`.

## The PA–Temperature–Flow Connection

This is the core insight behind Adaptive Flow, and why static PA values are always a compromise.

**The physics:** When your nozzle temperature rises, filament becomes less viscous. Less viscous filament needs less pressure to push through the nozzle — so the optimal Pressure Advance value *drops*. When the nozzle cools, viscosity increases and PA needs to go *up*.

**The problem with static PA:** In a typical print, the nozzle runs at one slicer-set temperature. But fast infill moves and slow perimeter moves have very different flow demands. A static PA value might be perfect for 200mm/s infill at 240°C but too aggressive for a 40mm/s outer wall where the nozzle has cooled slightly. The result: corner bulging in some places, insufficient compensation in others.

**What Adaptive Flow does:**
1. **Monitors volumetric flow** — how many mm³/s of filament the extruder is pushing
2. **Adjusts temperature to match** — boosts heat proportional to flow, with a 5-second lookahead to pre-heat before spikes
3. **Scales PA with temperature** — as boost increases, PA decreases by `pa_boost_k` per °C (e.g. +20°C boost with `pa_boost_k: 0.001` reduces PA by 0.020)
4. **Respects hardware limits** — caps boost when heater PWM exceeds 95%, enforces minimum PA, uses deadband to avoid jitter

The result: your corners stay sharp at every speed, your infill doesn't under-extrude at high flow, and your fine detail doesn't blob from excessive PA — all without calibrating anything.

## Improving Your Slicer Settings

One of the most impactful features is the **slicer diagnostics** built into the analysis dashboard (`http://<printer-ip>:7127`, Slicer tab).

Most print quality issues aren't caused by the printer — they're caused by mismatched slicer settings. Inner wall accel at 5000 but outer wall at 500 causes visible transition lines. Infill speed that exceeds your hotend's flow limit causes under-extrusion. Bridge settings that are too aggressive cause drooping.

The Slicer tab reads the settings embedded in your G-code file and cross-references them with your actual print data:

- **Calculates volumetric flow** for every speed setting — shows you exactly how many mm³/s each feature demands
- **Flags settings that exceed your nozzle's safe limit** — based on E3D's published flow data for your specific Revo nozzle and heater wattage
- **Shows maximum safe speed** for each setting — so you know how fast you can go before quality degrades
- **Detects acceleration mismatches** — inner vs outer wall accel differences that cause visible lines at feature transitions
- **Provides specific before→after recommendations** — not vague advice, but "change `inner_wall_acceleration` from 5000 to 3000" with the OrcaSlicer menu location

This means you can print once, open the dashboard, and get a concrete list of slicer changes that will improve your next print — no trial and error.

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

**Cura:**
```gcode
PRINT_START BED={material_bed_temperature_layer_0} EXTRUDER={material_print_temperature_layer_0} MATERIAL={material_type}
```

`{filament_type[0]}` / `{material_type}` resolves to PLA, PETG, ABS, etc. automatically — but only if you've set the filament type correctly in the slicer:

| Slicer | Where to set filament type |
|--------|---------------------------|
| **OrcaSlicer** | Select a filament preset in the top toolbar (e.g. "Generic PETG"). The type is embedded in the preset — if you create a custom filament, set **Type** under Filament Settings → Basic. |
| **PrusaSlicer** | Select a filament preset in the right panel. For custom filaments: Filament Settings → Filament → **Type** dropdown. |
| **SuperSlicer** | Same as PrusaSlicer — filament preset or Filament Settings → **Filament type**. |
| **Cura** | Select a material in the material dropdown (top bar). For custom materials: Preferences → Materials → select material → **Properties → Material** field. |

> **The print will error if MATERIAL is missing.** If you see `AT_START: No MATERIAL parameter!` in the console, your slicer start G-code is missing the `MATERIAL=` part.

### 4. Add Macros to printer.cfg

Copy one of the ready-to-use templates into your `printer.cfg` (or a `[include]` file):

| Template | For |
|----------|-----|
| [PRINT_START.example](PRINT_START.example) | **Any printer** — generic, minimal |
| [PRINT_START_VORON24.example](PRINT_START_VORON24.example) | **Voron 2.4 / Trident** — QGL, rapid_scan mesh, filament sensor |

Both templates have `# >>> EDIT` markers on every line you need to customise. Everything else works as-is.

The only required integration points are:
```ini
# At the end of PRINT_START, after heating:
AT_START MATERIAL={params.MATERIAL|default("PLA")}

# At the start of PRINT_END, before anything else:
AT_END
```

### 5. Disable Slicer Pressure Advance

Adaptive Flow manages PA dynamically. If your slicer also sets PA, they'll conflict. Remove any `SET_PRESSURE_ADVANCE` lines and disable slicer-side PA:

| Slicer | Where to disable PA |
|--------|-------------------|
| **OrcaSlicer** | Printer Settings → Advanced → **Enable pressure advance** = OFF. Also check Printer Settings → Custom G-code and remove any `SET_PRESSURE_ADVANCE` lines. |
| **PrusaSlicer** | Printer Settings → Custom G-code → remove any `SET_PRESSURE_ADVANCE` lines from start/end G-code. PrusaSlicer doesn't set PA by default. |
| **SuperSlicer** | Printer Settings → Extruder 1 → **Pressure advance** = 0. Also remove any `SET_PRESSURE_ADVANCE` from Custom G-code. |
| **Cura** | Remove any `SET_PRESSURE_ADVANCE` from Start G-code (Settings → Printer → Manage Printers → Machine Settings). Disable any PA plugin. |

### 6. Print

Set your slicer temperature to the filament's recommended base temp, slice, and print. The system handles dynamic adjustments during the print.

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
| PLA | 0.024 | 0.034 | 210–215°C | Tuned for high-flow variants (PLA HF, PLA+). Verify PA for your specific setup |
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
- **Slicer diagnostics** — the most useful tab. Extracts settings directly from your G-code, cross-references acceleration values with print data, and tells you exactly which slicer setting to change and what value to use. This is the fastest way to fix print quality issues.
- **Live print monitoring** — temperature, flow, PA, and heater PWM plotted over the entire print timeline
- **Extrusion quality score** — weighted 0–100 composite rating covering thermal stability (45%), flow steadiness (30%), heater reserve (10%), and pressure consistency (15%). Scores are context-aware — heater reserve is treated as a capacity warning when thermal stability proves the heater is keeping up.
- **Heater analysis** — shows power usage at different flow rates, so you can see if your heater is the bottleneck
- **Distribution** — how your print spent its time across speeds and flow rates
- **Z-Height analysis** — identifies which layers had the most thermal stress

Every chart has tooltips explaining what you're looking at and what "good" looks like.

## How It Works

During a print, the system continuously:

1. **Measures** volumetric flow rate (mm³/s) from extruder velocity and filament cross-section
2. **Predicts** upcoming flow changes 5 seconds ahead via G-code lookahead — pre-heats the nozzle *before* a fast infill segment arrives
3. **Adjusts** nozzle temperature proportional to flow demand — more heat for high-flow moves, base temp for fine detail
4. **Scales PA in real-time** as temperature changes — hotter filament is less viscous, so PA decreases automatically (PA_adjusted = PA_base − boost × pa_boost_k)
5. **Monitors** heater duty cycle, capping temperature boost when PWM exceeds 95% so the system never demands more than the heater can deliver
6. **Guards** tricky geometry (Speed Guard) — auto-slows acceleration at domes, overhangs, and curves to prevent ringing and artifacts

All adjustments stay within safe limits defined by your hardware and material profile.

## What You Don't Need To Do

- ~~Print PA calibration patterns for every material~~
- ~~Recalibrate PA when you change nozzles or temperatures~~
- ~~Run flow tests with calipers~~
- ~~Calculate volumetric flow limits for your hotend~~
- ~~Tune temperature for different speeds and flow rates~~
- ~~Adjust settings when switching between Standard and HF nozzles~~
- ~~Manually override anything for heater upgrades~~
- ~~Guess which slicer acceleration/speed values to use~~ (the dashboard tells you)

For most users — especially those switching from a Standard to HF nozzle or changing filament brands — Adaptive Flow handles everything automatically.

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
| `AT_SPEED_GUARD_STATUS` | Show Speed Guard stress zone status |
| `AT_SET_PA MATERIAL=X PA=Y` | Override PA for a material |
| `AT_LIST_PA` | Show all PA values |

</details>

<details>
<summary>Features in detail</summary>

- **Dynamic Temperature** — Flow, speed, and acceleration-based boost with soft gating
- **Dynamic PA** — Scales with temperature boost, auto-compensates HF melt zone (1.4× PA, wider smooth_time)
- **5-Second Lookahead** — Pre-heats before flow spikes arrive
- **Speed Guard** — Auto-slows acceleration at tricky layers (domes, overhangs, curves) to prevent ringing and artifacts. Monitors stress per Z-height bin, persists scores across prints
- **Slicer Diagnostics** — Parses G-code footer, maps accel values to slicer features, shows flow rates and max speeds per setting based on E3D published data
- **Extrusion Quality Scoring** — Weighted 0–100 composite score covering thermal (45%), flow (30%), heater (10%), and pressure (15%) stability. Context-aware tips check your actual slicer settings before making recommendations
- **Boost Optimization** — Analyses actual heater and flow headroom, tells you how much faster you can print
- **Multi-Object Temp Management** — Prevents thermal runaway between sequential objects
- **Heater Duty Capping** — Won't request boost if heater is already at 95%+ PWM
- **First Layer Skip** — No boost on layer 1 for consistent squish

[Full configuration reference →](docs/CONFIGURATION.md) · [Analysis dashboard →](docs/ANALYSIS.md) · [Speed Guard docs →](docs/SPEED_GUARD.md)

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