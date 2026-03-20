# Adaptive Flow Dashboard

A browser-based dashboard for analyzing your Klipper Adaptive Flow prints. View print health, detect banding culprits, and diagnose slicer settings — all from any device on your network. **No SSH required.**

```
http://<printer-ip>:7127
```

The dashboard service starts on boot and restarts automatically if it crashes.

---

## Getting Started

The dashboard is installed automatically by `update.sh`. After updating, open your browser and navigate to `http://<printer-ip>:7127`.

The page loads with your most recent print selected. If a print is currently in progress, it is detected automatically and displayed with a **LIVE** indicator.

---

## Dashboard Layout

### Summary Cards

Five cards along the top give an at-a-glance health overview. Each card has a **?** tooltip explaining the metric, what "good" looks like, and when to worry:

| Card | What It Shows |
|------|---------------|
| **Material** | Filament type and print duration (or "PRINTING" + elapsed time during a live print) |
| **Extrusion Quality** | Weighted 0–100 composite score covering thermal stability, flow steadiness, heater reserve, and pressure consistency |
| **Temp Boost** | Average and max temperature boost applied by Adaptive Flow |
| **Heater Duty** | Average and max PWM duty cycle — flags saturation risk |
| **Speed Guard** | Percentage of layers where Speed Guard slowed acceleration to protect quality |

### Tab Navigation

Below the cards, tabs switch between analysis views. Each tab has a **?** tooltip describing what the chart shows and how to interpret it:

| Tab | Contents |
|-----|----------|
| **✂ Slicer** | The most useful tab. Extracts slicer settings from G-code, shows acceleration fingerprint chart, and tells you exactly which settings to change |
| **Timeline** | Temperature and flow/speed/PWM charts over time |
| **Z-Height** | Banding risk bar chart by Z-layer + problem zone breakdown |
| **Heater** | PWM vs flow-rate brackets + thermal lag episodes |
| **PA** | Pressure Advance value over time + oscillation zone table |
| **Speed Guard** | Speed Guard activation by Z-height — which layers were slowed and how often |
| **Distribution** | Speed and flow rate histograms — where your printer spends its time |

Every chart includes a description paragraph explaining what you're looking at — colour coding, what "good" looks like, and what to watch for. No prior knowledge required.

### Session Selector

A dropdown at the top lets you browse past prints. When a live print is detected (CSV log with no summary, modified within 2 minutes), it appears as **"LIVE PRINT"** at the top of the list.

---

## Key Features

### Real-Time Monitoring

The dashboard detects active prints automatically:

- A pulsing **LIVE** indicator appears in the header
- Charts auto-refresh every **5 seconds** during printing
- Summary cards show a "PRINTING" badge with elapsed time
- Charts update in-place without full page reloads
- Once the print finishes, the view seamlessly switches to the completed summary

For completed prints, optional auto-refresh runs at 30-second intervals.

## Interactive Charts

All charts are built with Chart.js v4 and support:

- **Hover tooltips** — hover over any data point for exact values
- **Zoom & pan** — scroll to zoom, drag to pan (on supported views)
- **Legend toggling** — click legend items to show/hide individual data series
- **Responsive layout** — charts resize to fit any screen (desktop, tablet, mobile)

### Slicer Tab

The Slicer tab extracts settings from your G-code file's footer and cross-references them with observed print data. It works with OrcaSlicer, BambuStudio, PrusaSlicer, and SuperSlicer — any slicer that writes `; key = value` comments.

**Acceleration Fingerprint** — a horizontal bar chart showing each distinct acceleration value the slicer used during the print, what percentage of print time was spent at that value, and which slicer feature maps to it. For example:

```
8000 (Outer Wall, Inner Wall, Bridge)  ████████████████████████ 72.7%
10000 (Default, Sparse Infill)         ██████ 17.9%
12000 (Travel)                         ██ 4.8%
6000 (Top Surface)                     █ 2.4%
2000 (Initial Layer)                   █ 2.3%
```

Fewer distinct values = fewer banding-causing transitions.

**Accel Breakdown** — table with exact sample counts and percentages per acceleration value.

**Issues & Suggestions** — when the diagnosis finds problematic settings, it shows specific before → after recommendations:

