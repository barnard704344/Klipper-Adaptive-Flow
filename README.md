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

1.  Create a file named `extruder_monitor.py` in your extras directory: `~/klipper/klippy/extras/extruder_monitor.py`
2.  Paste the content below into it.

<details>
<summary>Click to view <b>extruder_monitor.py</b></summary>

```python
import logging

class ExtruderMonitor:
    def __init__(self, config):
        self.printer = config.get_printer()
        config.get("stepper", "extruder")
        self.driver_name = config.get("driver_name", "tmc2209 extruder")
        self.printer.register_event_handler("klippy:connect", self.handle_connect)

    def handle_connect(self):
        try:
            self.tmc = self.printer.lookup_object(self.driver_name)
        except Exception as e:
             raise self.printer.config_error(f"ExtruderMonitor: Driver '{self.driver_name}' not found. Check printer.cfg.")

        gcode = self.printer.lookup_object('gcode')
        gcode.register_command("GET_EXTRUDER_LOAD", self.cmd_GET_EXTRUDER_LOAD,
                               desc="Get current extruder load")

    def cmd_GET_EXTRUDER_LOAD(self, gcmd):
        try:
            mcu_tmc = self.tmc.mcu_tmc
            val = mcu_tmc.get_register('SG_RESULT')
            if not isinstance(val, int):
                val = int(val)
            gcmd.respond_info(f"Extruder Load (SG_RESULT): {val}")
        except Exception as e:
            try:
                # Fallback for some TMC implementations
                val = self.tmc.get_register('SG_RESULT')
                gcmd.respond_info(f"Extruder Load (via Helper): {val}")
            except:
                gcmd.respond_info(f"Error reading TMC: {str(e)}")

    def get_status(self, eventtime):
        try:
            if hasattr(self, 'tmc'):
                val = self.tmc.mcu_tmc.get_register('SG_RESULT')
                return {'load': int(val)}
        except:
            pass
        return {'load': -1}

def load_config(config):
    return ExtruderMonitor(config)
</details>
[!IMPORTANT]
You must restart the Klipper service after adding this file.
sudo service klipper restart
Step 2: Install the Configuration
Create a file named auto_flow.cfg in your config directory: ~/printer_data/config/auto_flow.cfg
Paste the content below into it.
<details>
<summary>Click to view <b>auto_flow.cfg</b></summary>
code
Ini
[save_variables]
filename: ~/printer_data/config/sfs_auto_flow_vars.cfg

# =========================================================
#  CORE LOGIC LOOP (Runs every 1.0s)
# =========================================================
[gcode_macro _AUTO_TEMP_CORE]
description: Auto-Temp | Blob Detect | Dynamic PA | Z-Watcher
variable_current_boost: 0.0
variable_last_load_val: 0
variable_layer_crash_count: 0
variable_last_stable_z: 0.0
variable_base_pa: 0.0
variable_slowdown_layers_left: 0
variable_slowdown_active: False
gcode:
    {% set vars = printer.save_variables.variables %}
    {% set current_setpoint = printer.extruder.target|float %}
    {% set saved_base = vars.base_print_temp|default(0)|float %}
    
    {% if saved_base >= 170 and vars.at_enabled|default(False) %}
    
        # 1. LAYER WATCHER
        {% set current_z = printer.toolhead.position.z|float %}
        {% set filament_speed = printer.motion_report.live_extruder_velocity|float %}
        {% set last_z = printer["gcode_macro _AUTO_TEMP_CORE"].last_stable_z %}
        
        {% if current_z > (last_z + 0.05) and filament_speed > 0 %}
            _AT_EVALUATE_LAYER
            SET_GCODE_VARIABLE MACRO=_AUTO_TEMP_CORE VARIABLE=last_stable_z VALUE={current_z}
        {% endif %}

        # 2. BLOB/CRASH DETECTION
        {% set load_val = printer["extruder_monitor"].load|default(0)|int %}
        {% set last_load = printer["gcode_macro _AUTO_TEMP_CORE"].last_load_val %}
        {% set load_delta = (last_load - load_val)|abs %}

        # Sensitivity Threshold (Speed > 2mm/s, Load Delta > 20)
        {% if filament_speed > 2.0 and load_delta > 20 %}
            {% set current_crashes = printer["gcode_macro _AUTO_TEMP_CORE"].layer_crash_count %}
            SET_GCODE_VARIABLE MACRO=_AUTO_TEMP_CORE VARIABLE=layer_crash_count VALUE={current_crashes + 1}
        {% endif %}
        
        SET_GCODE_VARIABLE MACRO=_AUTO_TEMP_CORE VARIABLE=last_load_val VALUE={load_val}

        # 3. BOOST CALCULATION
        {% set vol_flow = filament_speed * 2.405 %} 
        {% set speed_k = vars.material_flow_k|default(0.0)|float %}
        {% set speed_boost = vol_flow * speed_k %}

        {% set board_temp = printer["temperature_sensor Toolhead_Temp"].temperature|default(30)|float %}
        {% set corrected_load = load_val + ((board_temp - 35.0) * 0.4) %}
        {% if corrected_load < 0 %} {% set corrected_load = 0 %} {% endif %}
        {% set strain = 60 - corrected_load %}
        {% if strain < 10 %} {% set strain = 0 %} {% endif %}
        {% set load_boost = strain * vars.material_viscosity_k|default(0.0)|float %}

        # 4. STICKY SMOOTHING (Prevents Corner Artifacts)
        {% set raw_boost_target = speed_boost + load_boost %}
        {% if raw_boost_target > 30.0 %} {% set raw_boost_target = 30.0 %} {% endif %}
        
        {% set last_boost = printer["gcode_macro _AUTO_TEMP_CORE"].current_boost %}
        
        {% if raw_boost_target >= last_boost %}
            # Heat up fast (80% weight)
            {% set smooth_boost = (last_boost * 0.2) + (raw_boost_target * 0.8) %}
        {% else %}
            # Cool down slow (Linear Decay) to hold heat in corners
            {% set possible_drop = last_boost - 0.2 %}
            {% if possible_drop < raw_boost_target %}
                {% set smooth_boost = raw_boost_target %}
            {% else %}
                {% set smooth_boost = possible_drop %}
            {% endif %}
        {% endif %}
        
        SET_GCODE_VARIABLE MACRO=_AUTO_TEMP_CORE VARIABLE=current_boost VALUE={smooth_boost}
        
        # 5. APPLY WITH DEADBANDS
        {% set new_target = (saved_base + smooth_boost)|round(0)|int %}
        {% set max_limit = vars.at_max_temp|default(300)|int %}
        {% if new_target > max_limit %} {% set new_target = max_limit %} {% endif %}

        {% if load_val != -1 %}
            {% set temp_diff = (new_target - current_setpoint)|abs %}
            {% if temp_diff >= 2 %} M104 S{new_target} {% endif %}
        {% endif %}

        {% set base_pa = printer["gcode_macro _AUTO_TEMP_CORE"].base_pa|default(0.0)|float %}
        {% if base_pa > 0 %}
            # PA Compensation: Reduce PA by 1% per 1C of boost
            {% set pa_multiplier = 1.0 - (smooth_boost * 0.01) %}
            {% if pa_multiplier < 0.5 %} {% set pa_multiplier = 0.5 %} {% endif %}
            {% set new_pa = base_pa * pa_multiplier %}
            {% set current_pa = printer.extruder.pressure_advance|float %}
            
            {% if (current_pa - new_pa)|abs > 0.01 %}
                SET_PRESSURE_ADVANCE ADVANCE={new_pa}
            {% endif %}
        {% endif %}
        
    {% else %}
        SET_GCODE_VARIABLE MACRO=_AUTO_TEMP_CORE VARIABLE=current_boost VALUE=0.0
        {% if not vars.at_enabled|default(False) %}
            UPDATE_DELAYED_GCODE ID=AUTO_TEMP_LOOP DURATION=0
        {% endif %}
    {% endif %}

[gcode_macro _AT_EVALUATE_LAYER]
description: Checks crash count on layer change
gcode:
    {% set macro_vars = printer["gcode_macro _AUTO_TEMP_CORE"] %}
    {% set crashes = macro_vars.layer_crash_count %}
    {% set CRASH_LIMIT = 3 %} 
    
    {% if crashes > CRASH_LIMIT %}
        RESPOND TYPE=error MSG="Adaptive Flow: Issues Detected ({crashes}). Slowing down."
        SET_GCODE_VARIABLE MACRO=_AUTO_TEMP_CORE VARIABLE=slowdown_layers_left VALUE=3
        {% if not macro_vars.slowdown_active %}
            M220 S50 
            SET_GCODE_VARIABLE MACRO=_AUTO_TEMP_CORE VARIABLE=slowdown_active VALUE=True
        {% endif %}
    {% else %}
        {% if macro_vars.slowdown_layers_left > 0 %}
            {% set remaining = macro_vars.slowdown_layers_left - 1 %}
            SET_GCODE_VARIABLE MACRO=_AUTO_TEMP_CORE VARIABLE=slowdown_layers_left VALUE={remaining}
            {% if remaining == 0 %}
                RESPOND MSG="Adaptive Flow: Recovered. Speed 100%."
                M220 S100
                SET_GCODE_VARIABLE MACRO=_AUTO_TEMP_CORE VARIABLE=slowdown_active VALUE=False
            {% endif %}
        {% endif %}
    {% endif %}
    SET_GCODE_VARIABLE MACRO=_AUTO_TEMP_CORE VARIABLE=layer_crash_count VALUE=0

[delayed_gcode AUTO_TEMP_LOOP]
initial_duration: 0
gcode:
    {% if printer.save_variables.variables.at_enabled|default(False) %}
        _AUTO_TEMP_CORE
        UPDATE_DELAYED_GCODE ID=AUTO_TEMP_LOOP DURATION=1.0
    {% endif %}

[gcode_macro AT_RESET_STATE]
gcode:
    SET_GCODE_VARIABLE MACRO=_AUTO_TEMP_CORE VARIABLE=last_stable_z VALUE=0.0
    SET_GCODE_VARIABLE MACRO=_AUTO_TEMP_CORE VARIABLE=layer_crash_count VALUE=0
    SET_GCODE_VARIABLE MACRO=_AUTO_TEMP_CORE VARIABLE=slowdown_active VALUE=False
    SET_GCODE_VARIABLE MACRO=_AUTO_TEMP_CORE VARIABLE=slowdown_layers_left VALUE=0
    M220 S100

[gcode_macro AT_ENABLE]
gcode:
    {% set current_target = printer.extruder.target|float %}
    {% if current_target < 170 %}
        RESPOND TYPE=error MSG="Heat nozzle first."
        SAVE_VARIABLE VARIABLE=at_enabled VALUE=False
    {% else %}
        SAVE_VARIABLE VARIABLE=base_print_temp VALUE={current_target}
        SAVE_VARIABLE VARIABLE=at_enabled VALUE=True
        {% set current_pa = printer.extruder.pressure_advance|default(0.0)|float %}
        SET_GCODE_VARIABLE MACRO=_AUTO_TEMP_CORE VARIABLE=base_pa VALUE={current_pa}
        RESPOND MSG="Adaptive Flow: ON. Base {current_target}C / PA {current_pa}"
        UPDATE_DELAYED_GCODE ID=AUTO_TEMP_LOOP DURATION=1.0
    {% endif %}

[gcode_macro AT_DISABLE]
gcode:
    SAVE_VARIABLE VARIABLE=at_enabled VALUE=False
    {% set base = printer.save_variables.variables.base_print_temp|default(0)|float %}
    {% if base > 100 %} M104 S{base} {% endif %}
    {% set base_pa = printer["gcode_macro _AUTO_TEMP_CORE"].base_pa|default(0.0)|float %}
    {% if base_pa > 0 %} SET_PRESSURE_ADVANCE ADVANCE={base_pa} {% endif %}
    UPDATE_DELAYED_GCODE ID=AUTO_TEMP_LOOP DURATION=0
    SET_GCODE_VARIABLE MACRO=_AUTO_TEMP_CORE VARIABLE=current_boost VALUE=0.0
    RESPOND MSG="Adaptive Flow: Disabled."

[gcode_macro AT_SET_FLOW_K]
gcode:
    SAVE_VARIABLE VARIABLE=material_flow_k VALUE={params.K|default(0)|float}

[gcode_macro AT_SET_VISC_K]
gcode:
    SAVE_VARIABLE VARIABLE=material_viscosity_k VALUE={params.K|default(0)|float}

[gcode_macro AT_SET_MAX]
gcode:
    SAVE_VARIABLE VARIABLE=at_max_temp VALUE={params.MAX|default(300)|int}
</details>
Step 3: Edit printer.cfg
Open your printer.cfg and add the following lines.
code
Ini
[include auto_flow.cfg]

[extruder_monitor]
# IMPORTANT: Change this to match your actual driver section!
# Examples: "tmc2209 extruder" or "tmc2209 stepper_e" or "tmc5160 extruder"
driver_name: tmc2209 extruder

[save_variables]
filename: ~/printer_data/config/sfs_auto_flow_vars.cfg
🚀 Usage
Start Macro
Replace your existing PRINT_START with the following. This handles the sensitivity tuning for different materials automatically.
<details>
<summary>Click to view <b>Universal PRINT_START</b></summary>
code
Ini
[gcode_macro PRINT_START]
gcode:
  # =================================================================
  # 1. PARAMETER SETUP
  # =================================================================
  {% set target_bed      = params.BED|default(60)|int %}
  {% set target_extruder = params.EXTRUDER|default(200)|int %}
  {% set material        = params.MATERIAL|default("PLA")|string %}
  
  # Default Config
  {% set tol = 2 %}            # Bed tolerance (+/- deg)
  {% set probe_temp = 150 %}   # Safe temp for probing (no ooze)
  {% set soak_min = 0 %}       # Optional heat soak time

  # Geometry helpers
  {% set x_wait = printer.toolhead.axis_maximum.x|float / 2 %}
  {% set y_wait = printer.toolhead.axis_maximum.y|float / 2 %}

  # =================================================================
  # 2. MATERIAL CONFIGURATION
  # =================================================================
  {% set load_k = 0.0 %}   
  {% set speed_k = 0.0 %}  
  {% set pa_val = 0.0 %}
  {% set max_temp_safety = 300 %}

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
      {% set load_k = 0.0 %}
      {% set speed_k = 0.0 %}
      {% set pa_val = 0.20 %} 
      {% set max_temp_safety = 240 %}
  {% endif %}

  # =================================================================
  # 3. PREPARATION
  # =================================================================
  AT_RESET_STATE
  AT_DISABLE
  
  M117 Homing...
  G28
  G90

  M117 Heating bed to {target_bed}C
  M140 S{target_bed}
  TEMPERATURE_WAIT SENSOR=heater_bed MINIMUM={target_bed - tol} MAXIMUM={target_bed + tol}

  {% if soak_min > 0 %}
    M117 Soaking bed...
    G4 P{soak_min * 60000}
  {% endif %}

  M117 Nozzle to {probe_temp}C
  M104 S{probe_temp}
  M109 S{probe_temp}

  # =================================================================
  # 4. MESH
  # =================================================================
  M117 Meshing...
  BED_MESH_CLEAR
  BED_MESH_CALIBRATE ADAPTIVE=1

  # =================================================================
  # 5. PRINT HEAT & ACTIVATE
  # =================================================================
  M117 Heating nozzle to {target_extruder}C
  M104 S{target_extruder}
  M109 S{target_extruder}

  {% if pa_val > 0 %}
      SET_PRESSURE_ADVANCE ADVANCE={pa_val}
  {% endif %}

  {% if load_k > 0 or speed_k > 0 %}
      AT_SET_VISC_K K={load_k}
      AT_SET_FLOW_K K={speed_k}
      AT_SET_MAX MAX={max_temp_safety}
      AT_ENABLE
      RESPOND MSG="Adaptive Flow: ON ({material})"
  {% else %}
      AT_DISABLE
      RESPOND MSG="Adaptive Flow: OFF ({material})"
  {% endif %}

  # =================================================================
  # 6. PRIME LINE
  # =================================================================
  M117 Priming...
  G0 X{x_wait - 50} Y4 F10000
  G0 Z0.4
  G91
  G1 X100 E20 F1000
  G90
  M117 Printing...
</details>
🔧 Tuning Guide
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
20: Lower this number to make it more sensitive (detect smaller blobs). Raise it if you get false positives.
