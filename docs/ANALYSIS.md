# Print Analysis — Banding Detection & Print Stats

The `analyze_print.py` tool provides ten modes:

1. **Single-print stats** — Quick health summary from the latest (or a specific) print
2. **Multi-print banding analysis** — Aggregates data across N prints to identify banding culprits
3. **Z-height banding heatmap** — Shows which layers have the most banding risk
4. **Print-over-print trends** — Tracks whether your config changes are helping
5. **Thermal lag report** — Identifies when the heater can't keep up with demand
6. **Heater headroom analysis** — Shows max safe flow rate before heater saturates
7. **PA stability analysis** — Detects PA oscillation zones that cause ribbing
8. **DynZ zone map** — Visualizes where DynZ was active by Z-height
9. **Speed/flow distribution** — Shows where your printer spends its time
10. **Web dashboard** — Interactive browser-based dashboard with Chart.js charts
7. **PA stability analysis** — Detects PA oscillation zones that cause ribbing

**No API keys or external services required.** Everything runs locally using your print logs.

---

## Single-Print Stats

After a print completes:

```bash
cd ~/Klipper-Adaptive-Flow
python3 analyze_print.py
```

This automatically finds the most recent print log and displays a summary:

```
File: benchy_20260225_143012_summary.json
============================================================
Material : PLA
Duration : 42.3 min  (2538 samples)
Boost    : avg 8.2°C / max 18.5°C
Heater   : avg 62% / max 81%
DynZ     : active 12% of print, min accel 2400 mm/s²
Banding  : 3 high-risk events — culprit: none

  ✓  Print looks healthy
============================================================
```

### Health Warnings

The tool flags common issues:

| Warning | Meaning |
|---------|---------|
| Heater near saturation (max PWM > 95%) | Heater can't keep up — reduce `flow_k` or `max_boost_limit` |
| High average heater duty (>85%) | Sustained load — consider slower speeds or lower boost |
| Large temp boost | `flow_k` or `max_boost_limit` may be too aggressive |
| High-risk banding events | Run `--count` analysis to identify the pattern |

### Analyze a Specific Print

```bash
python3 analyze_print.py /path/to/print_summary.json
```

---

## Multi-Print Banding Analysis

Aggregates data across multiple prints to identify consistent banding culprits.

### Usage

```bash
# Analyze last 10 prints
python3 analyze_print.py --count 10

# Filter by material
python3 analyze_print.py --count 10 --material PLA

# Analyze last 20 prints
python3 analyze_print.py --count 20
```

### What It Detects

The logging system tracks state transitions that cause banding:

| Event Type | What It Detects |
|------------|-----------------|
| **Accel changes** | Mid-layer acceleration switching (banding) |
| **PA changes** | PA oscillation causing ribbing |
| **DynZ transitions** | DynZ activation causing accel changes |
| **Temp overshoots** | Temperature instability |

Each print is diagnosed with a likely culprit. Multi-print analysis confirms patterns.

### Example Output

```
======================================================================
  BANDING ANALYSIS (10 prints)
======================================================================

Total printing time: 187.3 minutes
Materials: {'PLA': 10}

──────────────────────────────────────────────────────────────────────
  BANDING RISK OVERVIEW
──────────────────────────────────────────────────────────────────────
High-risk events: 423 (avg 42.3/print)
Accel changes: 387 (avg 38.7/print)
PA changes: 108 (avg 10.8/print)
DynZ transitions: 241 (avg 24.1/print)

──────────────────────────────────────────────────────────────────────
  DIAGNOSIS
──────────────────────────────────────────────────────────────────────
Most common culprit: dynz_accel_switching
Breakdown:
  - dynz_accel_switching: 9 prints
  - pa_oscillation: 1 print

──────────────────────────────────────────────────────────────────────
  RECOMMENDED FIX
──────────────────────────────────────────────────────────────────────
⚠  DynZ changing acceleration causes banding

FIX: Set variable_dynz_relief_method: 'temp_reduction'
```

### Banding Culprits

| Culprit | Cause | Fix |
|---------|-------|-----|
| `dynz_accel_switching` | DynZ changing acceleration | `dynz_relief_method: 'temp_reduction'` |
| `pa_oscillation` | PA changing too much | Lower `pa_boost_k` |
| `temp_instability` | Temperature oscillating | Lower ramp rates, check PID |
| `slicer_accel_control` | Slicer inserting accel commands | Disable firmware accel in slicer |
| `no_obvious_culprit` | Low event counts | Check mechanical (Z-wobble, filament) |

### CSV Logging Reference

Enhanced logging tracks these columns for banding analysis:

