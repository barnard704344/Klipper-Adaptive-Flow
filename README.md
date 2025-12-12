
# Klipper Adaptive Flow & Crash Guard

**A closed-loop flow control and artifact detection system for Klipper.**

This system uses the TMC driver feedback from your extruder to actively manage temperature, pressure advance, and print speed in real-time. It requires **no external sensors** and **no slicer modifications**.

## ✨ Features

### 1. 🌊 Hydro-Dynamic Temp Boosting
Automatically raises the Hotend Temperature as flow rate increases (Feed-Forward). This compensates for the thermal lag of the heater block during high-speed moves.

### 2. 🛡️ Extrusion Crash Detection
Monitors the extruder motor for sudden resistance spikes (Load Deltas).
*   **Blobs/Tangles/Clogs:** If the nozzle hits a blob or the filament tangles, the resistance spikes.
*   **Automatic Recovery:** If >3 spikes are detected in a single layer, the system automatically slows the print speed to **50%** for the next 3 layers to allow the print to recover, then restores full speed.

### 3. 🧠 Smart Cornering ("Sticky Heat")
Prevents the "Bulging Corner" issue common with other auto-temp scripts.
*   The script heats up fast but cools down *very slowly*.
*   This ensures the plastic remains fluid during the deceleration phase of a corner, preventing internal pressure buildup.

### 4. 📐 Dynamic Pressure Advance
Automatically **lowers** Pressure Advance (PA) as the temperature rises.
*   Hotter plastic is more fluid and requires less PA.
*   This prevents "gaps" or "cutting corners" caused by aggressive PA at high temperatures.

### 5. 👁️ Machine-Side Layer Watcher
Uses a Z-height monitor to detect layer changes automatically. You do not need to add custom G-Code to your Slicer.

---

## 📦 Installation

### Step 1: Install the Python Extension
This script is required to read the TMC register data directly.

a.  Create a file named `extruder_monitor.py` in your extras directory: `~/klipper/klippy/extras/extruder_monitor.py`
    <br/>Copy the extruder_monitor.py

b: Install the Configuration <br/>
Create a file named auto_flow.cfg in your config directory: ~/printer_data/config/auto_flow.cfg
   <br/> Copy auto_flow.cfg

c: restart klipper 

### Step 2: Edit printer.cfg
Open your printer.cfg and add the following lines.
```
[include auto_flow.cfg]

[extruder_monitor]
# IMPORTANT: Change this to match your actual driver section!
# Examples: "tmc2209 extruder" or "tmc2209 stepper_e" or "tmc5160 extruder"
driver_name: tmc2209 extruder

[save_variables]
filename: ~/printer_data/config/sfs_auto_flow_vars.cfg
```
 --- Toolhead Temp ---<br/>
 If using EBB36/SB2209, name your temp sensor "Toolhead_Temp" for better accuracy.
```
[temperature_sensor Toolhead_Temp]
sensor_type: temperature_mcu
sensor_mcu: EBBCan
```

In your TMC section of your extruder place the following
```
[tmc2209 extruder]
run_current: 0.650
stealthchop_threshold: 0
driver_SGTHRS: 120
```
###Step 3: update your machine settings Gcode in orca:
   ```
   PRINT_START BED={first_layer_bed_temperature[0]} EXTRUDER={first_layer_temperature[0]} MATERIAL={filament_type[0]}
```
   
🚀 Usage <br/>
Start Macro <br/>
Adapt your PRINT_START from my example. <br/>This handles the sensitivity tuning for different materials automatically.

#🔧 Tuning Guide
```
1. Flow K (Speed Boost)
How much temp to add based on speed.
Command: AT_SET_FLOW_K K=0.5
Meaning: For every 1mm³/s of flow, add ~0.5°C.

2. Viscosity K (Resistance Boost)
How much temp to add if the extruder is struggling (high load).
Command: AT_SET_VISC_K K=0.1
Meaning: If strain increases, boost temp to melt plastic faster.

3. Crash Sensitivity
To adjust how sensitive the crash detection is, edit auto_flow.cfg. Look for the logic block:
{% if filament_speed > 2.0 and load_delta > 20 %}
```
20: Lower this number to make it more sensitive (detect smaller blobs). Raise it if you get false positives.
code
Code