| Issue | What It Detects | Example Suggestion |
|-------|-----------------|-------------------|
| Bridge accel mismatch | Bridge acceleration far below outer wall | Bridge acceleration 1600 → 8000 |
| Bridge flow too low | Bridge flow ratio causing under-extrusion | Bridge flow 0.9 → 1.0 |
| Inner/outer wall mismatch | Different accel for inner vs outer walls | Inner wall acceleration 5000 → 8000 |
| Too many distinct accels | 5+ different values causing constant transitions | Informational — review settings |

**Settings Tables** — all acceleration, speed, and other quality-related settings extracted from the G-code, organised into Acceleration, Speed, and Other categories.

### Timeline Tab

Two stacked charts:

1. **Temperature chart** — target temp (dashed), actual temp (solid), and boost amount (filled area)
2. **Flow & speed chart** — flow rate (mm³/s), speed (mm/s), and heater PWM (%) over time

### Z-Height Tab

Bar chart showing banding risk score by Z-layer bin (default 0.5mm per bin). Problem zones (score ≥5) are highlighted. Below the chart, a breakdown lists each problem zone with its cause (accel changes, PA changes, Speed Guard transitions).

### Heater Tab

Two sections:

1. **Headroom bar chart** — average, P95, and max PWM at each flow-rate bracket. Shows remaining heater capacity at each flow level.
2. **Thermal lag table** — individual lag episodes listing duration, max lag, flow rate, PWM duty, and Z range.

### PA Tab

1. **PA timeline chart** — PA value plotted over time, with oscillation zones highlighted
2. **Oscillation zone table** — start time, duration, number of changes, and Z range for each zone

### Speed Guard Tab

Bar chart showing which layers were slowed down and how often. Yellow bars show the percentage of time each layer ran at reduced acceleration. Red bars show how often speed switched between fast and slow at each height — frequent switching can itself cause visible lines.

### Distribution Tab

Side-by-side histograms:

1. **Speed distribution** — percentage of print time in each speed bracket, with average boost and PWM
2. **Flow distribution** — percentage of print time in each flow bracket, with average boost and PWM

---

## API Endpoints

The dashboard exposes a JSON API:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/data` | GET | Latest print data (or live print if active) |
| `/api/data?session=<file>` | GET | Data for a specific completed print |
| `/api/apply-config` | POST | Apply a config change (JSON body: `{variable, value}`) |

The `/api/data` response includes `slicer_settings` (dict of all extracted G-code settings) and `slicer_diagnosis` (accel fingerprint, issues, suggestions).

Responses are cached with a 15-second TTL. Applying a config change via `/api/apply-config` invalidates the cache so subsequent reads reflect the updated state.

All endpoints return JSON — useful for custom integrations, Grafana panels, or external dashboards.

---

## Service Management

```bash
# Check status
sudo systemctl status adaptive-flow-dashboard

# Restart after config changes
sudo systemctl restart adaptive-flow-dashboard

# View logs
journalctl -u adaptive-flow-dashboard -f

