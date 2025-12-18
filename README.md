# Klipper Adaptive Flow & Crash Guard (E3D Revo Edition)

**A closed-loop flow control system tuned specifically for the E3D Revo ecosystem.**

This system uses TMC driver feedback to actively manage temperature, pressure advance, and print speed. It allows **Revo High Flow** nozzles to push past their rated limits safely, and makes **Standard Revo** nozzles behave like high-flow hotends during fast moves.

## ⚠️ Hardware Requirements

This script is **only** tested and verified on the following hardware. Using other hotends (Dragon, Rapido, V6) is **not supported** because the thermal boost curves are calibrated specifically for Revo heater cores.

*   **Hotend:** E3D Revo (Standard or High Flow nozzle).
*   **HeaterCore:** 40W (Standard Speed) or 60W (High Speed/High Flow).
*   **Extruder:** Voron StealthBurner (CW2), Sherpa Mini, or Orbiter.
*   **Motor:** NEMA 14 Pancake (LDO-36STH20 or Generic).
*   **Electronics:** BTT EBB36 / SB2209 (CAN Bus) or similar with TMC2209.

---

## ✨ Features

### 1. Revo-Optimized Temp Boosting
Automatically raises the temperature as flow rate increases. The script includes specific "Flow Gates" calculated for Revo geometry:
*   **Standard Nozzle:** Boosts start at **8mm³/s** (Breaking the 11mm³ limit).
*   **High Flow Nozzle:** Boosts start at **15mm³/s** (Breaking the 25mm³ limit).

### 2. Extrusion Crash Detection
Monitors the extruder motor for resistance spikes (blobs/tangles). If >3 spikes occur in one layer, the printer slows to 50% speed for 3 layers to recover.

### 3. Smart Cornering ("Sticky Heat")
Prevents "Bulging Corners". The script heats up instantly but cools down *slowly*, ensuring plastic remains fluid during corner braking.

### 4. Dynamic Pressure Advance
Automatically **lowers** Pressure Advance (PA) as the temperature rises, preventing gaps in corners.

---

## 📦 Installation

### Step 1: Install the Python Extension
This script is required to read the TMC register data directly.

a.  Create a file named `extruder_monitor.py` in your extras directory: `~/klipper/klippy/extras/extruder_monitor.py`
    <br/>Copy the extruder_monitor.py

b: Install the Configuration <br/>
Create a file named auto_flow.cfg in your config directory: ~/printer_data/config/auto_flow.cfg
   <br/> Copy auto_flow.cfg
Note: This file contains a USER CONFIGURATION section at the top. You should edit this to match your motor type (see Tuning section).
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



## ⚙️ Configuration & Tuning

All settings are located at the **top** of the `auto_flow.cfg` file in the `USER CONFIGURATION` block.

### Step 1: Optimize Motor Drivers (Highly Recommended)
NEMA 14 "Pancake" motors (LDO-36STH20) have very low inductance. Standard Klipper settings often cause them to run hot or report "0" load.

**Install Klipper TMC Autotune** to fix the electrical timing. While this **may not increase the load number** significantly on pancake motors, it ensures the reading is stable and the motor has maximum torque.

1.  **Install via SSH:**
    ```bash
    wget https://raw.githubusercontent.com/andrewmcgr/klipper_tmc_autotune/main/install.sh
    bash install.sh
    ```
2.  **Update `printer.cfg`:**
    Add this section (ensure `motor:` matches your specific model):
    ```ini
    [autotune_tmc extruder]
    motor: ldo-36sth20-1004ahg
    tuning_goal: performance
    ```
3.  **Restart Klipper:** `sudo service klipper restart`

---

### Step 2: Calibrate Motor Baseline
We need to tell the script what "Zero Load" looks like for your specific motor.

1.  Heat your nozzle to printing temp.
2.  Unload filament (or lift Z high so it extrudes into air).
3.  Run the command: `AT_CHECK_BASELINE`.
4.  Note the **Extruder Load** number printed in the console.
    *   **Pancake Motors (LDO/Orbiter/Sherpa):** Expect low numbers (**12 - 20**). This is normal.
    *   **Standard NEMA 17:** Expect higher numbers (**60 - 100**).
5.  **Edit `auto_flow.cfg`**:
    Update the `sensor_baseline` variable to match your number.
    ```ini
    {% set sensor_baseline = 16 %}
    ```

---

### Step 3: Select Nozzle Type
The script uses different "Flow Gates" depending on your hotend geometry.

*   **Revo High Flow / Rapido / Volcano:**
    Set `{% set use_high_flow_nozzle = True %}`.
    *(Boosts start at 15mm³/s)*.

*   **Standard Revo / V6 / Dragon SF:**
    Set `{% set use_high_flow_nozzle = False %}`.
    *(Boosts start at 8mm³/s to help the standard core keep up)*.

## 📊 Hardware Limits & Benchmarks

The Adaptive Flow script works by demanding **extra heat** during fast moves. However, it cannot create energy that your hardware is unable to supply. If your heater hits 100% duty cycle, the temperature will drop regardless of what the script commands.

### Case Study: E3D Revo High Flow (40W)
*   **Setup:** Revo HF 0.4mm Nozzle + **40W** HeaterCore.
*   **Material:** PETG.
*   **Script Settings:** Standard Auto-Flow defaults.
*   **Result:** Flow remains stable up to **~26 mm³/s**.
*   **The Ceiling:** Above 26 mm³/s, the 40W heater saturates (hits 100% power). It physically cannot generate enough heat to maintain the temperature, let alone boost it.

**Conclusion:**
To push reliably past **30 mm³/s** while using this script, a **60W HeaterCore** is required. The 60W core provides the necessary "headroom" for the script to apply Turbo/Boost commands during high-speed maneuvers without bottoming out the temperature.
