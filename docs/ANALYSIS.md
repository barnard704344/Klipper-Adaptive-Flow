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

Six cards along the top give an at-a-glance health overview. Each card has a **?** tooltip explaining the metric, what "good" looks like, and when to worry:

| Card | What It Shows |
|------|---------------|
| **Material** | Filament type and print duration (or "PRINTING" + elapsed time during a live print) |
| **Temp Boost** | Average and max temperature boost applied by Adaptive Flow |
| **Heater Duty** | Average and max PWM duty cycle — flags saturation risk |
| **DynZ** | Percentage of layers where acceleration was reduced for complex geometry |
| **Banding** | Number of high-risk events and the diagnosed culprit |
| **Vib Score** | ADXL vibration quality score (0–100). Only appears when vibration data exists for the selected print. Color-coded: green (≥80), amber (50–79), red (<50) |

In aggregate mode, cards display weighted averages across all prints of that material, with print count and total duration.

### Tab Navigation

Below the cards, tabs switch between analysis views. Each tab has a **?** tooltip describing what the chart shows and how to interpret it:

| Tab | Contents |
|-----|----------|
| **⚙ Recommendations** | Actionable tuning suggestions with one-click Apply buttons |
| **✂ Slicer** | Slicer settings extracted from G-code, acceleration fingerprint chart, and specific setting recommendations |
| **Timeline** | Temperature and flow/speed/PWM charts over time |
| **Z-Height** | Banding risk bar chart by Z-layer + problem zone breakdown |
| **Heater** | PWM vs flow-rate brackets + thermal lag episodes |
| **PA** | Pressure Advance value over time + oscillation zone table |
| **DynZ** | DynZ activation percentage and stress by Z-height |
| **Distribution** | Speed and flow rate histograms — where your printer spends its time |
| **Trends** | Print-over-print line charts tracking metrics across sessions |
| **Vibration** | ADXL vibration analysis — quality score, per-feature breakdown, banding correlation, accel recommendations |

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

> **Note:** The Slicer tab is hidden in aggregate mode since each print has its own G-code file. It only appears when viewing an individual print session.

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
- Vibration Score trend line (dashed green) — only shown when at least one print has vibration data
- Trend direction arrows (↑ worsening / ↓ improving) with percentage change

### Vibration Tab

Requires an ADXL345 accelerometer configured in Klipper. The system automatically samples vibration during prints — no manual action needed.

**How sampling works:**
- Samples a 0.5-second burst of accelerometer data at ~3200 Hz every 5 minutes
- Defers sampling when the toolhead is moving faster than 80 mm/s (to avoid interfering with prints)
- Uses exponential backoff on failures (doubles interval, caps at 30 min) to prevent MCU overload
- First sample is taken 60 seconds into the print (after first layer stabilises)

**Dashboard sections:**

#### Print Vibration Summary

Top-level metrics from all ADXL samples collected during the print:

| Metric | Meaning |
|--------|---------|
| **Quality Score** | 0–100 weighted score. 80+ = excellent, 50–79 = moderate, <50 = poor |
| **X / Y RMS** | Average and peak vibration amplitude per axis (mm/s²) |
| **Mag RMS** | Combined magnitude RMS — the single best number for overall vibration |
| **Mag Peak** | Highest instantaneous vibration spike observed |
| **Dom. Freq X / Y** | Dominant vibration frequency per axis (Hz) — compare with input shaper frequencies |

**Quality Score breakdown** (shown below the score):

| Component | Weight | What It Measures |
|-----------|--------|------------------|
| **RMS** | 40% | Overall vibration magnitude — lower is better. 0 mm/s² → 100, 1000+ → 0 |
| **Balance** | 15% | How similar X and Y vibration are. Imbalanced axes suggest belt tension issues |
| **Peak** | 20% | Crest factor (peak/average). Low spikes relative to average = better control |
| **Consistency** | 25% | Variation between features (walls vs infill vs travel). Consistent = good |

#### Vibration by Feature / Acceleration

Breaks down vibration by slicer feature (identified via acceleration value):

- Maps each acceleration value to its slicer feature(s) (e.g., 2975 → Outer Wall / Inner Wall)
- Shows X RMS, Y RMS, Mag RMS, average speed, and Z range per feature
- **Recommendation column**: compares each feature's vibration against the quietest feature
  - **✓ OK** — within 1.5× of baseline
  - **↓ [accel]** — suggests a reduced acceleration value with percentage reduction
- If any features are flagged for reduction, ready-to-use `SET_VELOCITY_LIMIT ACCEL=...` G-code commands are shown below the table

#### Vibration × Banding Correlation

Cross-references banding risk events with ADXL samples at matching Z-heights (±1.5 mm tolerance):

- Shows Z-height, banding risk score, vibration RMS, acceleration, speed, and probable cause
- **Strong correlations = mechanical cause confirmed** — the banding at that layer was caused by actual vibration, not just accel changes or PA transitions
- Up to 20 entries, sorted by banding risk (highest first)

#### Vibration Over Print Progress

Line chart of X, Y, and Magnitude RMS plotted against print progress percentage. Shows how vibration evolves through the print — useful for spotting:
- Speed/feature transitions causing vibration spikes
- Mechanical issues that worsen as the print gets taller (Z height)
- Specific layers where vibration peaks

#### Vibration Insights

Actionable recommendations based on the vibration data:
- High overall vibration warnings (X or Y RMS above 500)
- Axis imbalance detection (>40% difference between X and Y)
- Input shaper frequency mismatch warnings (dominant freq vs configured shaper freq)
- Noisiest feature identification with suggestions

