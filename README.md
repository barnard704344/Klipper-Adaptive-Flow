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
