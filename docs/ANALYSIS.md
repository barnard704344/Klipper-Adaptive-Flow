# Adaptive Flow Dashboard

A browser-based dashboard for analyzing your Klipper Adaptive Flow prints. View print health, detect banding culprits, track recommendation lifecycle, and compare materials — all from any device on your network. **No SSH required.**

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

### Material Selector

At the top of the page, buttons let you switch views:

| Button | What It Shows |
|--------|---------------|
| **Per-Print** | Analysis of a single selected print session (default) |
| **PLA Aggregate** | Cross-print analysis aggregated across all PLA sessions |
| **PETG Aggregate** | Cross-print analysis aggregated across all PETG sessions |

Material buttons appear dynamically based on the materials found in your log directory. Aggregate views combine data from every session of that material to surface patterns that aren't visible in a single print.

### Summary Cards

Six cards along the top give an at-a-glance health overview:

| Card | What It Shows |
|------|---------------|
| **Material** | Filament type and print duration (or "PRINTING" + elapsed time during a live print) |
| **Temp Boost** | Average and max temperature boost applied by Adaptive Flow |
| **Heater Duty** | Average and max PWM duty cycle — flags saturation risk |
| **Banding Risk** | Number of high-risk events and the diagnosed culprit |
| **Thermal Lag** | Percentage of print time where actual temp fell behind target |
| **PA Stability** | PA range and number of oscillation zones |

In aggregate mode, cards display weighted averages across all prints of that material, with print count and total duration.

### Tab Navigation

Below the cards, tabs switch between analysis views:

| Tab | Contents |
|-----|----------|
| **Timeline** | Temperature and flow/speed/PWM charts over time |
| **Z-Height** | Banding risk bar chart by Z-layer + problem zone breakdown |
| **Heater** | PWM vs flow-rate brackets + thermal lag episodes |
| **PA** | Pressure Advance value over time + oscillation zone table |
| **DynZ** | DynZ activation percentage and stress by Z-height |
| **Distribution** | Speed and flow rate histograms — where your printer spends its time |
| **Trends** | Print-over-print line charts tracking metrics across sessions |

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

### Recommendations Panel

Each analysis view can generate recommendations displayed in a panel below the charts. Recommendations are color-coded by severity:

| Badge | Meaning |
|-------|---------|
| **bad** (red) | Critical issue — should be fixed before printing more |
| **warn** (amber) | Notable concern — consider adjusting |
| **info** (blue) | Informational — no action needed right now |
| **good** (green) | Healthy — no problems detected |

Each recommendation includes:
- A title describing the issue
- A detailed explanation of the cause
- An **action** — the specific config change to make
- An **Apply** button to apply the change directly from the dashboard

### One-Click Config Apply

Clicking **Apply** on a recommendation:

1. Writes the suggested value to your Klipper macro config via `SAVE_VARIABLE`
2. Logs the change (variable, old value, new value, timestamp) to `config_changes_log.json`
3. The button changes to **"Applied ✓"** with a confirmation message

This lets you tune your config iteratively without SSH — apply a recommendation, print again, and check whether the metrics improved.

### Applied Recommendation Tracking

The dashboard tracks which recommendations you've already applied and how many prints have completed since:

| Status | Badge | What It Means |
|--------|-------|---------------|
| **Not applied** | Normal severity badge | Recommendation is new, not yet acted on |
| **Applied, awaiting prints** | ✓ green | Config was changed but no prints have completed since |
| **Monitoring (N of ~5 prints)** | ⏳ info | N prints completed since the change — still collecting data |
| **Verified** | No longer shown | After ~5 prints, the recommendation disappears if metrics improved |

This prevents the dashboard from repeatedly suggesting a change you've already made. The lifecycle automatically advances as you complete prints.

### Material Aggregate Analysis

Aggregate views combine data from all sessions of a given material type. This surfaces patterns that may not be obvious in any single print:

- **Weighted averages** — boost, heater duty, and other metrics weighted by sample count per session
- **Combined banding analysis** — total high-risk events, accel changes, PA changes, and DynZ transitions across all prints
- **Merged Z-height heatmap** — Z-banding data pooled from every session
- **Heater headroom across prints** — flow-vs-PWM brackets for the full material history
- **PA stability overview** — aggregate PA range, oscillation zone count
- **DynZ combined map** — activation patterns merged from all sessions
- **Speed/flow distribution** — how you typically print with this material
- **Cross-print trend** — metrics plotted session-over-session

Aggregate recommendations reflect the material's overall behavior rather than a single print's anomalies.

---

## Interactive Charts

All charts are built with Chart.js v4 and support:

- **Hover tooltips** — hover over any data point for exact values
- **Zoom & pan** — scroll to zoom, drag to pan (on supported views)
- **Legend toggling** — click legend items to show/hide individual data series
- **Responsive layout** — charts resize to fit any screen (desktop, tablet, mobile)