---

## API Endpoints

The dashboard exposes a JSON API:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/data` | GET | Latest print data (or live print if active) |
| `/api/data?session=<file>` | GET | Data for a specific completed print |
| `/api/material-data?material=PLA` | GET | Aggregate analysis for a given material |
| `/api/apply-config` | POST | Apply a config recommendation (JSON body: `{variable, value}`) |

The `/api/data` response includes `slicer_settings` (dict of all extracted G-code settings), `slicer_diagnosis` (accel fingerprint, issues, suggestions), and `recommendations` (including Slicer-category items when issues are found).

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
| `slicer_accel_control` | Slicer inserting accel G-code | Reduce distinct accel values in slicer, or match inner/outer wall accels. See Slicer tab for specific settings |
| `no_obvious_culprit` | Low event counts | Check mechanical causes (Z-wobble, filament) |

### Thermal Lag Detection

A lag episode is recorded whenever actual nozzle temperature falls more than 3°C (default) behind target. Each episode tracks duration, max lag, flow rate, PWM duty, and Z range. If PWM is near 100% during lag, the heater is at its physical limit; if PWM is below 90%, increasing `ramp_rate_rise` can help the heater respond faster.

### Heater Headroom Brackets

Samples are grouped by flow rate (mm³/s) and the average, P95, and max PWM are computed for each bracket. The bracket where P95 PWM crosses 95% marks your heater's effective flow limit. Below that threshold you have headroom to increase `flow_k`; above it, reduce `flow_k` or `max_boost_limit`.

> **Important:** Max PWM hitting 100% in the graphs is **normal PID behavior**, not heater saturation. Klipper's PID briefly goes full power on every temperature transition. The dashboard distinguishes between transient PID ramp-up (harmless) and sustained saturation (problematic) by checking average PWM and thermal lag — not just peak PWM. A 40W heater showing 100% max in the charts but 70% average with low thermal lag is working perfectly.

### Heater Recommendation Intelligence

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

### Vibration Quality Score (0–100)

The vibration quality score combines four weighted sub-scores:

```
Score = (Mag_RMS × 0.40) + (Axis_Balance × 0.15) + (Peak_Control × 0.20) + (Feature_Consistency × 0.25)
```

- **Mag RMS (40%):** Maps average magnitude RMS onto 0–100 scale (0 mm/s² = 100, 1000+ mm/s² = 0)
- **Axis Balance (15%):** Penalises imbalance between X and Y RMS. 50%+ difference = score of 0
- **Peak Control (20%):** Based on crest factor (peak ÷ RMS). Crest ≤1.5 = 100, crest ≥5.5 = 0
- **Feature Consistency (25%):** How much vibration varies between different print features. Low variation = high score

Interpretation:
- **80–100:** Excellent. Printer is mechanically sound, belts properly tensioned
- **50–79:** Moderate. Some features are noisy — check the per-feature table for specific recommendations
- **<50:** Poor. Likely belt tension issues, loose components, or excessive acceleration

### Per-Feature Accel Recommendations

For each slicer feature (identified by acceleration value), vibration is compared against the quietest feature as a baseline:

- **Below 1.5× baseline:** marked as OK — no action needed
- **Above 1.5× baseline:** a reduced acceleration is suggested, proportional to how far above baseline:
  - 1.5× → 12.5% reduction
  - 2.0× → 25% reduction
  - 3.0× → 50% reduction (maximum)
- Suggested values are rounded to the nearest 500 mm/s² (minimum 500)
- Ready-to-use `SET_VELOCITY_LIMIT ACCEL=` G-code is provided for each recommendation

### Banding × Vibration Correlation

For prints with both banding events and ADXL data, the system cross-references by Z-height:

1. Each high-risk banding moment is matched to the nearest ADXL sample within ±1.5 mm Z tolerance
2. A probable cause is classified based on vibration level and event flags (accel change, PA transition, temperature shift)
3. Results are de-duplicated by Z-height (keeping strongest) and limited to 20 entries

This provides evidence-based diagnosis: "the banding at Z=15mm was accompanied by 450 mm/s² vibration during an acceleration change" vs "the banding at Z=20mm had minimal vibration — likely a temperature issue instead."

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

# Start dashboard without ADXL sampling (prevents timer-too-close on slow boards)
python3 analyze_print.py --serve --port 7127 --no-adxl
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

### Vibration tab shows "No vibration data"

The ADXL auto-sampler requires:
- An ADXL345 accelerometer configured in Klipper (`[adxl345]` section in `printer.cfg`)
- At least one completed print **after** the vibration feature was installed
- The dashboard service must have been running during the print (not just started after)

Older prints stored before the update won't have vibration data — this is normal. The quality score, per-feature recommendations, and banding correlation will appear after your next print.

### ADXL "timer too close" errors

If ADXL sampling causes MCU timing errors:

```bash
# Option 1: Disable ADXL sampling entirely
# Edit /etc/systemd/system/adaptive-flow-dashboard.service
# Add --no-adxl to the ExecStart line
sudo systemctl daemon-reload && sudo systemctl restart adaptive-flow-dashboard

# Option 2: The system handles this automatically
# Sampling uses 0.5s bursts (not 2s), defers when toolhead is fast,
# and backs off exponentially on failures (up to 30 min between retries)
```

The auto-sampler is designed to be safe: short bursts, speed-aware deferral, and automatic backoff. Most printers handle it fine. The `--no-adxl` flag is a fallback for boards that can't handle any concurrent I2C/SPI traffic during printing.
