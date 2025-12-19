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
   <br/> Copy auto_flow.cfg <br/>
Note: This file contains a USER CONFIGURATION section at the top. You should edit this to match your motor type (see Tuning section). <br/>
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
In your extruder section add
```
[extruder]
max_extrude_only_distance: 101.0
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
The script uses different "Flow Gates" depending on which Revo nozzle you are installed.

*   **Revo High Flow (HF):**
    Set `{% set use_high_flow_nozzle = True %}`.
    *(Boosts start at 15mm³/s to push past the ~25mm³ limit)*.

*   **Revo Standard (Brass/ObX):**
    Set `{% set use_high_flow_nozzle = False %}`.
    *(Boosts start at 8mm³/s to help the standard core push past the ~11mm³ limit)*.


## 📊 Hardware Limits: 40W vs 60W

This script functions by commanding a **Temperature Spike** during high-speed moves. For this to work, your heater must have **"Headroom"** (unused power capacity). If your heater is already running at 100% power just to maintain the base temperature, the script cannot add more heat, and the temperature will drop.

### The Revo Benchmark (PETG / 0.4mm Nozzle)

*   **Scenario A: 40W HeaterCore**
    *   At **~26 mm³/s**, the 40W heater hits 100% Duty Cycle.
    *   **Result:** The script attempts to boost the temperature, but the heater physically cannot supply more energy.
    *   **Hard Limit:** 26 mm³/s.

*   **Scenario B: 60W HeaterCore**
    *   At **~26 mm³/s**, the 60W heater is likely running at only 70% Duty Cycle.
    *   **Result:** When the script demands a boost for a 300mm/s sprint, the heater has the **reserve power** to deliver that heat instantly.
    *   **New Limit:** ~32+ mm³/s.

**Conclusion:**
To reliably print above **26 mm³/s** with this system, the **60W HeaterCore** is mandatory. The 40W core simply lacks the overhead required for dynamic temperature boosting at those flow rates.
