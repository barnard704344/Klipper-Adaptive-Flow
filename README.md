# Klipper Adaptive Flow

**A closed-loop flow control system for Klipper using G-code lookahead and live velocity data.**

This system dynamically adjusts nozzle temperature based on real-time extrusion flow rate and upcoming G-code moves. It is specifically tuned for **E3D Revo** hotends.



---

## Why Use This?

**The Problem:** Standard 3D printing uses a fixed hotend temperature. But filament viscosity and required melt energy change constantly based on:
- How fast you're extruding (infill vs perimeters vs small details)
- Upcoming flow changes the slicer has planned
- Acceleration and deceleration phases

Running too cold = under-extrusion, weak layer adhesion, clogs.  
Running too hot = stringing, oozing, heat creep, burned filament.

**The Solution:** This system monitors real-time extrusion velocity and looks ahead at upcoming G-code to **dynamically adjust temperature** — hotter for high-flow sections, cooler for fine details.

### What You Get

| Benefit | How It Works |
|---------|--------------|
| **Better print quality** | Temperature matched to actual flow demand reduces under-extrusion and stringing |
| **Faster prints** | Push higher flow rates without quality loss — the system compensates automatically |
| **Zero configuration** | Detects material from temp, sets optimal K-values automatically |
| **Set and forget** | Just add `AT_START` to your slicer — no per-print tuning needed |
| **Works with any slicer** | Temperature-based material detection means no slicer plugins required |
| **Self-improving** | K-values automatically tune themselves based on your printer's thermal response |

### Who Is This For?

- ✅ You have an **E3D Revo** hotend (HF or Standard)
- ✅ You want **Bambu-style automatic temperature control** on your Klipper printer
- ✅ You print a variety of materials and want consistent quality without manual tuning
- ✅ You push high flow rates and fight under-extrusion at speed

### Who Is This NOT For?

- ❌ Non-Revo hotends (different thermal mass = different K-values needed)
- ❌ Users who prefer fully manual control

---

## ⚠️ Hardware Requirements

This script is tuned for the following hardware:

| Component | Supported Options |
|-----------|-------------------|
| **Hotend** | E3D Revo (Standard or High Flow nozzle) |
| **HeaterCore** | 40W (Standard) or 60W (High Speed/High Flow) |
| **Extruder** | Voron StealthBurner (CW2), Sherpa Mini, Orbiter, or similar |

**No TMC/StallGuard requirement** — The system uses Klipper's live velocity data and G-code lookahead, not motor load sensing.

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
4. **Print!** — No calibration required.

The system will automatically:
- **Detect material** from your slicer's temperature setting
- **Apply optimal K-values** and pressure advance for that material
- **Self-learn** over time, adjusting K-values based on thermal response

That's it. No `AT_INIT_MATERIAL`, no calibration wizards, no manual baseline setting.

---

## ✨ Features

### 1. Revo-Optimized Temp Boosting
Automatically raises the temperature as flow rate increases.
- **Predictive Acceleration:** Detects rapid speed changes and "kicks" the heater to overcome thermal lag.
- **Flow Gates:** Ignores very slow moves to prevent unnecessary heating.
  - **Standard Nozzle:** Boosts start > 2mm³/s.
  - **High Flow Nozzle:** Boosts start > 3mm³/s.

### 2. Burst Protection (Noise Filtering)
High-speed printing often involves tiny, rapid movements like **Gap Infill**.
- **The Problem:** Without filtering, the script sees a 0.1-second spike to 300mm/s and immediately commands a +20°C boost. Since the move is over before the heater can react, the nozzle just overheats while idle.
- **The Solution:** The script applies a **Rolling Weighted Average** to the flow calculation.
  - Sustained speed (long walls/infill) triggers the full boost.
  - Short bursts (< 0.5s) are smoothed out and ignored.
  - This ensures the heater only reacts to moves long enough to benefit from the extra energy.

### 3. Smart Cornering ("Sticky Heat")
High-speed printing often suffers from **Bulging Corners**. This happens because when the print head brakes for a corner, the pressure in the nozzle doesn't drop instantly.
- **The Problem:** Standard logic cools the nozzle when speed drops. Cooler plastic = Higher Viscosity = More Bulging.
- **The Fix:** This script uses **Asymmetric Smoothing**. It heats up *instantly* to match acceleration but cools down *very slowly*. This keeps the plastic fluid during corner braking, allowing the extruder to relieve pressure effectively.

