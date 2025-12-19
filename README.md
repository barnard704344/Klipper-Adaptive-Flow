# Klipper Adaptive Flow — Lookahead Branch

> **Note:** This system is designed for **E3D Revo hotends only** (Revo HF and Revo Standard).

This branch adds **live G-code lookahead** to the Adaptive Flow system, enabling predictive temperature and pressure advance adjustments based on upcoming extrusion moves.

## What's New in This Branch

The `lookahead-feature` branch extends the original Adaptive Flow with:

| Feature | Description |
|---------|-------------|
| **Live G-code parsing** | `extruder_monitor.py` intercepts incoming `G0/G1` commands in real-time |
| **G-code interceptor** | New `gcode_interceptor.py` module provides reliable G-code event hooks |
| **Lookahead buffer** | Stores upcoming extrusion segments (E delta + duration) with auto-expiry |
| **Predicted extrusion rate** | Calculates expected mm/s based on buffered moves |
| **Proactive temp boost** | Raises temperature *before* high-flow sections arrive |
| **Smoother transitions** | Reduces under-extrusion at flow ramp-ups |
| **Relative extrusion support** | Handles both M82 (absolute) and M83 (relative) extrusion modes |
| **Single-point configuration** | All user settings are macro variables at the top of `auto_flow.cfg` |

## How It Works

```
G-code Stream → gcode_interceptor.py → extruder_monitor.py → Lookahead Buffer
                                                                    ↓
                                                predicted_extrusion_rate (mm/s)
                                                                    ↓
                                     auto_flow.cfg → lookahead_boost → M104 / PA adjust
```

1. `gcode_interceptor.py` wraps Klipper's G-code dispatch and broadcasts lines to subscribers
2. `extruder_monitor.py` receives G-code lines, parses `G0/G1` moves, and calculates upcoming extrusion demand
3. Extrusion segments are stored in a buffer (auto-expires after 2 seconds)
4. Every 1 second, `auto_flow.cfg` reads `predicted_extrusion_rate`
5. If upcoming flow > current flow, it applies a **lookahead boost** to temperature
6. Temperature ramps *before* the high-flow section, not after

## Files

| File | Purpose |
|------|---------|
| `gcode_interceptor.py` | Klipper module — intercepts G-code and broadcasts to subscribers |
| `extruder_monitor.py` | Klipper module — TMC load reading + live lookahead parsing |
| `auto_flow.cfg` | Macros for adaptive temp, PA, blob detection, and lookahead boost |

## Installation

1. Copy the Python modules to Klipper extras:
   ```bash
   cp gcode_interceptor.py ~/klipper/klippy/extras/
   cp extruder_monitor.py ~/klipper/klippy/extras/
   ```

2. Add both modules to your `printer.cfg`:
   ```ini
   [gcode_interceptor]
   
   [extruder_monitor]
   driver_name: tmc2209 extruder   ; adjust to match your TMC driver config section
   ```

3. Include the macros:
   ```ini
   [include auto_flow.cfg]
   ```

4. Restart Klipper:
   ```bash
   sudo systemctl restart klipper
   ```

5. Check logs for:
   ```
   GCodeInterceptor: Ready and intercepting G-code
   Live G-code lookahead hook installed via gcode_interceptor.
   ```

## Configuration Options

### extruder_monitor

| Option | Default | Description |
|--------|---------|-------------|
| `stepper` | `extruder` | Extruder stepper name |
| `driver_name` | `tmc2209 extruder` | TMC driver config section name |

### gcode_interceptor

No configuration options required — just add `[gcode_interceptor]` to enable.

## Usage

Enable adaptive flow before printing:
```gcode
AT_INIT_MATERIAL MATERIAL=PLA
```

The system will automatically:
- Monitor extruder load (SG_RESULT from TMC)
- Track live extrusion velocity
- Parse upcoming G-code for lookahead
- Adjust temperature and pressure advance in real-time

### Manual Lookahead Commands

You can also manually add lookahead segments (useful for testing or custom macros):

