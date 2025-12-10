# Klipper Adaptive Flow & Crash Guard

**A closed-loop flow control detection system for Klipper.**

This system uses the TMC driver feedback from your extruder to actively manage temperature and pressure advance It requires **no external sensors** and **no slicer modifications**.
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

3.  Add the following to your `PRINT_START` macro :

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

    
4.  Add the following to your `PRINT_END` macro :

    ```ini
    [gcode_macro PRINT_END]
    gcode:
        # 1. Disable Systems
        AT_DISABLE
        
        

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