| Column | Description |
|--------|-------------|
| `pa_delta` | PA change from last sample |
| `accel_delta` | Acceleration change |
| `temp_target_delta` | Target temp change |
| `temp_overshoot` | Actual - Target temp |
| `dynz_transition` | DynZ state change (1=ON, -1=OFF) |
| `layer_transition` | Layer change detected |
| `banding_risk` | Risk score 0-10 |
| `event_flags` | Human-readable events (e.g., "ACCEL_CHG:+1200") |

**Banding Risk Score (0-10):**
- +3: Accel change >500 mm/s²
- +2: PA change >0.005
- +2: Temp change >3°C
- +2: DynZ state transition
- +1: Temp overshoot >5°C

Score ≥5 = high risk event (likely visible artifact)

### Debugging Workflow

1. **Print 5-10 test cubes** with logging enabled (happens automatically)
2. **Run banding analysis:**
   ```bash
   python3 analyze_print.py --count 10
   ```
3. **Check consistency**: If 8+ prints show same culprit → confirmed diagnosis
4. **Apply fix** from recommendations
5. **Verify**: Print another cube, check if high-risk events drop to near zero

---

## Z-Height Banding Heatmap

Shows banding risk broken down by Z-height layer bins. Lets you correlate visible banding lines on your part to specific events in the data.

### Usage

```bash
# Latest print
python3 analyze_print.py --z-map

# Specific print
python3 analyze_print.py --z-map my_print_summary.json

# Custom bin size (default 0.5mm)
python3 analyze_print.py --z-map --z-bin 1.0
```

### Example Output

```
======================================================================
  Z-HEIGHT BANDING HEATMAP
======================================================================

     Z range  Avg risk  Events  Bar
──────────────────────────────────────────────────────────────────────
 0.0-0.5mm       1.2       0  ████░░░░░░░░░░░░░░░░░░░░░░░░░░
 0.5-1.0mm       0.8       0  ███░░░░░░░░░░░░░░░░░░░░░░░░░░░
 5.0-5.5mm       7.3      12  ██████████████████████████░░░░  <-- PROBLEM
 5.5-6.0mm       6.1       8  ████████████████████████░░░░░░  <-- PROBLEM

──────────────────────────────────────────────────────────────────────
  PROBLEM ZONES
──────────────────────────────────────────────────────────────────────

  Z 5.0-5.5mm  (avg risk 7.3, 12 high-risk events)
    Caused by: 9 accel changes, 3 DynZ transitions

  Z 5.5-6.0mm  (avg risk 6.1, 8 high-risk events)
    Caused by: 6 accel changes, 2 PA changes
```

### How to Use It

1. Print something and notice banding at a specific height
2. Run `--z-map` and find the matching Z range
3. The "Caused by" line tells you what triggered it (accel, PA, DynZ, etc.)
4. Apply the appropriate fix from the banding culprits table

---

## Print-Over-Print Trends

Compares key metrics across your last N prints to show whether your config changes are helping or hurting.

### Usage

```bash
# Trends across last 10 prints
python3 analyze_print.py --trend 10

# Filter by material
python3 analyze_print.py --trend 10 --material PLA
```

### Example Output

```
======================================================================
  PRINT-OVER-PRINT TRENDS (10 prints, oldest → newest)
======================================================================

Print         Boost  Heater  Risk Ev Culprit
──────────────────────────────────────────────────────────────────────
2026-02-16     12.3°C     78%       42 dynz_accel_switching
2026-02-17     11.8°C     75%       38 dynz_accel_switching
2026-02-18      9.1°C     68%       15 pa_oscillation
2026-02-20      8.4°C     65%        8 none
2026-02-22      7.9°C     63%        3 none

──────────────────────────────────────────────────────────────────────
  TREND DIRECTION
──────────────────────────────────────────────────────────────────────
  Avg boost          ↓  35.2% down  (improving)
  Heater duty        ↓  18.7% down  (improving)
  Banding events     ↓  92.1% down  (improving)
```

### How to Use It

1. Make a config change (e.g., switch DynZ relief method)
2. Print a few test objects
3. Run `--trend 10` to see if metrics are trending down
4. **Improving** = your change helped. **Worsening** = revert it.

---

## Thermal Lag Report

Shows moments where the actual nozzle temperature fell behind the target — i.e., the heater couldn't keep up with demand.

### Usage

```bash
python3 analyze_print.py --lag

# Custom threshold (default 3°C)
python3 analyze_print.py --lag --lag-threshold 5.0
```

### What It Shows

