# Klipper Adaptive Flow & Crash Guard

**A closed-loop flow control and artifact detection system for Klipper.**

This system uses the TMC driver feedback from your extruder to actively manage temperature, pressure advance, and print speed in real-time. It requires **no external sensors** and **no slicer modifications**.

---

## Features

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

## Installation

### Step 1: Install the Python Extension
This script is required to read the TMC register data.

1.  Copy `extras/extruder_monitor.py` to your Klipper extras directory:
    ```bash
    cp extras/extruder_monitor.py ~/klipper/klippy/extras/
    ```
2.  Restart the Klipper service:
    ```bash
    sudo service klipper restart
    ```

### Step 2: Install the Configuration
1.  Copy `config/auto_flow.cfg` to your configuration directory (usually `~/printer_data/config/`).
2.  Open your `printer.cfg` and add the following:

    ```ini
    [include auto_flow.cfg]

    [extruder_monitor]
    # Change this to match your specific driver (e.g., tmc2209 extruder)
    driver_name: tmc2209 extruder

    [save_variables]
    filename: ~/printer_data/config/sfs_auto_flow_vars.cfg
    ```

3.  Add the following to your `PRINT_START` macro (or use this complete example):

    ```ini
    [gcode_macro PRINT_START]
    gcode:
        ###################################################################
        # PRINT_START (Cleaned for TMC Auto-Temp + Hardcoded PA)
        ###################################################################

        # ---- 1. Get Parameters ----
        {% set target_bed       = params.BED|default(60)|int %}
        {% set target_extruder  = params.EXTRUDER|default(200)|int %}
        {% set material         = params.MATERIAL|default("PLA")|string %}

        # ---- 2. Reset Systems ----
        AT_RESET_STATE
        AT_DISABLE
        
        # ---- 3. Geometry helpers ----
        {% set x_max   = printer.toolhead.axis_maximum.x|float %}
        {% set y_max   = printer.toolhead.axis_maximum.y|float %}
        {% set x_min   = printer.toolhead.axis_minimum.x|float %}
        {% set y_min   = printer.toolhead.axis_minimum.y|float %}
        {% set x_mid   = (x_max + x_min) / 2.0 %}
        {% set y_mid   = (y_max + y_min) / 2.0 %}

        

        # ============================================================
        #  MATERIAL CONFIGURATION (HYBRID: Speed + Load)
        # ============================================================
        
        # Initialize Defaults
        {% set load_k = 0.0 %}   # TMC Reaction (The "Correction")
        {% set speed_k = 0.0 %}  # Speed Prediction (The "Sprint")
        {% set pa_val = 0.0 %}
        {% set max_temp_safety = 300 %}

        # --- LOGIC BLOCK ---
        {% if 'PLA' in material %}
            {% set load_k = 0.10 %}
            {% set speed_k = 0.50 %}
            {% set pa_val = 0.040 %}
            {% set max_temp_safety = 235 %}
            
        {% elif 'PETG' in material %}
            {% set load_k = 0.15 %}
            {% set speed_k = 0.60 %}
            {% set pa_val = 0.060 %}
            {% set max_temp_safety = 265 %}
            
        {% elif 'ABS' in material or 'ASA' in material %}
            {% set load_k = 0.20 %}
            {% set speed_k = 0.80 %}
            {% set pa_val = 0.050 %}
            {% set max_temp_safety = 290 %}
            
        {% elif 'PC' in material or 'NYLON' in material %}
            {% set load_k = 0.25 %}
            {% set speed_k = 0.90 %}
            {% set pa_val = 0.055 %}
            {% set max_temp_safety = 300 %}
            
        {% elif 'TPU' in material %}
            # Disable Auto-Temp for TPU (thermal instability can cause foaming/inconsistent extrusion)
            {% set load_k = 0.0 %}
            {% set speed_k = 0.0 %}
            {% set pa_val = 0.20 %} 
            {% set max_temp_safety = 240 %}
        {% endif %}

        # --- APPLY PA ---
        {% if pa_val > 0 %}
            SET_PRESSURE_ADVANCE ADVANCE={pa_val}
        {% endif %}

        # --- APPLY AUTO-TEMP ---
        {% if load_k > 0 or speed_k > 0 %}
            AT_SET_VISC_K K={load_k}          # Set TMC Sensitivity
            AT_SET_FLOW_K K={speed_k}         # Set Speed Sensitivity
            AT_SET_MAX MAX={max_temp_safety}  # Set Safety Ceiling
            AT_ENABLE
            RESPOND MSG="Material: {material} | PA: {pa_val} | Load_K: {load_k} | Speed_K: {speed_k}"
        {% else %}
            AT_DISABLE
            RESPOND MSG="Material: {material} | PA: {pa_val} | Auto-Temp OFF"
        {% endif %}
        

        # ---- 8. Purge Line ----
        M117 Purge lines…
        SAVE_GCODE_STATE NAME=PRIMELINE
        G90
        G92 E0
        G1 X{ (x_min + 30) if (x_min + 30) > 30 else 30 } Y{y_min + 5} Z0.3 F12000
        G91
        G1 X220 E18 F1200
        G1 Y1 F6000
        G1 X-220 E9 F1200
        G90
        G92 E0
        RESTORE_GCODE_STATE NAME=PRIMELINE

        M117 Printing…
    ```

