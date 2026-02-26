# Print Analysis — Banding Detection & Print Stats

The `analyze_print.py` tool provides two modes:

1. **Single-print stats** — Quick health summary from the latest (or a specific) print
2. **Multi-print banding analysis** — Aggregates data across N prints to identify banding culprits

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