- **Overall stats**: average lag, max lag, % of print time in lag
- **Lag episodes**: each period where temp fell behind, with duration, max lag, flow rate at the time, PWM duty, and Z range
- **Recommendations**: whether the issue is heater saturation (physical limit) or ramp rate (software config)

### How to Use It

1. Run `--lag` after a print where you suspect under-temperature
2. If worst episodes show PWM near 100% → heater is at its limit, reduce demand
3. If PWM is <90% during lag → increase `ramp_rate_rise` so the heater responds faster

---

## Heater Headroom Analysis

Groups all CSV samples by flow rate brackets and shows how much heater capacity remains at each level.

### Usage

```bash
python3 analyze_print.py --headroom
```

### Example Output

```
======================================================================
  HEATER HEADROOM ANALYSIS
======================================================================

Flow rate vs heater duty — shows how much capacity remains.

 Flow (mm³/s)  Samples  Avg PWM  P95 PWM  Max PWM  Headroom
──────────────────────────────────────────────────────────────────────
         0-2      450      32%      45%      52%  ████████████████████ 55%
         2-5     1200      48%      62%      71%  ████████████████░░░░ 38%
        5-8      800      65%      78%      85%  ████████████░░░░░░░░ 22%
       8-10      300      78%      89%      94%  ████████░░░░░░░░░░░░ 11%
      10-12      120      86%      96%      99%  ██░░░░░░░░░░░░░░░░░░  4% SATURATED
```

### How to Use It

1. Find the flow bracket where P95 PWM crosses 95% → that's your heater's effective limit
2. If you regularly print above that flow rate, reduce `flow_k` or `max_boost_limit`
3. If all brackets show plenty of headroom, you can safely increase `flow_k`

---

## PA Stability Analysis

Analyzes Pressure Advance value changes over time and detects oscillation zones where PA bounces rapidly — a common cause of visible ribbing.

### Usage

```bash
python3 analyze_print.py --pa-stability
```

### What It Shows

- **PA range and stdev**: how much PA varied during the print
- **Change count**: number of significant PA changes (>±0.003)
- **Oscillation zones**: time periods where PA changed ≥4 times within 10 seconds
- **Recommendations**: whether to increase `pa_deadband` or lower `pa_boost_k`

### How to Use It

1. If you see ribbing on your prints, run `--pa-stability`
2. Check if oscillation zones correlate with ribbing locations (Z height)
3. If many oscillation zones: increase `pa_deadband` (try 0.005+)
4. If PA range is very wide (>0.02): lower `pa_boost_k`

---

## DynZ Zone Map

Visualizes DynZ activation patterns by Z-height, showing where Dynamic Z-Window reduced acceleration to protect print quality.

### Usage

```bash
python3 analyze_print.py --dynz-map

# Custom bin size (shares --z-bin with --z-map)
python3 analyze_print.py --dynz-map --z-bin 1.0
```

### Example Output

```
======================================================================
  DYNZ ZONE MAP
======================================================================

     Z range  Active  Trans  Avg Accel  Stress  Bar
──────────────────────────────────────────────────────────────────────
 0.0-0.5mm     0.0%      0       5000     0.12  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
 5.0-5.5mm    62.3%      4       3200     8.41  ████████████████████░░░░░░░░░░  <-- HIGH
 5.5-6.0mm    41.1%      2       3800     5.23  █████████████░░░░░░░░░░░░░░░░░

──────────────────────────────────────────────────────────────────────
  SUMMARY
──────────────────────────────────────────────────────────────────────
  Avg activation: 12.4%
  Total transitions: 6
  High-activity zones: 5.0mm

  ⚠ DynZ heavily active at those heights.
    → Check for thin walls, overhangs, or rapid geometry changes
    → If banding appears there, try dynz_relief_method: 'temp_reduction'
```

### How to Use It

1. If you see banding at specific heights, run `--dynz-map`
2. Check if DynZ was highly active at those same Z heights
3. If DynZ activation correlates with banding → switch to `dynz_relief_method: 'temp_reduction'`
4. High "Stress" values indicate layers with challenging geometry (thin walls, rapid speed changes)

---

## Speed/Flow Distribution

Shows how your printer spends its time across different speed and flow rate brackets.

### Usage

```bash
python3 analyze_print.py --distribution
```

### Example Output