4.  Add the following to your `PRINT_END` macro (or use this complete example):

    ```ini
    [gcode_macro PRINT_END]
    gcode:
        # 1. Disable Systems
        AT_DISABLE
        SET_FILAMENT_SENSOR SENSOR=SFS_T0 ENABLE=0
        TURN_OFF_HEATERS
        M107

        # 2. Safe Z Raise
        {% set max_z = printer.toolhead.axis_maximum.z|float %}
        {% set act_z = printer.toolhead.position.z|float %}
        {% if act_z < (max_z - 20) %}
            {% set z_safe = 20.0 %}
        {% else %}
            {% set z_safe = max_z - act_z %}
        {% endif %}
        
        # 3. Retract & Move
        G91
        G1 E-5 F3600
        G0 Z{z_safe} F3600
        G90
        G0 X{printer.toolhead.axis_maximum.x - 10} Y{printer.toolhead.axis_maximum.y - 10} F12000
        
        # 4. Motors Off
        M84 X Y E
        M117 Print Complete.
    ```

### Step 3: Configure Your Slicer (Orca Slicer)
Add the following to your **Machine G-code** settings in Orca Slicer (under Printer Settings → Machine G-code → Machine start G-code):

```gcode
PRINT_START BED={first_layer_bed_temperature[0]} EXTRUDER={first_layer_temperature[0]} MATERIAL={filament_type[0]}
```

This passes the bed temperature, extruder temperature, and filament type from the slicer to your `PRINT_START` macro.

---

## Tuning & Usage

### 1. Flow K (Speed Boost)
How much temp to add based on speed.

**Command:** `AT_SET_FLOW_K K=0.5`

**Meaning:** For every 1mm³/s of flow, add ~0.5°C.

### 2. Viscosity K (Resistance Boost)
How much temp to add if the extruder is struggling (high load).

**Command:** `AT_SET_VISC_K K=0.1`

**Meaning:** If strain increases, boost temp to melt plastic faster.

### 3. Max Temperature Limit
Set the maximum temperature ceiling for safety.

**Command:** `AT_SET_MAX MAX=300`

**Meaning:** Prevents the auto-temp system from exceeding this temperature.

### 4. Crash Sensitivity
To adjust how sensitive the crash detection is, edit `auto_flow.cfg`:

*   `load_delta > 20`: Lower this number to make it more sensitive (detect smaller blobs). Raise it if you get false positives.

---

## Requirements

*   Klipper Firmware
*   TMC Stepper Driver on Extruder (TMC2209, TMC2130, TMC5160)
*   `[save_variables]` enabled
