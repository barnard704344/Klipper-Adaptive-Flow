# Klipper Adaptive Flow & Crash Guard (E3D Revo Edition)

**A closed-loop flow control and artifact detection system for Klipper.**

This system uses TMC driver feedback to actively manage temperature, pressure advance, and print speed. It is specifically tuned to overcome the thermal limitations of **E3D Revo** hotends using **Voron/Sherpa** style extruders.

## ⚠️ Hardware Requirements

This script is tuned for the following hardware ecosystem.

*   **Hotend:** E3D Revo (Standard or High Flow nozzle).
*   **HeaterCore:** 40W (Standard Speed) or 60W (High Speed/High Flow).
*   **Extruder:** Voron StealthBurner (CW2), Sherpa Mini, or Orbiter.
*   **Motor:** NEMA 14 Pancake (LDO-36STH20 or Generic).
*   **Electronics:** BTT EBB36 / SB2209 (CAN Bus) or similar with TMC2209.

---

## ✨ Features

### 1. Revo-Optimized Temp Boosting
Automatically raises the temperature as flow rate increases.
*   **Predictive Acceleration:** Detects rapid speed changes and "kicks" the heater to overcome thermal lag.
*   **Flow Gates:** Automatically ignores slow moves to prevent oozing.
    *   **Standard Nozzle:** Boosts start > 8mm³/s.
    *   **High Flow Nozzle:** Boosts start > 15mm³/s.

### 2. Burst Protection (Noise Filtering)
High-speed printing often involves tiny, rapid movements like **Gap Infill** or **Small Text**.
*   **The Problem:** Without filtering, the script sees a 0.1-second spike to 300mm/s and immediately commands a +20°C boost. Since the move is over before the heater can react, the nozzle just overheats while idle.
*   **The Solution:** The script applies a **Rolling Weighted Average** to the flow calculation.
    *   Sustained speed (long walls/infill) triggers the full boost.
    *   Short bursts (< 0.5s) are smoothed out and ignored.
    *   This ensures the heater only reacts to moves long enough to benefit from the extra energy.
      
### 3. Extrusion Crash Detection
Monitors the extruder motor for resistance spikes (blobs/tangles). If >3 spikes occur in one layer, the printer slows to 50% speed for 3 layers to recover.

### 4. Smart Cornering ("Sticky Heat")
High-speed printing often suffers from **Bulging Corners**. This happens because when the print head brakes for a corner, the pressure in the nozzle doesn't drop instantly.
*   **The Problem:** Standard logic cools the nozzle when speed drops. Cooler plastic = Higher Viscosity = More Bulging.
*   **The Fix:** This script uses **Asymmetric Smoothing**. It heats up *instantly* to match acceleration but cools down *very slowly*. This keeps the plastic fluid during corner braking, allowing the extruder to relieve pressure effectively.

### 5. Dynamic Pressure Advance
Works in tandem with Smart Cornering. As the temperature boosts, the plastic becomes more fluid (lower viscosity).
*   **The Logic:** Hotter plastic requires **less** Pressure Advance to control.
*   **The Action:** The script automatically **lowers** your PA value as the temperature rises. This prevents the "gaps" or "shredded corners" that occur when you apply high-speed PA values to super-heated plastic.
---



## 📦 Installation

### Step 1: Install the Python Extension
This script is required to read the TMC register data directly.

a. Create a file named `extruder_monitor.py` in your extras directory: `~/klipper/klippy/extras/extruder_monitor.py` <br/>
    - Copy the extruder_monitor.py

b. Install the Configuration <br/>
    - Create a file named `auto_flow.cfg` in your config directory: `~/printer_data/config/auto_flow.cfg` <br/>
    - Copy auto_flow.cfg 
    
c. Restart klipper <br/>
    - `sudo systemctl restart klipper`


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
<br/>.



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
The script automatically sets the "Flow Gate" based on your nozzle type to prevent boosting when it isn't needed.

1.  Open `auto_flow.cfg`.
2.  Find `use_high_flow_nozzle` at the top.

*   **Revo High Flow (HF):**
    Set `{% set use_high_flow_nozzle = True %}`.
    *(Sets gate to 15mm³/s)*.

*   **Revo Standard (Brass/ObX):**
    Set `{% set use_high_flow_nozzle = False %}`.
    *(Sets gate to 8mm³/s)*.


## 🌡️ Recommended Base Temperatures

When using this script, set your Slicer temperature to a standard **"Quality"** temperature (what you would use for slow perimeters or bridges).

**Do not** set your slicer to a high-speed temperature (e.g., don't set PETG to 270°C). If you do, the script will try to boost on top of that, hitting the 300°C safety limit and causing errors.

| Material | Slicer Base Temp | Max Safety Cap (`PRINT_START`) | Notes |
| :--- | :--- | :--- | :--- |
| **PLA** | **210°C** | 235°C | Best balance of cooling vs flow. |
| **PETG** | **240°C - 245°C** | 275°C | 245°C ensures good layer bond at low speeds. |
| **ABS / ASA** | **250°C** | 290°C | Needs heat. Boost takes it to ~275°C for strength. |
| **PC / Nylon** | **270°C** | 300°C | **Warning:** Revo max is 300°C. Ensure `max_temp` in config allows this. |
| **TPU** | **230°C** | 240°C | Auto-Flow is usually disabled for TPU to prevent foaming. |    

> [!NOTE]
> **Manufacturer Ratings vs. High Speed:**
> If your filament box says "210-230°C", pick the **highest number (230°C)** as your Base Temp in the slicer.
>
> High-speed printing reduces the time the plastic has to absorb heat. Using the lower end of the rating will cause cold-extrusion/delamination on High Flow hotends like the Revo.



## ✂️ Slicer Configuration

Because this system manages Pressure Advance dynamically, you must configure your slicer specifically to avoid conflicts.

### 1. Pressure Advance (Critical)
**Disable Pressure Advance in your Slicer.**
*   **Orca Slicer:** Set "Pressure Advance" to `0` in Filament Settings.
*   **Prusa Slicer:** Remove any `M572` or `SET_PRESSURE_ADVANCE` commands from custom G-code.

**Why?**
The script defines the **Base PA** inside the `AT_INIT_MATERIAL` macro (found in `auto_flow.cfg`).
1.  Open `auto_flow.cfg`.
2.  Scroll to the bottom (`AT_INIT_MATERIAL`).
3.  Update the `pa_val` for each material to match your calibrated values.
    *   *Example:* If your calibrated PA for PETG is 0.055, change `{% set pa_val = 0.060 %}` to `0.055`.

### 2. Max Volumetric Speed (Safety Caps)
To ensure the script works within safe physical limits, set the **Max Volumetric Speed** in your Slicer's Filament Settings based on your hardware combination:

| Heater Core | Nozzle Type | Recommended Slicer Limit | Bottleneck |
| :--- | :--- | :--- | :--- |
| **40W** | Standard (Brass) | **18 mm³/s** | Melt Zone Geometry |
| **60W** | Standard (Brass) | **20 mm³/s** | Melt Zone Geometry |
| **40W** | **High Flow (HF)** | **24 mm³/s** | Heater Power (Watts) |
| **60W** | **High Flow (HF)** | **32 mm³/s** | *Maximum Performance* |

*Note: These values are higher than official E3D ratings because the Adaptive Flow script actively overrides thermal limitations.*



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