```
======================================================================
  SPEED / FLOW DISTRIBUTION
======================================================================

──────────────────────────────────────────────────────────────────────
  SPEED DISTRIBUTION
──────────────────────────────────────────────────────────────────────

  Speed (mm/s)  % Time  Samples  Avg Boost   Avg PWM  Bar
──────────────────────────────────────────────────────────────────────
          0-25    12.3%      310       2.1°C      38%  ████████░░░░░░░░░░░░
        25-50    28.7%      722       4.8°C      52%  ████████████████████
        50-75    31.2%      785       7.2°C      64%  ████████████████████
       75-100    18.4%      463       9.8°C      71%  █████████████░░░░░░░
      100-125     7.1%      179      12.3°C      78%  █████░░░░░░░░░░░░░░░
      125-150     2.3%       58      15.1°C      84%  ██░░░░░░░░░░░░░░░░░░
```

### How to Use It

1. Find which speed/flow bracket gets the most time → that's where tuning matters most
2. If most time is at low speed, your slicer speeds may be throttled by acceleration limits
3. Compare the boost and PWM at each bracket to check if your `flow_k` scaling is appropriate
4. If high-speed brackets are barely used, consider lowering max speed in slicer to match reality

---

## Web Dashboard

Interactive browser-based dashboard with all analysis modes in one view, using Chart.js for interactive charts. **No SSH required** — the dashboard runs as a system service and is accessible from any browser on your network.

### Automatic Setup

The dashboard service is installed automatically by `update.sh`. After running the update script, the dashboard is accessible at:

```
http://<printer-ip>:7127
```

The service starts on boot and restarts automatically if it crashes.

### Manual Control

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

### Manual Start (without service)

If you prefer not to use the systemd service:

```bash
python3 ~/Klipper-Adaptive-Flow/analyze_print.py --serve
python3 ~/Klipper-Adaptive-Flow/analyze_print.py --serve --port 8080
python3 ~/Klipper-Adaptive-Flow/analyze_print.py --serve --material PLA
```

### Features

- **Real-time monitoring** — live-updating charts during an active print (5-second refresh)
- **LIVE indicator** — pulsing green dot and badge when a print is in progress
- **Summary cards** — Material, boost, heater duty, DynZ, banding risk at a glance
- **Temperature timeline** — Interactive chart of target vs actual temperature + boost
- **Flow & speed timeline** — Flow rate, speed, and PWM over time
- **Z-height banding heatmap** — Bar chart showing risk by Z layer
- **Heater headroom** — PWM by flow bracket + thermal lag episodes
- **PA stability** — PA value over time + oscillation zone table
- **DynZ zone map** — DynZ activation percentage by Z-height
- **Speed/flow distribution** — Side-by-side histograms of time spent in each bracket
- **Print-over-print trends** — Line chart tracking metrics across prints

### Real-Time Monitoring

The dashboard automatically detects an active print (a CSV log with no summary JSON, modified within the last 2 minutes). When a print is in progress:

- A **LIVE** indicator appears in the header
- **Auto-refresh enables automatically** at 5-second intervals
- Summary cards show "PRINTING" badge and elapsed time
- Charts update in-place without full page reloads (using `/api/data` endpoint)
- Once the print finishes, the dashboard switches to the completed summary

When viewing completed prints, auto-refresh runs at 30-second intervals (if enabled).

### Dashboard Controls

| Control | Description |
|---------|-------------|
| **Session selector** | Dropdown to switch between past prints (shows "LIVE PRINT" when active) |
| **Auto-refresh** | Enabled automatically during live prints (5s); optional for completed prints (30s) |
| **Tab navigation** | Switch between Timeline, Z-Height, Heater, PA, DynZ, Distribution, Trends |

### API Endpoint

The dashboard exposes a JSON API for programmatic access:

```
GET /api/data                    # Latest print (or live print if active)
GET /api/data?session=<file>     # Specific completed print
```

Returns all analysis data as JSON — useful for custom integrations or external dashboards.

### Requirements

- Python 3 (uses built-in `http.server` — no extra dependencies)
- Chart.js loaded from CDN (requires internet access on first page load; cached afterward)
- Port 7127 must be accessible from your browser's network

### How to Use It

1. Open `http://<printer-ip>:7127` in any browser
2. If a print is running, you'll see **LIVE** — charts update automatically every 5s
3. Once the print finishes, it seamlessly switches to the completed summary
4. Use the session dropdown to browse past prints
5. Use tabs to explore different analysis views

---

## Custom Log Directory

If your logs are in a non-standard location:

```bash
python3 analyze_print.py --log-dir /path/to/logs
python3 analyze_print.py --count 10 --log-dir /path/to/logs
```

---

## Troubleshooting

### "No logs found"

Make sure you've completed at least one print with Adaptive Flow enabled.

Check if logs exist:
```bash
ls ~/printer_data/logs/adaptive_flow/
```

### "Summary contains 0 samples"

The print may have been too short, or `AT_START`/`AT_END` weren't called in your start/end G-code.
