
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
1.  Add this temporary macro to your `printer.cfg` (you can delete it later)

```
[gcode_macro AT_CHECK_BASELINE]
description: Finds your motor's baseline load
gcode:
    {% set temp = params.TEMP|default(220)|int %}
    M117 Heating to {temp}C...
    M109 S{temp}
    M117 Extruding FAST (50mm/s)...
    G91
    # Increased speed to F3000 to wake up StallGuard
    G1 E100 F3000 
    G90
    # Read while moving
    GET_EXTRUDER_LOAD
    G4 P500
    GET_EXTRUDER_LOAD
    G4 P500
    GET_EXTRUDER_LOAD
    M117 Done.
```
1a. Add this to your Extruder section<br/>
```
    max_extrude_only_distance: 101.0
```

**Lift the Z axis or remove the filament from the hotend (so it extrudes with ZERO resistance).**<br/><br/>
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


## ⚠️ Troubleshooting Low Values
If your `AT_CHECK_BASELINE` returns very low numbers (e.g., **0 to 15**) even when spinning freely:

1.  **Lower the Noise Filter:** The script ignores strain values under 10 by default. You must lower this to allow the script to see small changes.
    *   Open `auto_flow.cfg`.
    *   Find: `{% if strain < 10 %}`
    *   Change to: `{% if strain < 2 %}`

2.  **Use the Low Baseline:** Set your baseline in `auto_flow.cfg` to match your highest average reading (e.g., **14**).
    *   Change `{% set strain = 60 - corrected_load %}` to `{% set strain = 14 - corrected_load %}`.

*Note: This is common with LDO Pancake motors. For better resolution, install **Klipper TMC Autotune** (see section below).*


## ⚡ Optional: Improving Sensor Accuracy (TMC Autotune)

If you have a **Pancake Motor** (e.g., LDO-36STH20 on a Voron CW2 or Orbiter), you might find that your `GET_EXTRUDER_LOAD` values are extremely low (0-10), even when the motor is spinning freely.

This happens because standard TMC driver settings are tuned for larger NEMA 17 motors. To fix this and get high-resolution data (40-100), you should install **Klipper TMC Autotune**.

### Installation
1.  SSH into your Raspberry Pi.
2.  Run the installer:
    ```bash
    wget https://raw.githubusercontent.com/andrewmcgr/klipper_tmc_autotune/main/install.sh
    bash install.sh
    ```
3.  Add the configuration to your `printer.cfg`.
    *(Make sure to change the `motor:` line to match your exact model)*

    ```ini
    [autotune_tmc extruder]
    motor: ldo-36sth20-1004ahg
    tuning_goal: performance
    ```
4.  **Restart Klipper** (`sudo service klipper restart`).

**Result:** Your motor will run cooler, quieter, and the Load Sensor readings will likely jump from ~5 to ~60, giving the Adaptive Flow script much more data to work with.

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


### 3. Toolhead Temperature (CAN Boards)
The script uses thermal compensation because stepper motors lose torque as they get hot.
*   **EBB36 / SB2209:** The script automatically looks for a sensor named `[temperature_sensor Toolhead_Temp]`. Ensure this is defined in your `printer.cfg` for maximum accuracy.
*   **Standard Wiring:** If no sensor is found, the script defaults to **35°C**. This is safe for all machines.



## 📝 My Findings: Tuning for Pancake Motors (LDO & Generic Clones)

If using a small NEMA 14 pancake motor (like the LDO-36STH20 or **Generic AliExpress Clones** found on Orbiter/Sherpa/CW2 extruders), the sensor readings will be much lower than standard NEMA 17 motors due to low inductance.

**Symptoms:**
*   `AT_CHECK_BASELINE` returns very low numbers (**0 to 18**) even when spinning freely.
*   Extruder clicks at high speed because the default boost is too weak to react to such a small signal.

**Required Changes:**

### 1. Update `auto_flow.cfg`
You must calibrate the script to work with the narrow signal window (0-16).

*   **Baseline:** Change `60` to your max reading (e.g., `16`).
    `{% set strain = 16 - corrected_load %}`
*   **Noise Filter:** Lower it so the script doesn't ignore the small signal.
    `{% if strain < 2 %} {% set strain = 0 %} {% endif %}`
*   **Crash Threshold:** Lower it so it can detect blobs.
    `{% if filament_speed > 2.0 and load_delta > 10 %}`

### 2. Update `PRINT_START` (High Gain Tuning)
Because the strain number is small (max 16), you need a **High K-Value** multiplier to get a meaningful temperature boost.

*   **Load K (Viscosity):** Increase from `0.1` to **0.6 - 0.8**.
    *   *Math:* Strain (16) * K (0.8) = **+12.8°C Boost**. (This effectively stops clicking).
*   **Speed K (Flow):** Increase from `0.5` to **0.8 - 1.0**.
    *   *Math:* 24mm³/s * K (0.8) = **+19.2°C Boost**. (Keeps up with high volumetric flow).