### Timeline Tab

Two stacked charts:

1. **Temperature chart** — target temp (dashed), actual temp (solid), and boost amount (filled area)
2. **Flow & speed chart** — flow rate (mm³/s), speed (mm/s), and heater PWM (%) over time

### Z-Height Tab

Bar chart showing banding risk score by Z-layer bin (default 0.5mm per bin). Problem zones (score ≥5) are highlighted. Below the chart, a breakdown lists each problem zone with its cause (accel changes, PA changes, DynZ transitions).

### Heater Tab

Two sections:

1. **Headroom bar chart** — average, P95, and max PWM at each flow-rate bracket. Shows remaining heater capacity at each flow level.
2. **Thermal lag table** — individual lag episodes listing duration, max lag, flow rate, PWM duty, and Z range.

### PA Tab

1. **PA timeline chart** — PA value plotted over time, with oscillation zones highlighted
2. **Oscillation zone table** — start time, duration, number of changes, and Z range for each zone

### DynZ Tab

Bar chart showing DynZ activation percentage and stress score by Z-height bin. High-activity zones are flagged with transition counts and average acceleration.

### Distribution Tab

Side-by-side histograms:

1. **Speed distribution** — percentage of print time in each speed bracket, with average boost and PWM
2. **Flow distribution** — percentage of print time in each flow bracket, with average boost and PWM

### Trends Tab

Line charts tracking key metrics across your last N prints (oldest → newest):

- Average boost, heater duty, banding events, and culprit for each session
- Trend direction arrows (↑ worsening / ↓ improving) with percentage change

---

## API Endpoints

The dashboard exposes a JSON API:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/data` | GET | Latest print data (or live print if active) |
| `/api/data?session=<file>` | GET | Data for a specific completed print |
| `/api/material-data?material=PLA` | GET | Aggregate analysis for a given material |
| `/api/apply-config` | POST | Apply a config recommendation (JSON body: `{variable, value}`) |

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
| DynZ state transition | +2 |
| Temp overshoot >5°C | +1 |

Score ≥5 = high-risk event (likely visible artifact on the part).

### Banding Culprits

When enough prints are analyzed, the dashboard diagnoses the most common banding cause:

| Culprit | Cause | Suggested Fix |
|---------|-------|---------------|
| `dynz_accel_switching` | DynZ changing acceleration mid-layer | `dynz_relief_method: 'temp_reduction'` |
| `pa_oscillation` | PA bouncing rapidly | Lower `pa_boost_k` or increase `pa_deadband` |
| `temp_instability` | Temperature oscillating | Lower ramp rates, check PID tuning |
| `slicer_accel_control` | Slicer inserting accel G-code | Disable firmware accel control in slicer |
| `no_obvious_culprit` | Low event counts | Check mechanical causes (Z-wobble, filament) |

### Thermal Lag Detection

A lag episode is recorded whenever actual nozzle temperature falls more than 3°C (default) behind target. Each episode tracks duration, max lag, flow rate, PWM duty, and Z range. If PWM is near 100% during lag, the heater is at its physical limit; if PWM is below 90%, increasing `ramp_rate_rise` can help the heater respond faster.

### Heater Headroom Brackets

Samples are grouped by flow rate (mm³/s) and the average, P95, and max PWM are computed for each bracket. The bracket where P95 PWM crosses 95% marks your heater's effective flow limit. Below that threshold you have headroom to increase `flow_k`; above it, reduce `flow_k` or `max_boost_limit`.

### PA Oscillation Zones

An oscillation zone is a period where PA changed ≥4 times within 10 seconds. These zones often correlate with visible ribbing. If many zones are detected, increase `pa_deadband` (try 0.005+). If PA range is very wide (>0.02), lower `pa_boost_k`.

### CSV Column Reference

Enhanced logging records these columns for each sample:

| Column | Description |
|--------|-------------|
| `pa_delta` | PA change from last sample |
| `accel_delta` | Acceleration change from last sample |
| `temp_target_delta` | Target temp change |
| `temp_overshoot` | Actual − Target temp |
| `dynz_transition` | DynZ state change (1=ON, −1=OFF) |
| `layer_transition` | Layer change detected |
| `banding_risk` | Risk score 0–10 |
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

# Multi-print banding analysis (last N prints)
python3 analyze_print.py --count 10
python3 analyze_print.py --count 10 --material PLA

# Z-height banding heatmap
python3 analyze_print.py --z-map
python3 analyze_print.py --z-map --z-bin 1.0

# Print-over-print trends
python3 analyze_print.py --trend 10

# Thermal lag report
python3 analyze_print.py --lag
python3 analyze_print.py --lag --lag-threshold 5.0

# Heater headroom
python3 analyze_print.py --headroom

# PA stability
python3 analyze_print.py --pa-stability

# DynZ zone map
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
