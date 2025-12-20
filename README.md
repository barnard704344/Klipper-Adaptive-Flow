# Klipper Adaptive Flow

**Dynamic temperature control for Klipper using G-code lookahead and live velocity data.**

Automatically adjusts nozzle temperature based on real-time extrusion flow — hotter for high-flow sections, cooler for fine details. Tuned for **E3D Revo** hotends.

---

## Quick Start

1. **Install files:**
   ```bash
   # Clone repository
   git clone https://github.com/barnard704344/Klipper-Adaptive-Flow.git
   cd Klipper-Adaptive-Flow
   
   # Copy to Klipper
   cp gcode_interceptor.py extruder_monitor.py ~/klipper/klippy/extras/
   cp auto_flow.cfg ~/printer_data/config/
   ```

2. **Add to `printer.cfg`:**
   ```ini
   [include auto_flow.cfg]
   [gcode_interceptor]
   [extruder_monitor]
   ```

3. **Add to slicer start G-code** (after heating):
   ```gcode
   AT_START
   ```

4. **Add to slicer end G-code:**
   ```gcode
   AT_END
   ```

5. **Restart Klipper and print.** That's it.

---

## How It Works

The system monitors extrusion velocity and upcoming G-code to adjust temperature dynamically:

| Flow Condition | Action |
|----------------|--------|
| High flow (infill, fast walls) | Boost temperature to prevent under-extrusion |
| Low flow (details, perimeters) | Return to base temp to reduce stringing |
| Acceleration ramps | Predictive heating before flow increase |
| Sharp corners | Maintain heat for proper PA behavior |

### Flow Gates (When Boosting Activates)

Boost only triggers when flow exceeds material-specific thresholds based on E3D Revo datasheet:

| Material | High Flow Nozzle | Standard Nozzle |
|----------|------------------|-----------------|
| PLA | 10 mm³/s | 8 mm³/s |
| PETG | 14 mm³/s | 10 mm³/s |
| ABS/ASA | 12 mm³/s | 9 mm³/s |
| PC | 11 mm³/s | 8 mm³/s |
| NYLON | 12 mm³/s | 9 mm³/s |
| TPU | Disabled | Disabled |

---

## Configuration

Edit `auto_flow.cfg` to set your nozzle type:

```ini
variable_use_high_flow_nozzle: True   # Set False for standard Revo nozzles
```

All other settings auto-configure based on material detection.

### Key Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `flow_smoothing` | 0.3 | Filter for flow spikes (0.0-1.0, higher = more filtering) |
| `max_boost_limit` | 50 | Maximum temp boost above base (°C) |
| `ramp_rate_rise` | 4.0 | Max temp increase per second |
| `ramp_rate_fall` | 0.2 | Max temp decrease per second (slow cooldown) |
| `self_learning_enabled` | True | Auto-tune K-values over time |
| `pa_auto_learning` | True | Experimental PA tuning from corners |

---

## Auto-Detection

`AT_START` detects material from your slicer's temperature setting:

| Temperature | Material |
|-------------|----------|
| 280°C+ | PC |
| 260-280°C | NYLON |
| 240-260°C | ABS/ASA |
| 220-240°C | PETG |
| 180-220°C | PLA |
| <180°C | TPU |

---

## Commands

| Command | Description |
|---------|-------------|
| `AT_START` | Enable adaptive flow (call after heating) |
| `AT_END` | Disable and report stats |
| `AT_STATUS` | Show current state, flow, boost level |
| `AT_SET_PA MATERIAL=PLA PA=0.045` | Save calibrated PA value |
| `AT_LIST_PA` | List saved PA values |

---

## Self-Learning

The system improves over time:

**K-Value Learning:** Monitors if temperature follows demand. Adjusts boost aggressiveness every 50 samples.

**PA Learning (Experimental):** Detects sharp corners, measures thermal response. Too hot after corners = increase PA. Too cold = decrease PA. Saves learned values per material.

---

## Slicer Settings

- **Pressure Advance:** Set to 0 in slicer. Use `AT_SET_PA` to store values in Klipper.
- **Temperature:** Use normal quality temps. Script boosts automatically when needed.
- **Max Volumetric Speed:** Stay within your setup's limits:
  | Setup | Limit |
  |-------|-------|
  | 40W + Standard | 17 mm³/s |
  | 40W + High Flow | 24 mm³/s |
  | 60W + High Flow | 32 mm³/s |

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| No temperature boost | Flow below gate threshold — check `AT_STATUS` for current flow |
| Erratic temperatures | Increase `flow_smoothing` to 0.5 |
| "Heater struggling" warnings | Reduce max volumetric speed in slicer |
| Thermal runaway errors | Lower `max_boost_limit` or check heater wiring |

---

## Files

| File | Purpose |
|------|---------|
| `auto_flow.cfg` | Klipper macros — all adaptive logic |
| `extruder_monitor.py` | G-code parsing, lookahead, corner detection |
| `gcode_interceptor.py` | Hooks into Klipper G-code stream |
| `community_defaults.json` | Shared material settings (auto-fetched) |

---

## Requirements

- **Hotend:** E3D Revo (HF or Standard) with 40W or 60W heater
- **Firmware:** Klipper
- **No TMC/StallGuard required** — uses velocity data only

---

## License

MIT License



