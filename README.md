
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

c: restart klipper <br/>
sudo systemctl restart klipper


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
### Step 3: If you use PrusaSlicer, SuperSlicer, or OrcaSlicer, put this in your "Machine Start G-code":
   ```
   PRINT_START BED={first_layer_bed_temperature[0]} EXTRUDER={first_layer_temperature[0]} MATERIAL={filament_type[0]}
```
   
### Step 4: Start Macro <br/>
Adapt your PRINT_START from my example. 
<br/>This handles the sensitivity tuning for different materials automatically.



### 🔧 Tuning Guide

## 1. Standard Tuning (K-Values)
```
These values control how aggressively the temperature boosts based on speed and resistance.

*   **Flow K (Speed Boost):**
    *   **Command:** `AT_SET_FLOW_K K=0.5`
    *   **Meaning:** For every 1mm³/s of flow, add ~0.5°C.
    *   *Tune:* Increase if you see underextrusion on long, fast walls. Decrease if you cook filament.

*   **Viscosity K (Resistance Boost):**
    *   **Command:** `AT_SET_VISC_K K=0.1`
    *   **Meaning:** If the motor works harder (strain), add temp to melt plastic faster.
    *   *Tune:* Increase if you get clicking/skipping during fast infill.

```

## 🛠️ Calibration: Finding Your Motor Baseline

The script relies on knowing what your motor "feels like" when it is free-spinning versus when it is pushing hard. It assumes a default baseline of **60** (SG_RESULT), but different motors and gear ratios will report different numbers.

**If this baseline is wrong, the Viscosity/Strain Boost will not work.**

### Step 1: Run the Test
1.  Add this temporary macro to your `printer.cfg` (you can delete it later).

```
[gcode_macro AT_CHECK_BASELINE]
description: Finds your motor's baseline load
gcode:
    {% set temp = params.TEMP|default(220)|int %}
    M117 Heating to {temp}C...
    M109 S{temp}
    M117 Extruding FAST...
    G91
    # Move faster (F1200 = 20mm/s) to trigger StallGuard
    G1 E50 F1200 
    G90
    # Read while moving
    GET_EXTRUDER_LOAD
    G4 P500
    GET_EXTRUDER_LOAD
    G4 P500
    GET_EXTRUDER_LOAD
    M117 Done.
```
Lift the Z axis or remove the filament from the hotend (so it extrudes with ZERO resistance).<br/><br/>
2. Run  **AT_CHECK_BASELINE.**<br/>
Look at the Console. You will see numbers like:<br/>
Extruder Load (SG_RESULT): 118<br/>
Extruder Load (SG_RESULT): 120<br/>
Extruder Load (SG_RESULT): 119<br/>

### Step 2: Update Config
Take the average number returned in the console (e.g., 120). <br/>
Open auto_flow.cfg and find this line inside _AUTO_TEMP_CORE:<br/>

Change:<br/>
{% set strain = 60 - corrected_load %}<br/>
To:<br/>
{% set strain = 120 - corrected_load %}<br/>
Now the script knows that 120 is "Zero Strain". Any number lower than 120 means the motor is working hard, and it will apply the boost.

### Step 3: Crash Sensitivity (Blob Detection)
If the printer triggers the "Slowing down" recovery mode randomly when there is no actual blob or tangle, your motor signal is too noisy.<br/>
Open auto_flow.cfg.<br/>
Find this logic block:<br/>
```
{% if filament_speed > 2.0 and load_delta > 20 %}
Increase the 20:
20: Standard NEMA 14 (LDO-36STH20).
30-40: Noisy/Weak motors (False positives).
10-15: Strong NEMA 17 motors (Need high sensitivity).

```



## ⚙️ Hardware Compatibility & Tuning

This system relies on physical feedback, so different hardware configurations require slightly different tuning. Here is how to adjust for your specific machine.

### 1. Motor Strength (Crash Sensitivity)
Different motors report "Load" differently. A small pancake motor (NEMA 14) fluctuates much more than a large NEMA 17.

*   **The Symptom:** If your printer triggers the "Slowing down" recovery mode randomly when there is no actual blob or tangle.
*   **The Fix:** You need to make the system *less sensitive*.
    1.  Open `auto_flow.cfg`.
    2.  Find this line: `{% if filament_speed > 2.0 and load_delta > 20 %}`
    3.  **Increase the `20`**.
        *   **20:** Standard NEMA 14 (e.g., LDO-36STH20).
        *   **30-40:** Very "noisy" or weak motors.
        *   **10-15:** Strong NEMA 17 motors (they plow through resistance, so you need high sensitivity).

### 2. Hotend Efficiency (Flow K)
The `speed_k` value controls how much temp boost is added per mm³/s of speed. This depends on your hotend's melting capacity.

*   **Standard Flow (e.g., V6, Revo, Dragon SF):**
    *   These struggle at high speeds. They need a **Higher K** (0.5 - 1.0) to compensate for thermal lag.
*   **High Flow (e.g., Rapido, Goliath, Dragon UHF):**
    *   These melt plastic very efficiently. They need a **Lower K** (0.2 - 0.4). Boosting too high will cook the filament.

*Adjust this in your `PRINT_START` macro under the `speed_k` variable.*

### 3. Toolhead Temperature (CAN Boards)
The script uses thermal compensation because stepper motors lose torque as they get hot.
*   **EBB36 / SB2209:** The script automatically looks for a sensor named `[temperature_sensor Toolhead_Temp]`. Ensure this is defined in your `printer.cfg` for maximum accuracy.
*   **Standard Wiring:** If no sensor is found, the script defaults to **35°C**. This is safe for all machines.