### 4. Dynamic Pressure Advance
Works in tandem with Smart Cornering. As the temperature boosts, the plastic becomes more fluid (lower viscosity).
- **The Logic:** Hotter plastic requires **less** Pressure Advance to control.
- **The Action:** The script automatically **lowers** your PA value as the temperature rises. This prevents the "gaps" or "shredded corners" that occur when you apply high-speed PA values to super-heated plastic.

### 5. Live G-code Lookahead
The system parses upcoming G-code moves to predict flow changes *before* they happen.
- **Proactive Boosting:** Raises temperature before high-flow sections arrive
- **Smoother Transitions:** Eliminates under-extrusion at flow ramp-ups

### 6. Thermal Safety
Built-in runaway protection with emergency shutdown if temperature exceeds safe limits.

### 7. Self-Learning K-Values
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

[save_variables]
filename: ~/printer_data/config/adaptive_flow_vars.cfg
```

**Toolhead Temperature Sensor (recommended for EBB36/SB2209):**
```ini
[temperature_sensor Toolhead_Temp]
sensor_type: temperature_mcu
sensor_mcu: EBBCan
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

### Select Nozzle Type

Edit `auto_flow.cfg`:
- **Revo High Flow:** `variable_use_high_flow_nozzle: True`
- **Revo Standard:** `variable_use_high_flow_nozzle: False`

---

## ✂️ Slicer Configuration

### Pressure Advance — Disable It

The script manages PA dynamically. **Set PA to 0 in your slicer.**

Store your calibrated values in Klipper instead:
```gcode
AT_SET_PA MATERIAL=PLA PA=0.045
```

### Temperatures — Use "Quality" Settings

Set your slicer to normal quality temperatures. The script boosts automatically during high-speed moves.

| Material | Slicer Temp | Script Boosts To |
|----------|-------------|------------------|
| PLA | 210°C | up to 235°C |
| PETG | 245°C | up to 275°C |
| ABS/ASA | 250°C | up to 290°C |

### Max Volumetric Speed

| Setup | Slicer Limit |
|-------|--------------|
| 40W + Standard Nozzle | 17 mm³/s |
| 60W + Standard Nozzle | 20 mm³/s |
| 40W + High Flow | 24 mm³/s |
| 60W + High Flow | 32 mm³/s |

> **Note:** For speeds above 26 mm³/s, the 60W HeaterCore is required — the 40W runs at 100% duty cycle and has no headroom for temperature boosts.

---

## Zero-Config Features

When using `AT_START`, several automatic systems handle configuration:

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

### PA Commands

| Command | Description |
|---------|-------------|
| `AT_SET_PA MATERIAL=PLA PA=0.045` | Save calibrated PA value |
| `AT_GET_PA MATERIAL=PLA` | Show PA for material |
| `AT_LIST_PA` | List all PA values |

### Feature Controls

| Command | Description |
|---------|-------------|
| `AT_THERMAL_STATUS` | Show thermal safety status |

### Diagnostic Commands

| Command | Description |
|---------|-------------|
| `GET_PREDICTED_LOAD` | Predicted extrusion rate from lookahead |
| `SET_LOOKAHEAD E=2.5 D=0.5` | Manually add lookahead segment |
| `SET_LOOKAHEAD CLEAR` | Clear lookahead buffer |

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Lookahead not working | Check logs for "intercepting G-code" message |
| Erratic temperature | Lower lookahead boost multiplier or increase smoothing |
| Thermal warnings | Check heater wattage vs flow rate demands |
| No temperature boost | Verify flow exceeds 2mm³/s (std) or 3mm³/s (HF) |

---

## Files

| File | Purpose |
|------|---------|
| `extruder_monitor.py` | Klipper module — G-code lookahead parsing and flow prediction |
| `auto_flow.cfg` | Macros for adaptive temp, PA, and lookahead boost |

---

## Compatibility

- **Hotend:** E3D Revo only (Revo HF or Revo Standard)
- **Extrusion Mode:** Supports both absolute (M82) and relative (M83)
- **Platform:** Tested on Klipper with Raspberry Pi
- **Interfaces:** Works with Mainsail, Fluidd, and OctoPrint

---

## License

MIT License — See LICENSE file for details.