```gcode
SET_LOOKAHEAD E=2.5 D=0.5    ; Add segment: 2.5mm extrusion over 0.5 seconds
SET_LOOKAHEAD CLEAR          ; Clear the lookahead buffer
GET_PREDICTED_LOAD           ; Query predicted extrusion rate and load
GET_EXTRUDER_LOAD            ; Query current TMC StallGuard value
```

## Baseline Calibration

The sensor baseline is the StallGuard reading when extruding freely with no resistance. Accurate calibration is essential for load detection.

### Automatic Calibration (Recommended)

Run the automatic calibration macro:

```gcode
AT_AUTO_CALIBRATE TEMP=220 LENGTH=50 SAMPLES=10
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `TEMP` | 220 | Temperature for calibration |
| `LENGTH` | 50 | Total mm of filament to extrude |
| `SAMPLES` | 10 | Number of SG readings to average |

The macro will:
1. Heat to the specified temperature
2. Extrude filament while sampling SG_RESULT
3. Filter outliers and calculate the average
4. Display the recommended baseline value

After calibration, save the value:
```gcode
SAVE_VARIABLE VARIABLE=sensor_baseline VALUE=16
```

### Manual Calibration (Legacy)

For manual calibration, use:
```gcode
AT_CHECK_BASELINE TEMP=220
```
This extrudes 100mm at 50mm/s and displays raw SG values. Note the average and update `variable_sensor_baseline` in `auto_flow.cfg`.

## Tuning

All user configuration is done via macro variables at the top of `auto_flow.cfg`:

### Hotend & Sensor Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `variable_use_high_flow_nozzle` | `True` | `True` for Revo HF, `False` for Revo Standard |
| `variable_sensor_baseline` | `16` | StallGuard baseline (run `AT_AUTO_CALIBRATE` to find yours) |
| `variable_noise_filter` | `2` | Min strain delta before applying load boost (2 for Pancake, 10 for NEMA17) |
| `variable_crash_threshold` | `10` | Load delta that triggers blob detection (10 for Pancake, 20 for NEMA17) |

### Advanced Tuning

| Variable | Default | Description |
|----------|---------|-------------|
| `variable_flow_smoothing` | `0.5` | Exponential smoothing factor (0.0-1.0, higher = smoother) |
| `variable_max_boost_limit` | `50.0` | Maximum temp boost above base (°C) |
| `variable_ramp_rate_rise` | `2.0` | Max temp increase per second (°C/s) |
| `variable_ramp_rate_fall` | `0.2` | Max temp decrease per second (°C/s) |

### Lookahead Boost

In `auto_flow.cfg`, the lookahead boost multiplier can be adjusted:
```jinja
{% set lookahead_boost = lookahead_delta * 0.5 %}  ; 0.5°C per mm³/s predicted increase
```

Increase for more aggressive pre-heating, decrease if you see overheating on small prints.

### Lookahead Expiry

Buffered segments auto-expire after 2 seconds to prevent stale data. This is set in `extruder_monitor.py`:
```python
max_age = 2.0  # seconds
```

## Compatibility

- **Hotend:** E3D Revo only (Revo HF or Revo Standard)
- **TMC Driver:** Requires StallGuard support (TMC2209, TMC2130, TMC5160)
- **Extrusion Mode:** Supports both absolute (M82) and relative (M83) extrusion
- **Platform:** Tested on Klipper with Raspberry Pi
- **Overhead:** Minimal CPU usage (host-side parsing only)
- **Interfaces:** Works with Mainsail, Fluidd, and OctoPrint

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Driver not found" error | Verify `driver_name` matches your TMC config section exactly |
| Lookahead not working | Check logs for "intercepting G-code" message |
| High CPU usage | Reduce lookahead buffer size or increase expiry time |
| Erratic temperature | Lower `lookahead_boost` multiplier or increase smoothing |

## Branch Info

| Branch | Description |
|--------|-------------|
| `main` | Original Adaptive Flow (reactive only) |
| `lookahead-feature` | **This branch** — adds predictive lookahead |



