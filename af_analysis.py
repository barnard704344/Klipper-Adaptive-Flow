"""
Adaptive Flow — Per-print analysis functions.

This module contains all the statistical analysis functions that operate on
a single print's CSV log: banding detection, Z-height heatmaps, thermal lag,
heater headroom, PA stability, DynZ zone mapping, speed/flow distribution,
and boost optimization.

Import from this module via analyze_print.py or af_slicer as needed.
"""

import os
import csv
import json
import statistics
import math
import logging
import threading
import time
from pathlib import Path
from collections import defaultdict

from af_config import LOG_DIR, CONFIG_DIR, _get_config_value, load_csv_rows


# =============================================================================
# TTL RESULT CACHE — avoid re-analyzing on rapid refreshes
# =============================================================================


_cache_lock = threading.Lock()
_cache_store = {}  # key → (timestamp, value)
_CACHE_TTL = 15    # seconds — stale after this


def _cache_get(key):
    """Return cached value if still fresh, else None."""
    with _cache_lock:
        entry = _cache_store.get(key)
        if entry and (time.time() - entry[0]) < _CACHE_TTL:
            return entry[1]
    return None


def _cache_set(key, value):
    """Store *value* in the cache under *key* with current timestamp."""
    with _cache_lock:
        _cache_store[key] = (time.time(), value)


def _cache_invalidate(prefix=None):
    """Drop all cache entries, or only those whose key starts with *prefix*."""
    with _cache_lock:
        if prefix is None:
            _cache_store.clear()
        else:
            for k in list(_cache_store):
                if k.startswith(prefix):
                    del _cache_store[k]


