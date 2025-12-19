# Klipper Adaptive Flow

**A closed-loop flow control and artifact detection system for Klipper.**

This system uses TMC driver feedback to actively manage temperature, pressure advance, and print speed. It is specifically tuned for **E3D Revo** hotends using **Voron/Sherpa** style extruders.

> **Hardware:** Designed for **E3D Revo hotends** (HF and Standard) with **TMC drivers** (2209/2240/5160)

---

## Why Use This?

**The Problem:** Standard 3D printing uses a fixed hotend temperature. But filament viscosity and required melt energy change constantly based on:
- How fast you're extruding (infill vs perimeters vs small details)
- How much back-pressure the nozzle creates under load
- Upcoming flow changes the slicer has planned

Running too cold = under-extrusion, weak layer adhesion, clogs.  
Running too hot = stringing, oozing, heat creep, burned filament.

**The Solution:** This system monitors real-time extruder load via TMC StallGuard and looks ahead at upcoming G-code to **dynamically adjust temperature** — hotter for high-flow sections, cooler during travel moves.

### What You Get

| Benefit | How It Works |
|---------|--------------|
| **Better print quality** | Temperature matched to actual flow demand reduces under-extrusion and stringing |
| **Faster prints** | Push higher flow rates without quality loss — the system compensates automatically |
| **Zero configuration** | Auto-calibrates on first print, detects material from temp, learns optimal settings |
| **Set and forget** | Just add `AT_START` to your slicer — no per-print tuning needed |
| **Works with any slicer** | Temperature-based material detection means no slicer plugins required |
| **Self-improving** | K-values automatically tune themselves based on your printer's thermal response |

### Who Is This For?

- ✅ You have an **E3D Revo** hotend (HF or Standard)
- ✅ You use **TMC stepper drivers** with StallGuard capability
- ✅ You want **Bambu-style automatic calibration** on your Klipper printer
- ✅ You print a variety of materials and want consistent quality without manual tuning
- ✅ You push high flow rates and fight under-extrusion at speed

### Who Is This NOT For?

- ❌ Non-Revo hotends (different thermal mass = different K-values needed)
- ❌ Non-TMC drivers (no StallGuard = no load sensing)
- ❌ Users who prefer fully manual control

---

## ⚠️ Hardware Requirements

This script is tuned for the following hardware ecosystem:

| Component | Supported Options |
|-----------|-------------------|
| **Hotend** | E3D Revo (Standard or High Flow nozzle) |
| **HeaterCore** | 40W (Standard) or 60W (High Speed/High Flow) |
| **Extruder** | Voron StealthBurner (CW2), Sherpa Mini, or Orbiter |
| **Motor** | NEMA 14 Pancake (LDO-36STH20 or Generic) |
| **Electronics** | BTT EBB36 / SB2209 (CAN Bus) or similar with TMC2209 |

---

## ⚡ Zero-Config Quick Start

**Bambu-style "install and forget" operation:**