# Stop the service
sudo systemctl stop adaptive-flow-dashboard
```

### Manual Start (without systemd)

```bash
python3 ~/Klipper-Adaptive-Flow/analyze_print.py --serve
python3 ~/Klipper-Adaptive-Flow/analyze_print.py --serve --port 8080
```

---

## Analysis Details

The dashboard runs the same analysis engine available via CLI. Below is a reference for what each analysis detects and how scores are calculated.

### Banding Risk Score (0–10)

Each CSV sample is scored for banding risk:

| Trigger | Points |
|---------|--------|
| Accel change >500 mm/s² | +3 |
| PA change >0.005 | +2 |
| Temp change >3°C | +2 |
| Speed Guard transition | +2 |
| Temp overshoot >5°C | +1 |

Score ≥5 = high-risk event (likely visible artifact on the part).

### Banding Culprits

When banding events are detected, the dashboard diagnoses the most likely cause:

| Culprit | Cause | Suggested Fix |
|---------|-------|---------------|
| `dynz_accel_switching` | Speed Guard changing acceleration mid-layer | `dynz_relief_method: 'temp_reduction'` |
| `pa_oscillation` | PA bouncing rapidly | Lower `pa_boost_k` or increase `pa_deadband` |
| `temp_instability` | Temperature oscillating | Lower ramp rates, check PID tuning |
| `slicer_accel_control` | Slicer inserting accel G-code | Reduce distinct accel values in slicer, or match inner/outer wall accels. See Slicer tab for specific settings |
| `no_obvious_culprit` | Low event counts | Check mechanical causes (Z-wobble, filament) |

### Thermal Lag Detection

A lag episode is recorded whenever actual nozzle temperature falls more than 3°C (default) behind target. Each episode tracks duration, max lag, flow rate, PWM duty, and Z range. If PWM is near 100% during lag, the heater is at its physical limit; if PWM is below 90%, increasing `ramp_rate_rise` can help the heater respond faster.

### Heater Headroom Brackets

Samples are grouped by flow rate (mm³/s) and the average, P95, and max PWM are computed for each bracket. The bracket where P95 PWM crosses 95% marks your heater's effective flow limit. Below that threshold you have headroom to increase `flow_k`; above it, reduce `flow_k` or `max_boost_limit`.

> **Important:** Max PWM hitting 100% in the graphs is **normal PID behavior**, not heater saturation. Klipper's PID briefly goes full power on every temperature transition. The dashboard distinguishes between transient PID ramp-up (harmless) and sustained saturation (problematic) by checking average PWM and thermal lag — not just peak PWM. A 40W heater showing 100% max in the charts but 70% average with low thermal lag is working perfectly.

### Heater Analysis Intelligence

The dashboard uses a multi-signal approach to avoid false alarms about heater performance:

| Condition | Severity | Meaning |
|-----------|----------|---------|
| Avg PWM ≥85%, lag >10% | **bad** | Genuine saturation — heater can't keep up |
| Avg PWM ≥80%, lag >5% | **warn** | Working hard, limited margin |
| Max PWM ≥98%, lag ≤5% | **good** | Normal PID ramp-up, heater is fine |
| Avg PWM <60% | **good** | Plenty of headroom |
| Avg PWM <85%, lag ≤10% | **good** | Heater is healthy |

Per-bracket saturation (P95 >95% in high-flow brackets) is only flagged as a warning if the heater is also struggling globally (high avg PWM + high lag). Otherwise it's informational — brief 100% spikes at high flow are normal.

Thermal lag thresholds:

| Lag % | Severity | Action |
|-------|----------|--------|
| >15% | **bad** | Increase ramp rate or reduce flow_k |
| >8% | **warn** | Consider increasing ramp rate |
| 3–8% | **info** | Normal for smaller heaters, not visible in print quality |
| <1% | **good** | Excellent thermal tracking |

### PA Oscillation Zones

An oscillation zone is a period where PA changed ≥4 times within 10 seconds. These zones often correlate with visible ribbing. If many zones are detected, increase `pa_deadband` (try 0.005+). If PA range is very wide (>0.02), lower `pa_boost_k`.

### Extrusion Quality Score (0–100)

A weighted composite score evaluating four aspects of print health:

| Component | Weight | What It Measures |
|-----------|--------|------------------|
| **Thermal** | 35% | How well actual temperature tracked target (deviation %, in-band %) |
| **Flow** | 30% | Flow rate steadiness (jitter, big jumps as % of samples) |
| **Heater** | 20% | Heater reserve capacity (PWM saturation %, avg PWM) |
| **Pressure** | 15% | PA transient impact (frequency and severity of PA-related artifacts) |

Each component scores 0–100 independently, then they're combined into the overall score. The weakest component is highlighted in the dashboard summary card. Scores above 80 indicate good print quality; below 60 suggests actionable problems.

### Boost Optimization

The dashboard analyses your actual print data to determine whether you can print faster. It checks five systems:

| System | What It Checks |
|--------|---------------|
| **Heater capacity** | Average and peak PWM vs saturation threshold |
| **Flow capacity** | Peak flow vs safe flow limit for your nozzle |
| **Temperature boost** | Boost used vs available boost range |
| **Acceleration** | Actual accel vs input shaper recommended max |
| **Fan** | Fan utilisation (informational) |

The verdict is one of:
- **significant_headroom** — all systems have margin, with a suggested speed increase percentage
- **moderate_headroom** — some margin exists, with specific limiting factors identified
- **at_limit** — one or more systems are saturated, with the bottleneck identified
- **over_limit** — actively exceeding safe limits

When headroom exists, the dashboard shows specific suggestions (e.g. "Increase speeds by ~40%") and offers config changes like adjusting `flow_k` to better utilise available headroom.

### CSV Column Reference

The extruder monitor logs these columns for each sample (one row per ~0.5 seconds):

| Column | Description |
|--------|-------------|
| `elapsed_s` | Seconds since print start |
| `temp_actual` | Measured nozzle temperature (°C) |
| `temp_target` | Current target temperature (°C) |
| `boost` | Temperature boost applied (°C above base) |
| `flow` | Measured volumetric flow rate (mm³/s) |
| `speed` | Toolhead speed (mm/s) |
| `pwm` | Heater PWM duty cycle (0.0–1.0) |
| `pa` | Current Pressure Advance value |
| `z_height` | Current Z position (mm) |
| `predicted_flow` | Lookahead predicted flow rate (mm³/s) |
| `dynz_active` | Speed Guard active — 1 if layer is being slowed, 0 if at full speed |
| `accel` | Current acceleration (mm/s²) |
| `fan_pct` | Part cooling fan percentage (0–100) |
| `pa_delta` | PA change from previous sample |
| `accel_delta` | Acceleration change from previous sample |
| `temp_target_delta` | Target temp change from previous sample |
| `temp_overshoot` | Actual − Target temperature |
| `dynz_transition` | Speed Guard state change (1=slowed, −1=restored, 0=no change) |
| `layer_transition` | Layer change detected (1 or 0) |
| `banding_risk` | Composite risk score 0–10 |
| `event_flags` | Human-readable events (e.g., `ACCEL_CHG:+1200`) |

---

## CLI Reference

The analysis engine is also accessible via command line for scripting and quick checks:

```bash
cd ~/Klipper-Adaptive-Flow