def find_latest_summary(log_dir):
    """Find the most recent *_summary.json file."""
    summaries = sorted(
        Path(log_dir).glob('*_summary.json'),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return str(summaries[0]) if summaries else None


def load_summary(path):
    """Load a print summary JSON, handling corrupted files gracefully."""
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {'_error': 'Summary file does not contain a JSON object'}
        return data
    except json.JSONDecodeError as exc:
        return {'_error': f'Invalid JSON: {exc}'}
    except Exception as exc:
        return {'_error': str(exc)}


def print_single_summary(summary, path):
    """Display a concise health summary for one print."""
    print(f"\nFile: {os.path.basename(path)}")
    print("=" * 60)

    material = summary.get('material', 'Unknown')
    duration = summary.get('duration_min', 0)
    samples  = summary.get('samples', 0)
    avg_boost = summary.get('avg_boost', 0)
    max_boost = summary.get('max_boost', 0)
    avg_pwm   = summary.get('avg_pwm', 0)
    max_pwm   = summary.get('max_pwm', 0)

    print(f"Material : {material}")
    print(f"Duration : {duration:.1f} min  ({samples} samples)")
    print(f"Boost    : avg {avg_boost:.1f}\u00b0C / max {max_boost:.1f}\u00b0C")
    print(f"Heater   : avg {avg_pwm:.0%} / max {max_pwm:.0%}")

    # DynZ
    dynz_pct  = summary.get('dynz_active_pct', 0)
    accel_min = summary.get('accel_min', 0)
    if dynz_pct > 0:
        print(f"DynZ     : active {dynz_pct}% of print, min accel {accel_min} mm/s\u00b2")
    else:
        print(f"DynZ     : inactive (no stress zones)")

    # Banding summary from extruder_monitor
    ba = summary.get('banding_analysis', {})
    if ba:
        hr = ba.get('high_risk_events', 0)
        culprit = ba.get('likely_culprit', 'none')
        print(f"Legacy   : {hr} risk events (deprecated \u2014 see Quality Score)")
    else:
        print("Legacy   : no banding data")

    # Quick health verdict
    print()
    warnings = []
    if max_pwm > 0.95:
        warnings.append("Heater near saturation (max PWM > 95%)")
    if avg_pwm > 0.85:
        warnings.append("High average heater duty (>85%)")
    if max_boost > 30:
        warnings.append(f"Large temp boost ({max_boost:.0f}\u00b0C) \u2014 check flow_k / max_boost_limit")

    if warnings:
        for w in warnings:
            print(f"  \u26a0  {w}")
    else:
        print("  \u2713  Print looks healthy")

    print("=" * 60)


# =============================================================================
# MULTI-PRINT BANDING ANALYSIS
# =============================================================================

def find_recent_sessions(log_dir, count=None, material=None):
    """Find recent print sessions, optionally filtered by material."""
    sessions = []

    for file in Path(log_dir).glob('*_summary.json'):
        try:
            with open(file, 'r') as f:
                summary = json.load(f)

            if material and summary.get('material', '').upper() != material.upper():
                continue

            csv_file = str(file).replace('_summary.json', '.csv')
            if not os.path.exists(csv_file):
                continue

            sessions.append({
                'summary_file': str(file),
                'csv_file': csv_file,
                'summary': summary,
                'timestamp': summary.get('start_time', ''),
            })
        except Exception as exc:
            print(f"Warning: Could not read {file}: {exc}")

    sessions.sort(key=lambda x: x['timestamp'], reverse=True)

    if count:
        sessions = sessions[:count]

    return sessions


# =========================================================================
# EXTRUSION QUALITY SCORE — physics-based print quality predictor
# =========================================================================
# Replaces the old "banding risk" event counter which just tallied
# potential causes without checking if they actually affected extrusion.
#
# Four sub-scores (each 0-100, higher = better) that each map to a
# measurable physical quantity AND a specific remediation:
#
#  1. Thermal Stability  — temp within ±1°C of target during extrusion
#  2. Flow Steadiness    — inverse of sample-to-sample flow rate jitter
#  3. Heater Reserve     — inverse of time PWM ≥95% during extrusion
#  4. Pressure Stability — inverse of accel-induced pressure transients
#
# Combined into a single 0-100 "Extrusion Quality" score.
# =========================================================================

def compute_extrusion_quality(timeline):
    """Compute extrusion quality score from timeline data.

    Parameters
    ----------
    timeline : list[dict]
        Timeline points with keys: ta (temp actual), tt (temp target),
        f (flow mm³/s), pw (pwm 0-1), a (accel mm/s²), fn (fan %).

    Returns
    -------
    dict with keys:
        score       : int 0-100 overall quality
        thermal     : int 0-100 thermal stability sub-score
        flow        : int 0-100 flow steadiness sub-score
        heater      : int 0-100 heater reserve sub-score
        pressure    : int 0-100 pressure stability sub-score
        detail      : dict with raw metrics behind each sub-score
    Returns None if insufficient data.
    """
    if not timeline or len(timeline) < 20:
        return None

    # Filter to actively-extruding samples only (flow > 0.5 mm³/s)
    active = [pt for pt in timeline if pt.get('f', 0) > 0.5]
    if len(active) < 10:
        return None

    n = len(active)

    # ------------------------------------------------------------------
    # 1. THERMAL STABILITY — what % of extrusion time is temp on-target?
    #    Direct correlation: temp deviation → viscosity change → banding
    # ------------------------------------------------------------------
    temp_in_band = 0    # within ±1°C
    temp_close = 0      # within ±2°C
    temp_deviations = []
    for pt in active:
        dev = abs(pt.get('ta', 0) - pt.get('tt', 0))
        temp_deviations.append(dev)
        if dev <= 1.0:
            temp_in_band += 1
        if dev <= 2.0:
            temp_close += 1

    pct_in_band = temp_in_band / n * 100
    pct_close = temp_close / n * 100
    avg_dev = statistics.mean(temp_deviations) if temp_deviations else 0
    max_dev = max(temp_deviations) if temp_deviations else 0

    # Score: 100 if always in band, scales down.
    # Use a blend: 70% weight on ±1°C, 30% on ±2°C
    thermal_score = int(min(100, pct_in_band * 0.7 + pct_close * 0.3))

    # ------------------------------------------------------------------
    # 2. FLOW STEADINESS — how smooth is the commanded flow rate?
    #    Large sample-to-sample jumps = pressure transients = banding.
    #    This is what flow_smoothing is supposed to fix.
    # ------------------------------------------------------------------
    flows = [pt.get('f', 0) for pt in active]
    mean_flow = statistics.mean(flows)
    if mean_flow < 1.0:
        mean_flow = 1.0  # avoid div-by-zero on very low flow prints

    # Compute sample-to-sample flow deltas
    flow_deltas = [abs(flows[i] - flows[i - 1]) for i in range(1, len(flows))]
    avg_delta = statistics.mean(flow_deltas) if flow_deltas else 0
    # Normalize by mean flow: 0 = perfectly smooth, higher = jittery
    normalized_jitter = avg_delta / mean_flow

    # Count "big jumps" (>2 mm³/s delta) — these are the ones that
    # actually cause visible pressure artifacts
    big_jumps = sum(1 for d in flow_deltas if d > 2.0)
    big_jump_pct = big_jumps / max(len(flow_deltas), 1) * 100

    # Score: penalize jitter.  Typical good print: jitter < 0.05
    # Typical moderate: jitter 0.10-0.20. Bad: jitter > 0.30
    flow_score = int(max(0, min(100, 100 - normalized_jitter * 200)))
    # Extra penalty for big jumps, but capped to avoid double-floor
    if big_jump_pct > 5:
        flow_score = int(max(10, flow_score - big_jump_pct * 0.5))

    # ------------------------------------------------------------------
    # 3. HEATER RESERVE — how much headroom does the heater have?
    #    When PWM≥95% during extrusion, the heater can't respond to
    #    demand changes → temp drops → under-extrusion → banding.
    # ------------------------------------------------------------------
    pwm_saturated = sum(1 for pt in active if pt.get('pw', 0) >= 0.95)
    sat_pct = pwm_saturated / n * 100
    avg_pwm = statistics.mean([pt.get('pw', 0) for pt in active])

    # Score: 100 if never saturated, 0 if always saturated
    heater_score = int(max(0, min(100, 100 - sat_pct * 3)))
    # Also penalize high average PWM (little margin)
    if avg_pwm > 0.80:
        heater_score = int(max(0, heater_score - (avg_pwm - 0.80) * 100))

    # ------------------------------------------------------------------
    # 4. PRESSURE STABILITY — accel-induced pressure transients
    #    Large accel changes during high-flow extrusion cause pressure
    #    spikes that PA can't fully compensate for.
    # ------------------------------------------------------------------
    accels = [pt.get('a', 0) for pt in active]
    accel_deltas = [abs(accels[i] - accels[i - 1])
                    for i in range(1, len(accels))]

    # Weight by flow: a 3000 accel swing at 15 mm³/s matters much more
    # than the same swing at 2 mm³/s
    weighted_transients = []
    for i in range(1, len(accels)):
        ad = abs(accels[i] - accels[i - 1])
        fl = max(flows[i], flows[i - 1])
        if ad > 200 and fl > 2.0:
            # Normalize: 1000 accel delta at 10 mm³/s flow = 1.0 impact
            impact = (ad / 1000.0) * (fl / 10.0)
            weighted_transients.append(impact)

    avg_transient = (statistics.mean(weighted_transients)
                     if weighted_transients else 0)
    transient_count = len(weighted_transients)
    transient_pct = transient_count / max(n - 1, 1) * 100

    # Score: penalize transients
    # avg_transient of 1.0 = moderate (1000 accel swing at 10 mm³/s)
    # avg_transient of 3.0 = severe
    pressure_score = int(max(0, min(100, 100 - avg_transient * 15
                                    - transient_pct * 0.3)))

    # ------------------------------------------------------------------
    # OVERALL SCORE — weighted combination
    # ------------------------------------------------------------------
    overall = int(
        thermal_score * 0.35
        + flow_score * 0.30
        + heater_score * 0.20
        + pressure_score * 0.15
    )
    overall = max(0, min(100, overall))

    return {
        'score': overall,
        'thermal': thermal_score,
        'flow': flow_score,
        'heater': heater_score,
        'pressure': pressure_score,
        'detail': {
            'temp_in_band_pct': round(pct_in_band, 1),
            'temp_close_pct': round(pct_close, 1),
            'avg_temp_dev': round(avg_dev, 2),
            'max_temp_dev': round(max_dev, 1),
            'flow_jitter': round(normalized_jitter, 3),
            'big_jump_pct': round(big_jump_pct, 1),
            'big_jumps': big_jumps,
            'avg_flow_delta': round(avg_delta, 2),
            'mean_flow': round(mean_flow, 1),
            'pwm_saturated_pct': round(sat_pct, 1),
            'avg_pwm': round(avg_pwm, 3),
            'avg_transient_impact': round(avg_transient, 3),
            'transient_count': transient_count,
            'transient_pct': round(transient_pct, 1),
            'active_samples': n,
        },
    }


def analyze_csv_for_banding(csv_file):
    """Deep analysis of a CSV log file for banding-related events."""
    events = {
        'accel_spikes': [],
        'pa_oscillations': [],
        'temp_overshoots': [],
        'dynz_transitions': [],
        'high_risk_moments': [],
    }

    flow_values = []
    pa_values = []
    accel_values = []

    try:
        with open(csv_file, 'r') as f:
            reader = csv.DictReader(f)

            for row in reader:
                try:
                    elapsed = float(row['elapsed_s'])

                    if 'pa_delta' in row and abs(float(row['pa_delta'])) > 0.005:
                        events['pa_oscillations'].append({
                            'time': elapsed,
                            'delta': float(row['pa_delta']),
                            'z': float(row.get('z_height', 0)),
                        })

                    if 'accel_delta' in row and abs(float(row['accel_delta'])) > 500:
                        events['accel_spikes'].append({
                            'time': elapsed,
                            'delta': float(row['accel_delta']),
                            'z': float(row.get('z_height', 0)),
                        })

                    if 'temp_overshoot' in row and abs(float(row['temp_overshoot'])) > 5.0:
                        events['temp_overshoots'].append({
                            'time': elapsed,
                            'overshoot': float(row['temp_overshoot']),
                        })

                    if 'dynz_transition' in row and int(row['dynz_transition']) != 0:
                        events['dynz_transitions'].append({
                            'time': elapsed,
                            'state': 'ON' if int(row['dynz_transition']) > 0 else 'OFF',
                            'z': float(row.get('z_height', 0)),
                        })

                    if 'banding_risk' in row and int(row['banding_risk']) >= 5:
                        events['high_risk_moments'].append({
                            'time': elapsed,
                            'risk': int(row['banding_risk']),
                            'flags': row.get('event_flags', ''),
                            'z': float(row.get('z_height', 0)),
                        })

                    flow_values.append(float(row['flow']))
                    if 'pa' in row and float(row['pa']) > 0:
                        pa_values.append(float(row['pa']))
                    if 'accel' in row and int(row['accel']) > 0:
                        accel_values.append(int(row['accel']))

                except (KeyError, ValueError):
                    continue

    except Exception as exc:
        print(f"Warning: Could not analyze {csv_file}: {exc}")
        return None

    def safe_stdev(data):
        return statistics.stdev(data) if len(data) > 1 else 0.0

    return {
        'events': events,
        'variance': {
            'flow_stdev': safe_stdev(flow_values) if flow_values else 0.0,
            'pa_stdev': safe_stdev(pa_values) if pa_values else 0.0,
            'accel_stdev': safe_stdev(accel_values) if accel_values else 0.0,
        },
        'event_counts': {k: len(v) for k, v in events.items()},
    }


def aggregate_banding_analysis(sessions):
    """Aggregate banding data across multiple print sessions."""
    agg = {
        'session_count': len(sessions),
        'materials': defaultdict(int),
        'total_duration_min': 0,
        'total_samples': 0,
        'total_high_risk_events': 0,
        'total_accel_changes': 0,
        'total_pa_changes': 0,
        'total_dynz_transitions': 0,
        'total_temp_overshoots': 0,
        'culprits': defaultdict(int),
        'sessions': [],
    }

    for session in sessions:
        summary = session['summary']
        agg['materials'][summary.get('material', 'UNKNOWN')] += 1
        agg['total_duration_min'] += summary.get('duration_min', 0)
        agg['total_samples'] += summary.get('samples', 0)

        ba = summary.get('banding_analysis', {})
        agg['total_high_risk_events'] += ba.get('high_risk_events', 0)
        agg['total_accel_changes'] += ba.get('accel_changes', 0)
        agg['total_pa_changes'] += ba.get('pa_changes', 0)
        agg['total_dynz_transitions'] += ba.get('dynz_transitions', 0)
        agg['total_temp_overshoots'] += ba.get('temp_overshoots', 0)

        culprit = ba.get('likely_culprit', 'unknown')
        agg['culprits'][culprit] += 1

        csv_analysis = analyze_csv_for_banding(session['csv_file'])

        agg['sessions'].append({
            'filename': summary.get('filename', ''),
            'material': summary.get('material', ''),
            'start_time': summary.get('start_time', ''),
            'duration_min': summary.get('duration_min', 0),
            'banding_analysis': ba,
            'csv_analysis': csv_analysis,
        })

    if agg['session_count'] > 0:
        agg['avg_high_risk_per_print'] = round(
            agg['total_high_risk_events'] / agg['session_count'], 1)
        agg['avg_accel_changes_per_print'] = round(
            agg['total_accel_changes'] / agg['session_count'], 1)
        agg['avg_pa_changes_per_print'] = round(
            agg['total_pa_changes'] / agg['session_count'], 1)
        agg['avg_dynz_transitions_per_print'] = round(
            agg['total_dynz_transitions'] / agg['session_count'], 1)

    agg['most_common_culprit'] = (
        max(agg['culprits'], key=agg['culprits'].get) if agg['culprits'] else 'unknown'
    )

    return agg


def _diagnose_fix(culprit):
    """Return a human-readable diagnosis and fix for a banding culprit."""
    fixes = {
        'dynz_accel_switching': (
            "DynZ changing acceleration causes banding",
            "Set variable_dynz_relief_method: 'temp_reduction'",
        ),
        'pa_oscillation': (
            "PA oscillating too much",
            "Lower pa_boost_k or disable dynamic PA",
        ),
        'temp_instability': (
            "Temperature oscillating",
            "Lower ramp rates, check PID tuning",
        ),
        'slicer_accel_control': (
            "Slicer changing acceleration mid-print",
            "Disable firmware accel control in slicer",
        ),
    }
    diagnosis, fix = fixes.get(culprit, (
        "No obvious software culprit \u2014 check mechanical issues",
        "Inspect Z-axis (wobble, binding), filament path, extruder tension",
    ))
    return diagnosis, fix


def print_banding_report(agg):
    """Print the multi-print banding analysis report."""
    n = agg['session_count']
    print("\n" + "=" * 70)
    print(f"  BANDING ANALYSIS ({n} print{'s' if n != 1 else ''})")
    print("=" * 70 + "\n")

    print(f"Total printing time: {agg['total_duration_min']:.1f} minutes")
    print(f"Materials: {dict(agg['materials'])}\n")

    print("\u2500" * 70)
    print("  BANDING RISK OVERVIEW")
    print("\u2500" * 70)
    print(f"High-risk events: {agg['total_high_risk_events']} "
          f"(avg {agg.get('avg_high_risk_per_print', 0):.1f}/print)")
    print(f"Accel changes: {agg['total_accel_changes']} "
          f"(avg {agg.get('avg_accel_changes_per_print', 0):.1f}/print)")
    print(f"PA changes: {agg['total_pa_changes']} "
          f"(avg {agg.get('avg_pa_changes_per_print', 0):.1f}/print)")
    print(f"DynZ transitions: {agg['total_dynz_transitions']} "
          f"(avg {agg.get('avg_dynz_transitions_per_print', 0):.1f}/print)")
    print(f"Temp overshoots: {agg['total_temp_overshoots']}\n")

    print("\u2500" * 70)
    print("  DIAGNOSIS")
    print("\u2500" * 70)
    print(f"Most common cause: {_culprit_name(agg['most_common_culprit'])}")
    print("Breakdown:")
    for culprit, count in sorted(agg['culprits'].items(),
                                  key=lambda x: x[1], reverse=True):
        print(f"  - {_culprit_name(culprit)}: {count} print{'s' if count != 1 else ''}")

    culprit = agg['most_common_culprit']
    diagnosis, fix = _diagnose_fix(culprit)

    print("\n" + "\u2500" * 70)
    print("  RECOMMENDED FIX")
    print("\u2500" * 70)
    print(f"\u26a0  {diagnosis}\n")
    print(f"FIX: {fix}")

    # Individual print details
    print("\n" + "\u2500" * 70)
    print("  INDIVIDUAL PRINTS")
    print("\u2500" * 70)
    for i, s in enumerate(agg['sessions'][:5], 1):
        ba = s['banding_analysis']
        print(f"\n{i}. {s['filename']} ({s['material']}, {s['duration_min']:.1f}min)")
        print(f"   Events: {ba.get('high_risk_events', 0)} high-risk, "
              f"{ba.get('accel_changes', 0)} accel, {ba.get('pa_changes', 0)} PA")
        print(f"   Cause: {_culprit_name(ba.get('likely_culprit', 'unknown'))}")

    if len(agg['sessions']) > 5:
        print(f"\n   ... and {len(agg['sessions']) - 5} more")

    print("\n" + "=" * 70 + "\n")


# =============================================================================
# Z-HEIGHT BANDING HEATMAP
# =============================================================================

def analyze_z_banding(csv_file, bin_size=0.5, rows=None):
    """Analyze banding risk by Z-height bins from a single CSV log."""
    bins = defaultdict(lambda: {
        'samples': 0,
        'risk_sum': 0,
        'high_risk': 0,
        'accel_changes': 0,
        'pa_changes': 0,
        'dynz_transitions': 0,
        'events': [],
    })

    try:
        _rows = rows if rows is not None else load_csv_rows(csv_file)
        for row in _rows:
                try:
                    z = float(row.get('z_height', 0))
                    risk = int(row.get('banding_risk', 0))
                    bin_key = math.floor(z / bin_size) * bin_size

                    b = bins[bin_key]
                    b['samples'] += 1
                    b['risk_sum'] += risk

                    if risk >= 5:
                        b['high_risk'] += 1
                    if 'accel_delta' in row and abs(float(row['accel_delta'])) > 500:
                        b['accel_changes'] += 1
                    if 'pa_delta' in row and abs(float(row['pa_delta'])) > 0.005:
                        b['pa_changes'] += 1
                    if 'dynz_transition' in row and int(row['dynz_transition']) != 0:
                        b['dynz_transitions'] += 1

                    flags = row.get('event_flags', '')
                    if flags:
                        b['events'].append(flags)
                except (KeyError, ValueError):
                    continue
    except Exception as exc:
        print(f"Warning: Could not read {csv_file}: {exc}")
        return {}

    return dict(bins)


def _bar(value, max_value, width=30):
    """Render a horizontal bar using block characters."""
    if max_value <= 0:
        return ''
    filled = int(round(value / max_value * width))
    filled = min(filled, width)
    return '\u2588' * filled + '\u2591' * (width - filled)


def print_z_map(bins, bin_size=0.5):
    """Print the Z-height banding heatmap."""
    if not bins:
        print("No Z-height data available.")
        return

    sorted_z = sorted(bins.keys())
    max_risk = max(
        (b['risk_sum'] / b['samples'] if b['samples'] else 0)
        for b in bins.values()
    )
    max_risk = max(max_risk, 0.1)  # avoid division by zero

    print("\n" + "=" * 70)
    print("  Z-HEIGHT BANDING HEATMAP")
    print("=" * 70)
    print(f"\n{'Z range':>14}  {'Avg risk':>8}  {'Events':>6}  Bar")
    print("\u2500" * 70)

    problem_zones = []
    for z in sorted_z:
        b = bins[z]
        if b['samples'] == 0:
            continue
        avg_risk = b['risk_sum'] / b['samples']
        events = b['high_risk']
        z_end = z + bin_size
        label = f"{z:6.1f}-{z_end:.1f}mm"
        bar = _bar(avg_risk, max_risk)

        # Highlight high-risk zones
        marker = '  <-- PROBLEM' if avg_risk >= 4.0 or events >= 5 else ''
        print(f"{label:>14}  {avg_risk:>8.1f}  {events:>6}  {bar}{marker}")

        if avg_risk >= 4.0 or events >= 5:
            problem_zones.append({
                'z': z, 'z_end': z_end, 'avg_risk': avg_risk,
                'events': events,
                'accel': b['accel_changes'],
                'pa': b['pa_changes'],
                'dynz': b['dynz_transitions'],
            })

    # Summary of problem zones
    if problem_zones:
        print(f"\n\u2500" * 70)
        print("  PROBLEM ZONES")
        print("\u2500" * 70)
        for pz in problem_zones:
            print(f"\n  Z {pz['z']:.1f}-{pz['z_end']:.1f}mm  "
                  f"(avg risk {pz['avg_risk']:.1f}, {pz['events']} high-risk events)")
            parts = []
            if pz['accel']:
                parts.append(f"{pz['accel']} accel changes")
            if pz['pa']:
                parts.append(f"{pz['pa']} PA changes")
            if pz['dynz']:
                parts.append(f"{pz['dynz']} DynZ transitions")
            if parts:
                print(f"    Caused by: {', '.join(parts)}")
    else:
        print(f"\n\u2713 No problem zones detected — banding risk is low throughout.")

    print("\n" + "=" * 70 + "\n")


# =============================================================================
# PRINT-OVER-PRINT TRENDS
# =============================================================================

def print_trends(sessions):
    """Show key metrics trending across prints (oldest → newest)."""
    # Reverse so oldest is first (chronological)
    ordered = list(reversed(sessions))

    print("\n" + "=" * 70)
    print(f"  PRINT-OVER-PRINT TRENDS ({len(ordered)} prints, oldest \u2192 newest)")
    print("=" * 70)

    # Collect series
    labels = []
    boosts = []
    pwms = []
    risk_events = []
    culprits = []

    for s in ordered:
        summary = s['summary']
        ba = summary.get('banding_analysis', {})
        ts = summary.get('start_time', '')
        # Short label: date or filename
        if ts and len(ts) >= 10:
            label = ts[:10]
        else:
            label = summary.get('filename', '?')[:12]
        labels.append(label)
        boosts.append(summary.get('avg_boost', summary.get('auto_temp', {}).get('avg_boost', 0)))
        pwms.append(summary.get('avg_pwm', summary.get('heater', {}).get('avg_pwm', 0)))
        risk_events.append(ba.get('high_risk_events', 0))
        culprits.append(_culprit_name(ba.get('likely_culprit', '-')))

    # Print table
    n = len(ordered)
    col_w = max(12, max(len(l) for l in labels) + 1) if labels else 12

    print(f"\n{'Print':<{col_w}} {'Boost':>7} {'Heater':>8} {'Risk Ev':>8} Cause")
    print("\u2500" * 70)
    for i in range(n):
        pwm_str = f"{pwms[i]:.0%}" if isinstance(pwms[i], float) else f"{pwms[i]}"
        print(f"{labels[i]:<{col_w}} {boosts[i]:>6.1f}\u00b0C {pwm_str:>8} {risk_events[i]:>8} {culprits[i]}")

    # Trend arrows
    print(f"\n\u2500" * 70)
    print("  TREND DIRECTION")
    print("\u2500" * 70)

    def _trend(values):
        """Simple trend: compare first-half avg to second-half avg."""
        if len(values) < 2:
            return 'flat', 0.0
        mid = len(values) // 2
        first = statistics.mean(values[:mid]) if mid > 0 else 0
        second = statistics.mean(values[mid:]) if mid > 0 else 0
        delta = second - first
        pct = (delta / first * 100) if first != 0 else 0
        if abs(pct) < 5:
            return 'flat', pct
        return ('up', pct) if pct > 0 else ('down', pct)

    arrows = {
        'up': '\u2191',
        'down': '\u2193',
        'flat': '\u2192',
    }

    for name, values, good_dir in [
        ('Avg boost', boosts, 'down'),
        ('Heater duty', pwms, 'down'),
        ('Banding events', risk_events, 'down'),
    ]:
        direction, pct = _trend(values)
        arrow = arrows[direction]
        verdict = ''
        if direction != 'flat':
            if direction == good_dir:
                verdict = '  (improving)'
            else:
                verdict = '  (worsening)'
        print(f"  {name:<18} {arrow} {abs(pct):>5.1f}% {direction}{verdict}")

    print("\n" + "=" * 70 + "\n")


# =============================================================================
# THERMAL LAG REPORT
# =============================================================================

def analyze_thermal_lag(csv_file, lag_threshold=3.0, rows=None):
    """Identify moments where temp_actual falls behind temp_target.

    Returns a list of lag episodes and overall statistics.
    """
    episodes = []      # periods where lag exceeded threshold
    current_ep = None
    all_lags = []
    flow_at_lag = []

    try:
        _rows = rows if rows is not None else load_csv_rows(csv_file)
        for row in _rows:
                try:
                    elapsed = float(row['elapsed_s'])
                    t_actual = float(row['temp_actual'])
                    t_target = float(row['temp_target'])
                    flow = float(row['flow'])
                    pwm = float(row['pwm'])
                    z = float(row.get('z_height', 0))

                    lag = t_target - t_actual  # positive = behind
                    all_lags.append(lag)

                    if lag >= lag_threshold:
                        flow_at_lag.append(flow)
                        if current_ep is None:
                            current_ep = {
                                'start_s': elapsed, 'z_start': z,
                                'max_lag': lag, 'max_flow': flow,
                                'max_pwm': pwm, 'samples': 0,
                            }
                        current_ep['samples'] += 1
                        if lag > current_ep['max_lag']:
                            current_ep['max_lag'] = lag
                        if flow > current_ep['max_flow']:
                            current_ep['max_flow'] = flow
                        if pwm > current_ep['max_pwm']:
                            current_ep['max_pwm'] = pwm
                        current_ep['end_s'] = elapsed
                        current_ep['z_end'] = z
                    else:
                        if current_ep is not None:
                            episodes.append(current_ep)
                            current_ep = None
                except (KeyError, ValueError):
                    continue

        # close any open episode
        if current_ep is not None:
            episodes.append(current_ep)

    except Exception as exc:
        print(f"Warning: Could not read {csv_file}: {exc}")
        return None

    avg_lag = statistics.mean(all_lags) if all_lags else 0
    max_lag = max(all_lags) if all_lags else 0
    lag_pct = (sum(1 for l in all_lags if l >= lag_threshold) / len(all_lags) * 100) if all_lags else 0
    avg_flow_at_lag = statistics.mean(flow_at_lag) if flow_at_lag else 0

    return {
        'episodes': episodes,
        'avg_lag': avg_lag,
        'max_lag': max_lag,
        'lag_pct': lag_pct,
        'avg_flow_at_lag': avg_flow_at_lag,
        'total_samples': len(all_lags),
    }


def print_thermal_lag_report(lag_data, threshold=3.0):
    """Display the thermal lag report."""
    if lag_data is None:
        print("No thermal lag data available.")
        return

    print("\n" + "=" * 70)
    print("  THERMAL LAG REPORT")
    print("=" * 70)

    print(f"\nLag threshold: {threshold:.1f}\u00b0C")
    print(f"Avg lag      : {lag_data['avg_lag']:.1f}\u00b0C")
    print(f"Max lag      : {lag_data['max_lag']:.1f}\u00b0C")
    print(f"Time in lag  : {lag_data['lag_pct']:.1f}% of print")
    if lag_data['avg_flow_at_lag'] > 0:
        print(f"Avg flow when lagging: {lag_data['avg_flow_at_lag']:.1f} mm\u00b3/s")

    episodes = lag_data['episodes']
    if not episodes:
        print(f"\n\u2713 Heater kept up throughout \u2014 never fell >{threshold:.0f}\u00b0C behind target.")
        print("=" * 70 + "\n")
        return

    # Sort by severity
    episodes.sort(key=lambda e: e['max_lag'], reverse=True)

    print(f"\n\u2500" * 70)
    print(f"  LAG EPISODES ({len(episodes)} detected)")
    print("\u2500" * 70)
    print(f"\n{'#':>3}  {'Time':>10}  {'Duration':>8}  {'Max lag':>8}  {'Flow':>8}  {'PWM':>6}  Z range")
    print("\u2500" * 70)

    for i, ep in enumerate(episodes[:10], 1):
        dur = ep.get('end_s', ep['start_s']) - ep['start_s']
        z_range = f"{ep['z_start']:.1f}-{ep.get('z_end', ep['z_start']):.1f}mm"
        print(f"{i:>3}  {ep['start_s']:>8.0f}s  {dur:>6.0f}s  "
              f"{ep['max_lag']:>6.1f}\u00b0C  {ep['max_flow']:>6.1f}  "
              f"{ep['max_pwm']:>5.0%}  {z_range}")

    if len(episodes) > 10:
        print(f"\n   ... and {len(episodes) - 10} more episodes")

    # Actionable advice
    print(f"\n\u2500" * 70)
    print("  RECOMMENDATIONS")
    print("\u2500" * 70)

    worst = episodes[0]
    if worst['max_pwm'] >= 0.95:
        print("  \u26a0 Heater saturated during worst lag \u2014 it physically can't heat faster.")
        print("    \u2192 Lower flow_k or max_boost_limit to reduce temperature demand")
        print("    \u2192 Or reduce print speed for high-flow sections")
    else:
        print("  \u26a0 Heater has headroom but ramp rate is too slow.")
        print("    \u2192 Increase ramp_rate_rise (try +0.5\u00b0C/s increments)")
        print("    \u2192 Or increase flow_k slightly for earlier pre-heating")

    if lag_data['lag_pct'] > 20:
        print("  \u26a0 Heater behind >20% of the print \u2014 significant under-temperature risk")
    elif lag_data['lag_pct'] > 5:
        print("  \u26a0 Occasional lag \u2014 mostly during flow spikes")

    print("\n" + "=" * 70 + "\n")


# =============================================================================
# HEATER HEADROOM ANALYSIS
# =============================================================================

def analyze_heater_headroom(csv_file, flow_bins=None, rows=None):
    """Analyze flow rate vs PWM to determine heater capacity.

    Groups samples by flow-rate brackets and computes avg/max PWM for each.
    """
    if flow_bins is None:
        flow_bins = [0, 2, 5, 8, 10, 12, 15, 20, 25, 30, 40]

    brackets = defaultdict(lambda: {'pwm_values': [], 'count': 0})

    try:
        _rows = rows if rows is not None else load_csv_rows(csv_file)
        for row in _rows:
                try:
                    flow = float(row['flow'])
                    pwm = float(row['pwm'])
                    # Find the bracket
                    for j in range(len(flow_bins) - 1):
                        if flow_bins[j] <= flow < flow_bins[j + 1]:
                            key = (flow_bins[j], flow_bins[j + 1])
                            brackets[key]['pwm_values'].append(pwm)
                            brackets[key]['count'] += 1
                            break
                    else:
                        if flow >= flow_bins[-1]:
                            key = (flow_bins[-1], float('inf'))
                            brackets[key]['pwm_values'].append(pwm)
                            brackets[key]['count'] += 1
                except (KeyError, ValueError):
                    continue
    except Exception as exc:
        print(f"Warning: Could not read {csv_file}: {exc}")
        return None

    result = {}
    for key in sorted(brackets.keys()):
        vals = brackets[key]['pwm_values']
        result[key] = {
            'count': brackets[key]['count'],
            'avg_pwm': statistics.mean(vals) if vals else 0,
            'max_pwm': max(vals) if vals else 0,
            'p95_pwm': sorted(vals)[int(len(vals) * 0.95)] if len(vals) >= 20 else max(vals) if vals else 0,
        }
    return result


def print_headroom_report(headroom):
    """Display heater headroom analysis."""
    if not headroom:
        print("No heater headroom data available.")
        return

    print("\n" + "=" * 70)
    print("  HEATER HEADROOM ANALYSIS")
    print("=" * 70)
    print("\nFlow rate vs heater duty — shows how much capacity remains.\n")

    _hdr_flow = 'Flow (mm\u00b3/s)'
    print(f"{_hdr_flow:>16}  {'Samples':>7}  {'Avg PWM':>8}  {'P95 PWM':>8}  {'Max PWM':>8}  Headroom")
    print("\u2500" * 70)

    # Only flag saturation at meaningful flow rates (>=8 mm³/s).
    # Low-flow brackets show high PWM during ramp-up/retractions — not real limits.
    MIN_FLOW_SAT = 8.0

    saturation_flow = None
    for key in sorted(headroom.keys()):
        d = headroom[key]
        if d['count'] < 3:  # skip very sparse brackets
            continue
        lo, hi = key
        if hi == float('inf'):
            label = f"  {lo:.0f}+"
        else:
            label = f"  {lo:.0f}-{hi:.0f}"

        headroom_pct = max(0, (1.0 - d['p95_pwm']) * 100)
        bar_len = int(headroom_pct / 100 * 20)
        bar = '\u2588' * bar_len + '\u2591' * (20 - bar_len)

        warning = ''
        if d['p95_pwm'] >= 0.95:
            if lo >= MIN_FLOW_SAT:
                warning = ' SATURATED'
                if saturation_flow is None:
                    saturation_flow = lo
            else:
                warning = ' (normal at low flow)'
        elif d['p95_pwm'] >= 0.85 and lo >= MIN_FLOW_SAT:
            warning = ' !'

        print(f"{label:>16}  {d['count']:>7}  {d['avg_pwm']:>7.0%}  "
              f"{d['p95_pwm']:>7.0%}  {d['max_pwm']:>7.0%}  {bar} {headroom_pct:.0f}%{warning}")

    print(f"\n\u2500" * 70)
    print("  VERDICT")
    print("\u2500" * 70)

    if saturation_flow is not None:
        print(f"\n  \u26a0 Heater saturates at ~{saturation_flow:.0f} mm\u00b3/s flow rate.")
        print(f"    Above this, the heater cannot keep up with temperature demand.")
        print(f"    \u2192 Reduce flow_k by 0.1\u20130.3 to lower temperature demand")
        print(f"    \u2192 Or cap speeds in slicer to stay under {saturation_flow:.0f} mm\u00b3/s")
    else:
        print(f"\n  \u2713 Heater has headroom across all flow rates \u2014 no saturation detected.")
        # Find highest-flow bracket with data
        max_bracket = max((k for k in headroom if headroom[k]['count'] >= 3),
                          key=lambda k: k[0], default=None)
        if max_bracket:
            d = headroom[max_bracket]
            remaining = max(0, (1.0 - d['p95_pwm']) * 100)
            print(f"    At peak flow ({max_bracket[0]:.0f}+ mm\u00b3/s), "
                  f"{remaining:.0f}% heater capacity remains.")

    print("\n" + "=" * 70 + "\n")


# =============================================================================
# PA STABILITY ANALYSIS
# =============================================================================

def analyze_pa_stability(csv_file, window_s=10.0, rows=None):
    """Analyze PA value stability over time.

    Detects oscillation zones where PA changes frequently within a time window.
    """
    samples = []
    try:
        _rows = rows if rows is not None else load_csv_rows(csv_file)
        for row in _rows:
                try:
                    elapsed = float(row['elapsed_s'])
                    pa = float(row.get('pa', 0))
                    pa_delta = float(row.get('pa_delta', 0))
                    z = float(row.get('z_height', 0))
                    if pa > 0:
                        samples.append({
                            'time': elapsed, 'pa': pa,
                            'delta': pa_delta, 'z': z,
                        })
                except (KeyError, ValueError):
                    continue
    except Exception as exc:
        print(f"Warning: Could not read {csv_file}: {exc}")
        return None

    if len(samples) < 10:
        return {'samples': len(samples), 'oscillation_zones': [],
                'pa_range': 0, 'pa_stdev': 0, 'change_count': 0}

    pa_values = [s['pa'] for s in samples]
    pa_min = min(pa_values)
    pa_max = max(pa_values)
    pa_stdev = statistics.stdev(pa_values) if len(pa_values) > 1 else 0

    # Count significant changes
    change_count = sum(1 for s in samples if abs(s['delta']) > 0.003)

    # Detect oscillation zones: sliding window, count changes per window
    # Pre-compute which samples are "significant changes"
    sig_changes = [abs(s['delta']) > 0.003 for s in samples]
    n_samples = len(samples)
    oscillation_zones = []
    current_zone = None

    # Two-pointer window for O(n) instead of O(n^2)
    win_lo = 0
    changes_in_window = 0
    for i, s in enumerate(samples):
        t_center = s['time']
        t_start = t_center - window_s / 2
        t_end = t_center + window_s / 2

        # Shrink window from left
        while win_lo < n_samples and samples[win_lo]['time'] < t_start:
            if sig_changes[win_lo]:
                changes_in_window -= 1
            win_lo += 1

        # Expand window to the right to include new samples entering
        # We need to track an explicit right pointer too
        if i == 0:
            # Initialize: count all samples in the first window
            changes_in_window = 0
            win_lo = 0
            win_hi = 0
            while win_hi < n_samples and samples[win_hi]['time'] <= t_end:
                if sig_changes[win_hi]:
                    changes_in_window += 1
                win_hi += 1
            # Shrink from left
            while win_lo < n_samples and samples[win_lo]['time'] < t_start:
                if sig_changes[win_lo]:
                    changes_in_window -= 1
                win_lo += 1
        else:
            # Add new samples entering from the right
            while win_hi < n_samples and samples[win_hi]['time'] <= t_end:
                if sig_changes[win_hi]:
                    changes_in_window += 1
                win_hi += 1
            # Remove samples leaving from the left
            while win_lo < n_samples and samples[win_lo]['time'] < t_start:
                if sig_changes[win_lo]:
                    changes_in_window -= 1
                win_lo += 1

        # An oscillation zone has >=4 changes in the window
        if changes_in_window >= 4:
            if current_zone is None:
                current_zone = {
                    'start_s': s['time'], 'z_start': s['z'],
                    'changes': changes_in_window,
                    'pa_min': s['pa'], 'pa_max': s['pa'],
                }
            current_zone['end_s'] = s['time']
            current_zone['z_end'] = s['z']
            current_zone['changes'] = max(current_zone['changes'], changes_in_window)
            current_zone['pa_min'] = min(current_zone['pa_min'], s['pa'])
            current_zone['pa_max'] = max(current_zone['pa_max'], s['pa'])
        else:
            if current_zone is not None:
                oscillation_zones.append(current_zone)
                current_zone = None

    if current_zone is not None:
        oscillation_zones.append(current_zone)

    return {
        'samples': len(samples),
        'pa_min': pa_min,
        'pa_max': pa_max,
        'pa_range': pa_max - pa_min,
        'pa_stdev': pa_stdev,
        'change_count': change_count,
        'oscillation_zones': oscillation_zones,
    }


def print_pa_stability_report(pa_data):
    """Display PA stability analysis."""
    if pa_data is None:
        print("No PA data available.")
        return

    print("\n" + "=" * 70)
    print("  PA STABILITY ANALYSIS")
    print("=" * 70)

    if pa_data['samples'] < 10:
        print("\nInsufficient PA data (need at least 10 samples with PA > 0).")
        print("=" * 70 + "\n")
        return

    print(f"\nPA range  : {pa_data['pa_min']:.4f} \u2014 {pa_data['pa_max']:.4f} "
          f"(span {pa_data['pa_range']:.4f})")
    print(f"PA stdev  : {pa_data['pa_stdev']:.5f}")
    print(f"Changes   : {pa_data['change_count']} significant (>\u00b10.003)")

    zones = pa_data['oscillation_zones']

    if not zones:
        print(f"\n\u2713 PA is stable throughout \u2014 no oscillation zones detected.")
        if pa_data['pa_range'] < 0.005:
            print(f"  PA barely moved ({pa_data['pa_range']:.4f} range) \u2014 "
                  f"effectively constant.")
        print("=" * 70 + "\n")
        return

    print(f"\n\u2500" * 70)
    print(f"  OSCILLATION ZONES ({len(zones)} detected)")
    print("\u2500" * 70)
    print(f"\n{'#':>3}  {'Time':>10}  {'Duration':>8}  {'PA range':>10}  "
          f"{'Changes':>8}  Z range")
    print("\u2500" * 70)

    for i, z in enumerate(zones[:10], 1):
        dur = z.get('end_s', z['start_s']) - z['start_s']
        pa_span = z['pa_max'] - z['pa_min']
        z_range = f"{z['z_start']:.1f}-{z.get('z_end', z['z_start']):.1f}mm"
        print(f"{i:>3}  {z['start_s']:>8.0f}s  {dur:>6.0f}s  "
              f"{pa_span:>9.4f}  {z['changes']:>8}  {z_range}")

    if len(zones) > 10:
        print(f"\n   ... and {len(zones) - 10} more zones")

    print(f"\n\u2500" * 70)
    print("  RECOMMENDATIONS")
    print("\u2500" * 70)

    if len(zones) > 5:
        print("  \u26a0 Frequent PA oscillation \u2014 likely causing visible ribbing.")
        print("    \u2192 Increase pa_deadband (try 0.005 or higher)")
        print("    \u2192 Or lower pa_boost_k to reduce PA sensitivity to temp changes")
    elif len(zones) > 0:
        print("  \u26a0 Some PA oscillation zones detected.")
        print("    \u2192 If you see ribbing at those Z heights, increase pa_deadband")
    if pa_data['pa_range'] > 0.02:
        print(f"  \u26a0 Wide PA range ({pa_data['pa_range']:.4f}) \u2014 "
              f"pa_boost_k may be too aggressive")

    print("\n" + "=" * 70 + "\n")


# =============================================================================
# DYNZ ZONE MAP
# =============================================================================

def analyze_dynz_zones(csv_file, bin_size=0.5, rows=None):
    """Analyze DynZ activation patterns by Z-height."""
    bins = defaultdict(lambda: {
        'samples': 0, 'dynz_active': 0, 'transitions': 0,
        'accel_sum': 0, 'stress_sum': 0,
    })
    try:
        _rows = rows if rows is not None else load_csv_rows(csv_file)
        for row in _rows:
                try:
                    z = float(row.get('z_height', 0))
                    dynz = int(row.get('dynz_active', 0))
                    accel = float(row.get('accel', 0))
                    speed = float(row.get('speed', 0))
                    flow = float(row.get('flow', 0))
                    pwm = float(row.get('pwm', 0))
                    trans = int(row.get('dynz_transition', 0))
                    bin_key = math.floor(z / bin_size) * bin_size
                    b = bins[bin_key]
                    b['samples'] += 1
                    if dynz:
                        b['dynz_active'] += 1
                    if trans != 0:
                        b['transitions'] += 1
                    b['accel_sum'] += accel
                    stress = (speed / max(flow, 0.1)) * pwm
                    b['stress_sum'] += stress
                except (KeyError, ValueError):
                    continue
    except Exception as exc:
        print(f"Warning: Could not read {csv_file}: {exc}")
        return {}
    result = {}
    for key in sorted(bins.keys()):
        b = bins[key]
        if b['samples'] == 0:
            continue
        result[key] = {
            'samples': b['samples'],
            'active_pct': round(b['dynz_active'] / b['samples'] * 100, 1),
            'transitions': b['transitions'],
            'avg_accel': round(b['accel_sum'] / b['samples']),
            'avg_stress': round(b['stress_sum'] / b['samples'], 2),
        }
    return result


def print_dynz_map(zones, bin_size=0.5):
    """Print the DynZ zone map to terminal."""
    if not zones:
        print("No DynZ data (DynZ may be inactive this print).")
        return
    print("\n" + "=" * 70)
    print("  DYNZ ZONE MAP")
    print("=" * 70)

    any_active = any(v['active_pct'] > 0 for v in zones.values())
    if not any_active:
        print("\n  \u2713 DynZ was not active at any Z-height.")
        print("=" * 70 + "\n")
        return

    max_active = max(v['active_pct'] for v in zones.values())

    print(f"\n{'Z range':>14}  {'Active':>7}  {'Trans':>5}  "
          f"{'Avg Accel':>10}  {'Stress':>7}  Bar")
    print("\u2500" * 70)

    for z in sorted(zones.keys()):
        v = zones[z]
        z_end = z + bin_size
        label = f"{z:6.1f}-{z_end:.1f}mm"
        bar = _bar(v['active_pct'], max(max_active, 0.1))
        marker = '  <-- HIGH' if v['active_pct'] >= 50 else ''
        print(f"{label:>14}  {v['active_pct']:>6.1f}%  {v['transitions']:>5}  "
              f"{v['avg_accel']:>10}  {v['avg_stress']:>6.1f}  {bar}{marker}")

    total_transitions = sum(v['transitions'] for v in zones.values())
    avg_active = statistics.mean(v['active_pct'] for v in zones.values())

    print(f"\n\u2500" * 70)
    print("  SUMMARY")
    print("\u2500" * 70)
    print(f"  Avg activation: {avg_active:.1f}%")
    print(f"  Total transitions: {total_transitions}")

    high_zones = [z for z in zones if zones[z]['active_pct'] >= 50]
    if high_zones:
        print(f"  High-activity zones: "
              f"{', '.join(f'{z:.1f}mm' for z in sorted(high_zones))}")
        print(f"\n  \u26a0 DynZ heavily active at those heights.")
        print("    \u2192 Check for thin walls, overhangs, or rapid geometry changes")
        print("    \u2192 If banding appears there, try "
              "dynz_relief_method: 'temp_reduction'")
    else:
        print(f"\n  \u2713 DynZ activation is mild throughout.")

    print("\n" + "=" * 70 + "\n")


# =============================================================================
# SPEED / FLOW DISTRIBUTION
# =============================================================================

def analyze_speed_flow_distribution(csv_file, rows=None):
    """Analyze time spent in speed and flow rate brackets."""
    speed_edges = [0, 25, 50, 75, 100, 125, 150, 200, 250, 300, 400]
    flow_edges = [0, 2, 4, 6, 8, 10, 12, 15, 20, 25, 30, 40]
    speed_brackets = defaultdict(lambda: {
        'count': 0, 'boost_sum': 0, 'pa_sum': 0, 'pwm_sum': 0,
    })
    flow_brackets = defaultdict(lambda: {
        'count': 0, 'boost_sum': 0, 'pa_sum': 0, 'pwm_sum': 0,
    })

    try:
        _rows = rows if rows is not None else load_csv_rows(csv_file)
        for row in _rows:
                try:
                    speed = float(row['speed'])
                    flow = float(row['flow'])
                    boost = float(row.get('boost', 0))
                    pa = float(row.get('pa', 0))
                    pwm = float(row.get('pwm', 0))

                    placed = False
                    for j in range(len(speed_edges) - 1):
                        if speed_edges[j] <= speed < speed_edges[j + 1]:
                            key = (speed_edges[j], speed_edges[j + 1])
                            b = speed_brackets[key]
                            b['count'] += 1
                            b['boost_sum'] += boost
                            b['pa_sum'] += pa
                            b['pwm_sum'] += pwm
                            placed = True
                            break
                    if not placed and speed >= speed_edges[-1]:
                        key = (speed_edges[-1], 999)
                        b = speed_brackets[key]
                        b['count'] += 1
                        b['boost_sum'] += boost
                        b['pa_sum'] += pa
                        b['pwm_sum'] += pwm

                    placed = False
                    for j in range(len(flow_edges) - 1):
                        if flow_edges[j] <= flow < flow_edges[j + 1]:
                            key = (flow_edges[j], flow_edges[j + 1])
                            b = flow_brackets[key]
                            b['count'] += 1
                            b['boost_sum'] += boost
                            b['pa_sum'] += pa
                            b['pwm_sum'] += pwm
                            placed = True
                            break
                    if not placed and flow >= flow_edges[-1]:
                        key = (flow_edges[-1], 999)
                        b = flow_brackets[key]
                        b['count'] += 1
                        b['boost_sum'] += boost
                        b['pa_sum'] += pa
                        b['pwm_sum'] += pwm
                except (KeyError, ValueError):
                    continue
    except Exception as exc:
        print(f"Warning: Could not read {csv_file}: {exc}")
        return None

    def _fmt(brackets):
        result = {}
        total = sum(b['count'] for b in brackets.values())
        for key in sorted(brackets.keys()):
            b = brackets[key]
            if b['count'] == 0:
                continue
            n = b['count']
            result[key] = {
                'count': n,
                'pct': round(n / total * 100, 1) if total else 0,
                'avg_boost': round(b['boost_sum'] / n, 1),
                'avg_pa': round(b['pa_sum'] / n, 4),
                'avg_pwm': round(b['pwm_sum'] / n, 3),
            }
        return result

    return {'speed': _fmt(speed_brackets), 'flow': _fmt(flow_brackets)}


def print_distribution(dist):
    """Print speed/flow distribution analysis to terminal."""
    if not dist:
        print("No distribution data.")
        return

    print("\n" + "=" * 70)
    print("  SPEED / FLOW DISTRIBUTION")
    print("=" * 70)

    print(f"\n\u2500" * 70)
    print("  SPEED DISTRIBUTION")
    print("\u2500" * 70)
    print(f"\n{'Speed (mm/s)':>16}  {'% Time':>7}  {'Samples':>8}  "
          f"{'Avg Boost':>10}  {'Avg PWM':>8}  Bar")
    print("\u2500" * 70)

    max_pct = max((v['pct'] for v in dist['speed'].values()), default=1)
    for key in sorted(dist['speed'].keys()):
        v = dist['speed'][key]
        lo, hi = key
        label = f"{lo}-{hi}" if hi < 999 else f"{lo}+"
        bar = _bar(v['pct'], max(max_pct, 0.1), width=20)
        print(f"{label:>16}  {v['pct']:>6.1f}%  {v['count']:>8}  "
              f"{v['avg_boost']:>9.1f}\u00b0C  {v['avg_pwm']:>7.0%}  {bar}")

    print(f"\n\u2500" * 70)
    print("  FLOW DISTRIBUTION")
    print("\u2500" * 70)
    hdr_flow = 'Flow (mm\u00b3/s)'
    print(f"\n{hdr_flow:>16}  {'% Time':>7}  {'Samples':>8}  "
          f"{'Avg Boost':>10}  {'Avg PWM':>8}  Bar")
    print("\u2500" * 70)

    max_pct = max((v['pct'] for v in dist['flow'].values()), default=1)
    for key in sorted(dist['flow'].keys()):
        v = dist['flow'][key]
        lo, hi = key
        label = f"{lo}-{hi}" if hi < 999 else f"{lo}+"
        bar = _bar(v['pct'], max(max_pct, 0.1), width=20)
        print(f"{label:>16}  {v['pct']:>6.1f}%  {v['count']:>8}  "
              f"{v['avg_boost']:>9.1f}\u00b0C  {v['avg_pwm']:>7.0%}  {bar}")

    peak_speed = max(dist['speed'].items(), key=lambda x: x[1]['pct'])
    peak_flow = max(dist['flow'].items(), key=lambda x: x[1]['pct'])
    print(f"\n\u2500" * 70)
    print("  INSIGHT")
    print("\u2500" * 70)
    lo_s, hi_s = peak_speed[0]
    lo_f, hi_f = peak_flow[0]
    s_lbl = f"{lo_s}-{hi_s}" if hi_s < 999 else f"{lo_s}+"
    f_lbl = f"{lo_f}-{hi_f}" if hi_f < 999 else f"{lo_f}+"
    print(f"  Most time at {s_lbl} mm/s speed ({peak_speed[1]['pct']:.1f}%)")
    print(f"  Most time at {f_lbl} mm\u00b3/s flow ({peak_flow[1]['pct']:.1f}%)")

    high_speed = {k: v for k, v in dist['speed'].items() if k[0] >= 150}
    if high_speed:
        high_pct = sum(v['pct'] for v in high_speed.values())
        if high_pct < 5:
            print(f"  \u26a0 Only {high_pct:.1f}% of time above 150mm/s "
                  "\u2014 speeds may be throttled by acceleration limits")

    print("\n" + "=" * 70 + "\n")


# =============================================================================
# BOOST OPTIMIZATION ANALYSIS — can the printer go faster?
# =============================================================================

def analyze_boost_optimization(csv_file, summary=None, hotend_info=None,
                                printer_hw=None, slicer_settings=None, rows=None):
    """Analyze actual print data to determine if the printer could be pushed faster.

    Examines per-sample boost, PWM, flow, and speed data to find where the
    printer had thermal/flow headroom.  Returns a dict with per-aspect
    headroom analysis and concrete speed suggestions.

    This is the "could I go faster?" analysis — it uses *actual print data*
    rather than just slicer settings, so it reflects real-world performance.
    """
    all_rows = rows if rows is not None else load_csv_rows(csv_file)
    if not all_rows or len(all_rows) < 50:
        return None

    s = summary or {}
    hw = printer_hw or {}
    hi = hotend_info or {}
    ss = slicer_settings or {}

    safe_flow = hi.get('safe_flow', 11)
    peak_flow = hi.get('peak_flow', 14)
    nozzle_type = hi.get('nozzle_type', 'SF')
    material = hi.get('material', 'PLA')
    kinematics = hw.get('kinematics', 'unknown')

    # Input shaper data
    is_data = hw.get('input_shaper', {})
    shaper_limits = {}
    for ax in ('x', 'y'):
        ax_data = is_data.get(ax, {})
        rec = ax_data.get('recommended_max_accel')
        if rec:
            shaper_limits[ax] = rec
    shaper_quality_max = min(shaper_limits.values()) if shaper_limits else None

    # ── Collect per-sample stats ──
    boost_vals = []
    pwm_vals = []
    flow_vals = []
    speed_vals = []
    accel_vals_raw = []
    fan_vals = []

    # Per-flow-bracket analysis (how much headroom at each flow level)
    flow_brackets = {}  # {bracket_label: {samples, boost_sum, pwm_sum, ...}}
    bracket_edges = [0, 2, 4, 6, 8, 10, 12, 15, 20, 25]

    for row in all_rows:
        try:
            flow = float(row.get('flow', 0))
            speed = float(row.get('speed', 0))
            boost = float(row.get('boost', 0))
            pwm = float(row.get('pwm', 0))
            accel = float(row.get('accel', 0))
            fan = float(row.get('fan_pct', 0))

            if flow <= 0 and speed <= 0:
                continue  # skip travel/idle samples

            boost_vals.append(boost)
            pwm_vals.append(pwm)
            flow_vals.append(flow)
            speed_vals.append(speed)
            if accel > 0:
                accel_vals_raw.append(accel)
            fan_vals.append(fan)

            # Bin into flow bracket
            placed = False
            for j in range(len(bracket_edges) - 1):
                if bracket_edges[j] <= flow < bracket_edges[j + 1]:
                    key = f"{bracket_edges[j]}-{bracket_edges[j+1]}"
                    placed = True
                    break
            if not placed:
                key = f"{bracket_edges[-1]}+"

            if key not in flow_brackets:
                flow_brackets[key] = {
                    'samples': 0, 'boost_sum': 0, 'boost_max': 0,
                    'pwm_sum': 0, 'pwm_max': 0, 'speed_sum': 0,
                    'speed_max': 0, 'flow_sum': 0, 'flow_max': 0,
                    'fan_sum': 0, 'accel_sum': 0, 'accel_count': 0,
                }
            b = flow_brackets[key]
            b['samples'] += 1
            b['boost_sum'] += boost
            b['boost_max'] = max(b['boost_max'], boost)
            b['pwm_sum'] += pwm
            b['pwm_max'] = max(b['pwm_max'], pwm)
            b['speed_sum'] += speed
            b['speed_max'] = max(b['speed_max'], speed)
            b['flow_sum'] += flow
            b['flow_max'] = max(b['flow_max'], flow)
            b['fan_sum'] += fan
            if accel > 0:
                b['accel_sum'] += accel
                b['accel_count'] += 1

        except (KeyError, ValueError):
            continue

    if not flow_vals:
        return None

    n = len(flow_vals)
    avg_flow = sum(flow_vals) / n
    max_flow = max(flow_vals)
    avg_speed = sum(speed_vals) / n
    max_speed = max(speed_vals)
    avg_boost = sum(boost_vals) / n
    max_boost = max(boost_vals)
    avg_pwm = sum(pwm_vals) / n
    max_pwm = max(pwm_vals)

    # ── Compute headroom on each dimension ──

    # 1. THERMAL HEADROOM: how much more boost could the heater handle
    #    PWM headroom = how far from saturation
    thermal_headroom_pct = round((1.0 - avg_pwm) * 100, 1)
    thermal_at_limit = avg_pwm >= 0.85

    # 2. FLOW HEADROOM: how far from hotend safe limit
    flow_headroom = round(safe_flow - max_flow, 1)
    flow_headroom_pct = round((safe_flow - avg_flow) / safe_flow * 100, 1) if safe_flow > 0 else 0
    flow_at_limit = max_flow >= safe_flow * 0.90

    # 3. BOOST HEADROOM: how much temp boost was used vs available
    #    Typical max boost for PLA is ~25-30°C, PETG ~20°C
    max_boost_available = {'PLA': 30, 'PETG': 25, 'ABS': 20, 'ASA': 20, 'TPU': 15}.get(material, 25)
    boost_headroom = round(max_boost_available - max_boost, 1)
    boost_headroom_pct = round((max_boost_available - avg_boost) / max_boost_available * 100, 1) if max_boost_available > 0 else 0

    # 4. ACCEL HEADROOM: were actual accels near shaper/firmware limit
    accel_headroom = None
    if accel_vals_raw and shaper_quality_max:
        max_accel_used = max(accel_vals_raw)
        avg_accel_used = sum(accel_vals_raw) / len(accel_vals_raw)
        accel_headroom = {
            'avg_used': int(avg_accel_used),
            'max_used': int(max_accel_used),
            'shaper_limit': int(shaper_quality_max),
            'pct_used': round(max_accel_used / shaper_quality_max * 100, 1),
            'at_limit': max_accel_used >= shaper_quality_max * 0.90,
        }

    # 5. FAN HEADROOM: was part cooling at max
    avg_fan = sum(fan_vals) / n if fan_vals else 0
    max_fan = max(fan_vals) if fan_vals else 0
    fan_at_limit = max_fan >= 98  # effectively at 100%

    # ── Per-bracket summary ──
    bracket_analysis = {}
    for key in sorted(flow_brackets.keys(), key=lambda k: float(k.split('-')[0]) if '-' in k else float(k.replace('+', ''))):
        b = flow_brackets[key]
        if b['samples'] < 5:
            continue
        ns = b['samples']
        avg_b = b['boost_sum'] / ns
        avg_p = b['pwm_sum'] / ns
        avg_s = b['speed_sum'] / ns
        avg_f = b['flow_sum'] / ns
        avg_fn = b['fan_sum'] / ns
        avg_a = b['accel_sum'] / b['accel_count'] if b['accel_count'] > 0 else 0

        bracket_analysis[key] = {
            'samples': ns,
            'pct_time': round(ns / n * 100, 1),
            'avg_boost': round(avg_b, 1),
            'max_boost': round(b['boost_max'], 1),
            'avg_pwm': round(avg_p, 3),
            'max_pwm': round(b['pwm_max'], 3),
            'avg_speed': round(avg_s, 1),
            'max_speed': round(b['speed_max'], 1),
            'avg_flow': round(avg_f, 1),
            'max_flow': round(b['flow_max'], 1),
            'avg_fan': round(avg_fn, 1),
            'avg_accel': int(avg_a),
            # Per-bracket verdicts
            'thermal_ok': avg_p < 0.80,
            'boost_ok': avg_b < max_boost_available * 0.7,
            'flow_ok': b['flow_max'] < safe_flow * 0.90,
        }

    # ── Overall optimization verdict ──
    # Determine what's the limiting factor and what can be increased
    limiting_factors = []
    can_increase = []

    if thermal_at_limit:
        limiting_factors.append('heater')
    elif thermal_headroom_pct > 30:
        can_increase.append({
            'aspect': 'Heater capacity',
            'headroom': f'{thermal_headroom_pct:.0f}% PWM headroom',
            'detail': f'Avg PWM was only {avg_pwm*100:.0f}% — the heater has '
                      f'significant reserve capacity for higher flow demands.',
        })

    if flow_at_limit:
        limiting_factors.append('hotend flow')
    elif flow_headroom_pct > 25:
        can_increase.append({
            'aspect': 'Flow capacity',
            'headroom': f'{flow_headroom:.1f} mm³/s headroom ({flow_headroom_pct:.0f}%)',
            'detail': f'Peak flow was {max_flow:.1f} mm³/s vs safe limit of '
                      f'{safe_flow} mm³/s — room to increase speeds.',
        })

    if boost_headroom > 5:
        can_increase.append({
            'aspect': 'Temperature boost',
            'headroom': f'{boost_headroom:.0f}°C unused boost range',
            'detail': f'Max boost used was {max_boost:.1f}°C out of ~{max_boost_available}°C '
                      f'available — the system can compensate for higher flow.',
        })

    if accel_headroom and not accel_headroom['at_limit']:
        can_increase.append({
            'aspect': 'Acceleration',
            'headroom': f'{accel_headroom["pct_used"]:.0f}% of shaper limit used',
            'detail': f'Max accel used was {accel_headroom["max_used"]} vs shaper '
                      f'limit of {accel_headroom["shaper_limit"]} — room for higher accels.',
        })

    # ── Compute suggested speed increase ──
    # Find the bottleneck and compute how much faster the printer could go
    speed_increase_pct = 0
    bottleneck = None

    if not limiting_factors:
        # Nothing at limit — compute increase based on tightest headroom
        # Flow is usually the binding constraint
        if safe_flow > 0 and avg_flow > 0:
            flow_ratio = (safe_flow * 0.85) / avg_flow  # target 85% of safe
            speed_increase_pct = min(int((flow_ratio - 1) * 100), 100)  # cap at 100%
        if thermal_headroom_pct < flow_headroom_pct:
            # Thermal is tighter — recalculate based on PWM headroom
            # Rough model: each 10% more speed → ~8% more PWM demand
            thermal_increase = int(thermal_headroom_pct / 0.8)
            speed_increase_pct = min(speed_increase_pct, thermal_increase)
        bottleneck = 'none — all systems have headroom'
    else:
        bottleneck = ', '.join(limiting_factors)
        if 'hotend flow' in limiting_factors and 'heater' not in limiting_factors:
            # Flow-limited but heater OK — can push a bit more with boost
            if boost_headroom > 5:
                speed_increase_pct = min(int(boost_headroom / max_boost_available * 30), 15)
        elif 'heater' in limiting_factors and 'hotend flow' not in limiting_factors:
            # Heater-limited — no room to increase
            speed_increase_pct = 0

    # Clamp to reasonable range
    speed_increase_pct = max(0, min(speed_increase_pct, 100))

    # ── Build concrete suggestions with numerical values ──
    sls = slicer_settings or {}
    nozzle_d = hi.get('nozzle_diameter', 0.4)
    layer_h = sls.get('layer_height', 0.2)
    if not isinstance(layer_h, (int, float)):
        try: layer_h = float(layer_h)
        except (ValueError, TypeError): layer_h = 0.2

    # Read current flow_k
    _current_flow_k = _get_config_value('flow_k', material)

    suggestions = []
    if speed_increase_pct >= 10:
        new_avg_flow = round(avg_flow * (1 + speed_increase_pct / 100), 1)
        mult = 1 + speed_increase_pct / 100

        # Per-feature speed suggestions
        _speed_keys = [
            ('outer_wall_speed', 'Outer wall'),
            ('inner_wall_speed', 'Inner wall'),
            ('sparse_infill_speed', 'Infill'),
            ('internal_solid_infill_speed', 'Solid infill'),
            ('top_surface_speed', 'Top surface'),
        ]
        speed_lines = []
        per_setting_changes = []
        for sk, label in _speed_keys:
            cur = sls.get(sk)
            if cur is not None:
                try:
                    cur_v = float(cur)
                except (ValueError, TypeError):
                    continue
                new_v = int(cur_v * mult)
                # Cap at flow limit
                lw = nozzle_d + 0.05
                max_v = int(safe_flow * 0.85 / (lw * layer_h)) if lw * layer_h > 0 else new_v
                new_v = min(new_v, max_v)
                if new_v > cur_v:
                    speed_lines.append(f'{label}: {int(cur_v)} → {new_v} mm/s')
                    per_setting_changes.append({'key': sk, 'current': int(cur_v), 'suggested': new_v})

        detail_text = (f'Increase speeds by ~{speed_increase_pct}% — avg flow rises from '
                       f'{avg_flow:.1f} to ~{new_avg_flow} mm³/s '
                       f'(safe limit: {safe_flow} mm³/s).')
        if speed_lines:
            detail_text += '\n' + '  •  '.join([''] + speed_lines)

        suggestions.append({
            'what': 'Increase print speeds',
            'detail': detail_text,
            'impact': 'faster prints',
            'per_setting': per_setting_changes,
        })

    if accel_headroom and accel_headroom['pct_used'] < 70:
        sug_accel = int(accel_headroom['shaper_limit'] * 0.85)
        cur_wall_accel = sls.get('outer_wall_acceleration')
        accel_detail = (f'Actual accels averaged {accel_headroom["avg_used"]} mm/s² '
                        f'(max {accel_headroom["max_used"]}). '
                        f'Shaper supports {accel_headroom["shaper_limit"]}.')
        if cur_wall_accel is not None:
            try:
                cwa = int(float(cur_wall_accel))
                accel_detail += f'\nWall accel: {cwa} → {sug_accel} mm/s²'
            except (ValueError, TypeError):
                pass
        else:
            accel_detail += f'\nSuggested wall accel: {sug_accel} mm/s²'
        cur_infill_accel = sls.get('sparse_infill_acceleration')
        if cur_infill_accel is not None:
            try:
                cia = int(float(cur_infill_accel))
                sug_infill = int(accel_headroom['shaper_limit'] * 0.95)
                accel_detail += f', Infill accel: {cia} → {sug_infill} mm/s²'
            except (ValueError, TypeError):
                pass

        suggestions.append({
            'what': 'Increase accelerations',
            'detail': accel_detail,
            'impact': 'less time accelerating/decelerating',
        })

    if boost_headroom > 10 and not thermal_at_limit:
        flow_k_detail = (f'Boost used only {max_boost:.1f}°C of ~{max_boost_available}°C range '
                         f'({avg_pwm*100:.0f}% heater duty).')
        if _current_flow_k is not None:
            sug_fk = round(_current_flow_k + 0.15, 2)
            sug_fk = min(sug_fk, 2.5)  # cap
            flow_k_detail += f'\nflow_k: {_current_flow_k} → {sug_fk}'
        else:
            flow_k_detail += '\nIncrease flow_k by 0.1–0.2 in your material profile.'

        suggestions.append({
            'what': 'Increase flow_k for more aggressive boost',
            'detail': flow_k_detail,
            'impact': 'better flow adaptation',
            'config_var': 'flow_k',
            'direction': 'increase',
            'suggested_value': sug_fk if _current_flow_k is not None else None,
            'material': material,
        })

    if fan_at_limit and material in ('PLA', 'PETG'):
        # Estimate max safe speed limited by cooling
        # Rough model: at fan 100%, cooling limit ≈ current max speed
        fan_detail = f'Fan was at {max_fan:.0f}% — already at maximum capacity.'
        if max_speed > 0:
            fan_detail += (f' At current speeds (max {max_speed:.0f} mm/s), '
                          f'cooling is fully utilized. Increasing speeds beyond '
                          f'~{int(max_speed * 1.1)} mm/s may cause cooling issues '
                          f'(stringing, poor overhangs).')
        fan_detail += ' Consider a higher-CFM fan or duct upgrade before increasing speeds.'

        suggestions.append({
            'what': 'Fan at maximum — cooling may limit speed gains',
            'detail': fan_detail,
            'impact': 'cooling constraint',
        })

    # ── Overall verdict ──
    if speed_increase_pct >= 25:
        verdict = 'significant_headroom'
        verdict_text = (f'Your printer has significant room to go faster. '
                        f'All systems had headroom — you could increase speeds by '
                        f'~{speed_increase_pct}% without exceeding thermal or flow limits.')
    elif speed_increase_pct >= 10:
        verdict = 'moderate_headroom'
        verdict_text = (f'There\'s moderate room for improvement (~{speed_increase_pct}% faster). '
                        f'The printer handled this print comfortably.')
    elif limiting_factors:
        verdict = 'at_limit'
        verdict_text = (f'The printer was near its limits on {bottleneck}. '
                        f'Current speeds are well-matched to your hardware.')
    else:
        verdict = 'well_tuned'
        verdict_text = ('Speeds are well-matched to your hardware. '
                        'The printer is close to optimal for this setup.')

    return {
        'verdict': verdict,
        'verdict_text': verdict_text,
        'speed_increase_pct': speed_increase_pct,
        'bottleneck': bottleneck,
        'limiting_factors': limiting_factors,
        'can_increase': can_increase,
        'suggestions': suggestions,
        'thermal': {
            'avg_pwm': round(avg_pwm, 3),
            'max_pwm': round(max_pwm, 3),
            'headroom_pct': thermal_headroom_pct,
            'at_limit': thermal_at_limit,
        },
        'flow': {
            'avg_flow': round(avg_flow, 1),
            'max_flow': round(max_flow, 1),
            'safe_flow': safe_flow,
            'peak_flow': peak_flow,
            'headroom': flow_headroom,
            'headroom_pct': flow_headroom_pct,
            'at_limit': flow_at_limit,
        },
        'boost': {
            'avg_boost': round(avg_boost, 1),
            'max_boost': round(max_boost, 1),
            'max_available': max_boost_available,
            'headroom': boost_headroom,
            'headroom_pct': boost_headroom_pct,
        },
        'accel': accel_headroom,
        'fan': {
            'avg_fan': round(avg_fan, 1),
            'max_fan': round(max_fan, 1),
            'at_limit': fan_at_limit,
        },
        'speed': {
            'avg_speed': round(avg_speed, 1),
            'max_speed': round(max_speed, 1),
        },
        'brackets': bracket_analysis,
        'material': material,
        'kinematics': kinematics,
        'nozzle_type': nozzle_type,
    }


# =============================================================================