1. Install the modules (see [Installation](#-installation))
2. Add one line to your slicer's start G-code (after heating):
   ```gcode
   AT_START
   ```
3. Add to your end G-code:
   ```gcode
   AT_END
   ```
4. **Print!** — No manual calibration required.

The system will automatically:
- **Auto-calibrate** baseline during your first print's extrusion
- **Detect material** from your slicer's temperature setting
- **Apply optimal K-values** and pressure advance for that material
- **Self-learn** over time, adjusting K-values based on thermal response

That's it. No `AT_INIT_MATERIAL`, no calibration wizards, no manual baseline setting.

---

## ✨ Features

### 1. Revo-Optimized Temp Boosting
Automatically raises the temperature as flow rate increases.
- **Predictive Acceleration:** Detects rapid speed changes and "kicks" the heater to overcome thermal lag.
- **Flow Gates:** Automatically ignores slow moves to prevent oozing.
  - **Standard Nozzle:** Boosts start > 8mm³/s.
  - **High Flow Nozzle:** Boosts start > 15mm³/s.

### 2. Burst Protection (Noise Filtering)
High-speed printing often involves tiny, rapid movements like **Gap Infill**.
- **The Problem:** Without filtering, the script sees a 0.1-second spike to 300mm/s and immediately commands a +20°C boost. Since the move is over before the heater can react, the nozzle just overheats while idle.
- **The Solution:** The script applies a **Rolling Weighted Average** to the flow calculation.
  - Sustained speed (long walls/infill) triggers the full boost.
  - Short bursts (< 0.5s) are smoothed out and ignored.
  - This ensures the heater only reacts to moves long enough to benefit from the extra energy.

### 3. Extrusion Crash Detection
Monitors the extruder motor for resistance spikes (blobs/tangles). If >3 spikes occur in one layer, the printer slows to 50% speed for 3 layers to recover.

### 4. Smart Cornering ("Sticky Heat")
High-speed printing often suffers from **Bulging Corners**. This happens because when the print head brakes for a corner, the pressure in the nozzle doesn't drop instantly.
- **The Problem:** Standard logic cools the nozzle when speed drops. Cooler plastic = Higher Viscosity = More Bulging.
- **The Fix:** This script uses **Asymmetric Smoothing**. It heats up *instantly* to match acceleration but cools down *very slowly*. This keeps the plastic fluid during corner braking, allowing the extruder to relieve pressure effectively.

### 5. Dynamic Pressure Advance
Works in tandem with Smart Cornering. As the temperature boosts, the plastic becomes more fluid (lower viscosity).
- **The Logic:** Hotter plastic requires **less** Pressure Advance to control.
- **The Action:** The script automatically **lowers** your PA value as the temperature rises. This prevents the "gaps" or "shredded corners" that occur when you apply high-speed PA values to super-heated plastic.

### 6. Live G-code Lookahead
The system parses upcoming G-code moves to predict flow changes *before* they happen.
- **Proactive Boosting:** Raises temperature before high-flow sections arrive
- **Smoother Transitions:** Eliminates under-extrusion at flow ramp-ups
- **Ooze Prevention:** Drops temp during long travels when no extrusion is coming

### 7. Ooze Prevention
Automatically drops temperature during long travel moves to reduce stringing and oozing.

### 8. Smart Retraction
Dynamic retraction distance based on current temperature boost — hotter plastic needs more retraction.

### 9. Thermal Safety
Built-in runaway protection with emergency shutdown if temperature exceeds safe limits.

### 10. Self-Learning K-Values
The system monitors thermal response and gradually optimizes boost aggressiveness over time.

---

## 📦 Installation

### Step 1: Download the Files

**Option A: Clone the repository (recommended)**
```bash
cd ~
git clone https://github.com/barnard704344/Klipper-Adaptive-Flow.git
cd Klipper-Adaptive-Flow

# Copy Python modules to Klipper extras
cp gcode_interceptor.py ~/klipper/klippy/extras/
cp extruder_monitor.py ~/klipper/klippy/extras/

# Copy macro file to your config
cp auto_flow.cfg ~/printer_data/config/
```

**Option B: Download files directly**
```bash
# Python modules
cd ~/klipper/klippy/extras/
wget https://raw.githubusercontent.com/barnard704344/Klipper-Adaptive-Flow/main/gcode_interceptor.py
wget https://raw.githubusercontent.com/barnard704344/Klipper-Adaptive-Flow/main/extruder_monitor.py

# Macro file
cd ~/printer_data/config/
wget https://raw.githubusercontent.com/barnard704344/Klipper-Adaptive-Flow/main/auto_flow.cfg
```

**Option C: Manual creation (if wget unavailable)**

Create each file using nano:
```bash
nano ~/klipper/klippy/extras/gcode_interceptor.py
# Paste contents from GitHub, save with Ctrl+X, Y, Enter

nano ~/klipper/klippy/extras/extruder_monitor.py
# Paste contents from GitHub, save with Ctrl+X, Y, Enter

nano ~/printer_data/config/auto_flow.cfg
# Paste contents from GitHub, save with Ctrl+X, Y, Enter
```

Restart Klipper:
```bash
sudo systemctl restart klipper
```

### Step 2: Edit printer.cfg

Add the following to your `printer.cfg`:

```ini
[include auto_flow.cfg]

[gcode_interceptor]

[extruder_monitor]
# IMPORTANT: Change this to match your actual driver section!
# Examples: "tmc2209 extruder" or "tmc2209 stepper_e" or "tmc5160 extruder"
driver_name: tmc2209 extruder

[save_variables]
filename: ~/printer_data/config/adaptive_flow_vars.cfg
```

**Toolhead Temperature Sensor (recommended for EBB36/SB2209):**
```ini
[temperature_sensor Toolhead_Temp]
sensor_type: temperature_mcu
sensor_mcu: EBBCan
```

**TMC Driver Configuration:**
In your TMC section, ensure StallGuard is enabled:
```ini
[tmc2209 extruder]
run_current: 0.650
stealthchop_threshold: 0
driver_SGTHRS: 120
```

**Extruder Configuration:**
```ini
[extruder]
max_extrude_only_distance: 101.0
```

### Step 3: Verify Installation

Restart Klipper and check logs for:
```
GCodeInterceptor: Ready and intercepting G-code
Live G-code lookahead hook installed via gcode_interceptor.
```

### Step 4: Integrate with Your PRINT_START Macro

Add `AT_START` at the end of your `PRINT_START` macro (after heating), and `AT_END` at the start of your `PRINT_END` macro:

```ini
[gcode_macro PRINT_START]
gcode:
    {% set BED = params.BED|default(60)|int %}
    {% set EXTRUDER = params.EXTRUDER|default(200)|int %}
    
    # Your existing startup sequence...
    G28                          ; Home
    M190 S{BED}                  ; Wait for bed
    M109 S{EXTRUDER}             ; Wait for hotend
    # ... bed mesh, purge line, etc ...
    
    # Enable Adaptive Flow (after heating!)
    AT_START

[gcode_macro PRINT_END]
gcode:
    AT_END                       ; Disable Adaptive Flow
    # Your existing end sequence...
    M104 S0                      ; Turn off hotend
    M140 S0                      ; Turn off bed
    G28 X Y                      ; Home X/Y
```

> **Important:** `AT_START` must be called AFTER heating is complete. It reads `printer.extruder.target` to detect material.

---

## ⚙️ Configuration & Tuning

All settings are located at the **top** of `auto_flow.cfg` in the USER CONFIGURATION block.

### Step 1: Install TMC Autotune (Required)

> ⚠️ **This step is required.** NEMA 14 "Pancake" motors (LDO-36STH20) have very low inductance. Without TMC Autotune, StallGuard will report "0" load and Adaptive Flow cannot function.

**Install Klipper TMC Autotune:**

```bash
cd ~
wget https://raw.githubusercontent.com/andrewmcgr/klipper_tmc_autotune/main/install.sh
bash install.sh
```

**Add to `printer.cfg`:**
```ini
[autotune_tmc extruder]
motor: ldo-36sth20-1004ahg
tuning_goal: performance
```

Restart Klipper: `sudo systemctl restart klipper`

### Step 2: Calibrate Motor Baseline

The baseline is the StallGuard reading at "zero load" (extruding freely into air).

**Automatic Calibration (Recommended):**
```gcode
AT_AUTO_CALIBRATE TEMP=220 LENGTH=50 SAMPLES=10
```

**Manual Calibration:**
1. Heat nozzle to printing temp
2. Unload filament or lift Z high
3. Run: `AT_CHECK_BASELINE`
4. Note the Extruder Load number:
   - **Pancake Motors (LDO/Orbiter/Sherpa):** Expect **12 - 20**
   - **Standard NEMA 17:** Expect **60 - 100**
5. Save: `SAVE_VARIABLE VARIABLE=sensor_baseline VALUE=16`

### Step 3: Select Nozzle Type

Edit `auto_flow.cfg`:

- **Revo High Flow (HF):** `variable_use_high_flow_nozzle: True` (gate at 15mm³/s)
- **Revo Standard (Brass/ObX):** `variable_use_high_flow_nozzle: False` (gate at 8mm³/s)

---

## 🌡️ Recommended Base Temperatures

When using this script, set your slicer temperature to a standard **"Quality"** temperature. **Do not** set a high-speed temperature — the script will boost on top of your base.

| Material | Slicer Base Temp | Max Safety Cap | Notes |
|----------|------------------|----------------|-------|
| **PLA** | **210°C** | 235°C | Best balance of cooling vs flow |
| **PETG** | **240-245°C** | 275°C | 245°C ensures good layer bond at low speeds |
| **ABS/ASA** | **250°C** | 290°C | Needs heat. Boost takes it to ~275°C |
| **PC/Nylon** | **270°C** | 300°C | ⚠️ Revo max is 300°C |
| **TPU** | **230°C** | 240°C | Auto-Flow usually disabled to prevent foaming |

> **Note:** If your filament says "210-230°C", pick the **highest number (230°C)** as your base. High-speed printing reduces heat absorption time.

---

## ✂️ Slicer Configuration

### 1. Pressure Advance (Critical)

**Disable Pressure Advance in your slicer.**
- **Orca Slicer:** Set "Pressure Advance" to `0` in Filament Settings
- **PrusaSlicer:** Remove any `M572` or `SET_PRESSURE_ADVANCE` commands

The script manages PA dynamically. Set your calibrated values with:
```gcode
AT_SET_PA MATERIAL=PLA PA=0.045
```

### 2. Max Volumetric Speed (Safety Caps)

Set **Max Volumetric Speed** in your slicer based on your hardware:

| Heater Core | Nozzle Type | Recommended Limit | Bottleneck |
|-------------|-------------|-------------------|------------|
| **40W** | Standard (Brass) | **17 mm³/s** | Melt Zone Geometry |
| **60W** | Standard (Brass) | **20 mm³/s** | Melt Zone Geometry |
| **40W** | **High Flow (HF)** | **24 mm³/s** | Heater Power |
| **60W** | **High Flow (HF)** | **32 mm³/s** | *Maximum Performance* |

*These values exceed official E3D ratings because Adaptive Flow actively manages thermal limitations.*

---

## 📊 Hardware Limits: 40W vs 60W

This script works by commanding **Temperature Spikes** during high-speed moves. Your heater must have **headroom** (unused power capacity).

### The Benchmark (Revo HF + PETG)

| HeaterCore | At 26mm³/s | Result |
|------------|------------|--------|
| **40W** | 100% Duty Cycle | Script can't boost — **Hard limit: 26mm³/s** |
| **60W** | ~70% Duty Cycle | Reserve power for boosts — **32+ mm³/s possible** |

**Conclusion:** To reliably print above **26 mm³/s** with High Flow, the **60W HeaterCore** is mandatory.

---

## Zero-Config Features

When using `AT_START`, several automatic systems handle configuration:

### Auto-Baseline Calibration

If no saved baseline exists, the system calibrates during the first print:
```
AUTO-CALIBRATION: Sampling baseline... (no saved value found)
AUTO-CALIBRATION: Baseline set to 16 (from 20 samples)
```

### Temperature-Based Material Detection

`AT_START` infers material from slicer temperature:

| Temperature | Detected Material |
|-------------|-------------------|
| 280°C+ | PC |
| 260-280°C | NYLON |
| 240-260°C | ABS/ASA |
| 220-240°C | PETG |
| 180-220°C | PLA |
| 160-180°C | TPU |

### Self-Learning K-Values

The system monitors thermal response and adjusts:
- **Too cold?** → Increases `speed_k` for faster response
- **Too hot?** → Decreases `speed_k` to reduce overshoot
- Learning is conservative (0.05/window) for stability

---

## Command Reference

### Core Commands

| Command | Description |
|---------|-------------|
| `AT_START` | Zero-config enable — detects material, applies settings |
| `AT_END` | Clean shutdown at print end |
| `AT_ENABLE` | Manually enable adaptive flow |
| `AT_DISABLE` | Manually disable adaptive flow |
| `AT_STATUS` | Display current status and all settings |

### Calibration Commands

| Command | Description |
|---------|-------------|
| `AT_AUTO_CALIBRATE TEMP=220` | Automatic baseline calibration |
| `AT_CHECK_BASELINE TEMP=220` | Manual baseline check |
| `AT_SET_PA MATERIAL=PLA PA=0.045` | Save calibrated PA value |
| `AT_GET_PA MATERIAL=PLA` | Show PA for material |
| `AT_LIST_PA` | List all PA values |

### Feature Controls

| Command | Description |
|---------|-------------|
| `AT_OOZE_PREVENTION ENABLE=1/0` | Toggle ooze prevention |
| `AT_SMART_RETRACTION ENABLE=1/0` | Toggle smart retraction |
| `AT_SMART_RETRACT` | Perform dynamic retraction |
| `AT_SMART_UNRETRACT` | Unretract |
| `AT_THERMAL_STATUS` | Show thermal safety status |

### Diagnostic Commands

| Command | Description |
|---------|-------------|
| `GET_EXTRUDER_LOAD` | Current TMC StallGuard value |
| `GET_PREDICTED_LOAD` | Predicted extrusion rate from lookahead |
| `SET_LOOKAHEAD E=2.5 D=0.5` | Manually add lookahead segment |
| `SET_LOOKAHEAD CLEAR` | Clear lookahead buffer |

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Driver not found" error | Verify `driver_name` matches your TMC config section exactly |
| Lookahead not working | Check logs for "intercepting G-code" message |
| Load always 0 | Install TMC Autotune; pancake motors need tuning |
| Erratic temperature | Lower lookahead boost multiplier or increase smoothing |
| Thermal warnings | Check heater wattage vs flow rate demands |

---

## Files

| File | Purpose |
|------|---------|
| `gcode_interceptor.py` | Klipper module — intercepts G-code and broadcasts to subscribers |
| `extruder_monitor.py` | Klipper module — TMC load reading + live lookahead parsing |
| `auto_flow.cfg` | Macros for adaptive temp, PA, blob detection, and lookahead boost |

---

## Compatibility

- **Hotend:** E3D Revo only (Revo HF or Revo Standard)
- **TMC Driver:** Requires StallGuard support (TMC2209, TMC2130, TMC5160)
- **Extrusion Mode:** Supports both absolute (M82) and relative (M83)
- **Platform:** Tested on Klipper with Raspberry Pi
- **Interfaces:** Works with Mainsail, Fluidd, and OctoPrint

---

## License

MIT License — See LICENSE file for details.