# Single-print health summary
python3 analyze_print.py

# Analyze a specific print
python3 analyze_print.py /path/to/print_summary.json

# Banding analysis (last N prints)
python3 analyze_print.py --count 10
python3 analyze_print.py --count 10 --material PLA

# Z-height banding heatmap
python3 analyze_print.py --z-map
python3 analyze_print.py --z-map --z-bin 1.0

# Thermal lag report
python3 analyze_print.py --lag
python3 analyze_print.py --lag --lag-threshold 5.0

# Heater headroom
python3 analyze_print.py --headroom

# PA stability
python3 analyze_print.py --pa-stability

# Speed Guard zone map
python3 analyze_print.py --dynz-map

# Speed/flow distribution
python3 analyze_print.py --distribution

# Custom log directory
python3 analyze_print.py --log-dir /path/to/logs
```

---

## Requirements

- Python 3 (uses built-in `http.server` — no extra dependencies)
- Chart.js v4 loaded from CDN (requires internet on first page load; cached afterward)
- Port 7127 accessible from your browser's network
- At least one completed print with Adaptive Flow enabled

### Slicer Filename Format

For **completed prints**, the material is read from the summary JSON (populated by the `MATERIAL=` parameter passed to `AT_START`). This always works regardless of filename.

For **live prints** (no summary exists yet), the dashboard extracts the material from the gcode filename. Your slicer's output filename must include the filament type as a separate token. The dashboard recognises: `PLA`, `PETG`, `ABS`, `ASA`, `TPU`, `PA`, `PC`, `NYLON`, `HIPS`, `PVA`, `PP`, `PEI`, `PCTG`, `CPE`.

**OrcaSlicer / PrusaSlicer** — set the filename format in **Print Settings → Output → Output filename format**:

```
{input_filename_base}_{filament_type[0]}_{print_time}.gcode
```

This produces filenames like `Voron Design Cube v7_PETG_19m57s.gcode`, from which the dashboard can identify `PETG`.

**Cura** — set **Preferences → Project → Default output filename**:

```
{file_name}_{material_type}_{print_time}
```

If your filenames don't include a material token, live prints will show "Unknown" for the material. Completed prints are unaffected since they use the `AT_START MATERIAL=` parameter.

---

## Troubleshooting

### "No logs found"

Ensure at least one print has completed with Adaptive Flow enabled:

```bash
ls ~/printer_data/logs/adaptive_flow/
```

### "Summary contains 0 samples"

The print was too short, or `AT_START`/`AT_END` weren't called in your start/end G-code.

### Dashboard not loading

```bash
# Check if the service is running
sudo systemctl status adaptive-flow-dashboard

# Check for port conflicts
ss -tlnp | grep 7127

# Restart the service
sudo systemctl restart adaptive-flow-dashboard
```
