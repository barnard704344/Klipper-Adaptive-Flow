#!/usr/bin/env python3
"""
Adaptive Flow Print Analyzer — Banding Detection & Print Stats

Statistical analysis of print logs to identify banding culprits
and display per-print health summaries. No external APIs required.

Usage:
    python3 analyze_print.py                         # Show latest print stats
    python3 analyze_print.py <summary.json>          # Show specific print stats
    python3 analyze_print.py --count 10              # Banding analysis (last 10 prints)
    python3 analyze_print.py --count 10 --material PLA  # Filter by material
"""

import os
import sys
import json
import csv
import statistics
import argparse
from pathlib import Path
from collections import defaultdict

# =============================================================================
# CONFIGURATION
# =============================================================================
LOG_DIR = os.path.expanduser('~/printer_data/logs/adaptive_flow')


# =============================================================================
# SINGLE-PRINT STATS
# =============================================================================

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
        print(f"Banding  : {hr} high-risk events \u2014 culprit: {culprit}")
    else:
        print("Banding  : no banding data (update extruder_monitor?)")

    # Quick health verdict
    print()
    warnings = []
    if max_pwm > 0.95:
        warnings.append("Heater near saturation (max PWM > 95%)")
    if avg_pwm > 0.85:
        warnings.append("High average heater duty (>85%)")
    if max_boost > 30:
        warnings.append(f"Large temp boost ({max_boost:.0f}\u00b0C) \u2014 check flow_k / max_boost_limit")
    if ba.get('high_risk_events', 0) > 20:
        warnings.append(f"{ba['high_risk_events']} high-risk banding events \u2014 run --count analysis")

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
    print(f"Most common culprit: {agg['most_common_culprit']}")
    print("Breakdown:")
    for culprit, count in sorted(agg['culprits'].items(),
                                  key=lambda x: x[1], reverse=True):
        print(f"  - {culprit}: {count} print{'s' if count != 1 else ''}")

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
        print(f"   Culprit: {ba.get('likely_culprit', 'unknown')}")

    if len(agg['sessions']) > 5:
        print(f"\n   ... and {len(agg['sessions']) - 5} more")

    print("\n" + "=" * 70 + "\n")


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Adaptive Flow \u2014 Banding Detection & Print Stats',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Latest print stats:
    python3 analyze_print.py

  Specific print:
    python3 analyze_print.py /path/to/print_summary.json

  Multi-print banding analysis:
    python3 analyze_print.py --count 10
    python3 analyze_print.py --count 10 --material PLA
        """,
    )
    parser.add_argument(
        'summary_file', nargs='?',
        help='Path to summary JSON (default: most recent)')
    parser.add_argument(
        '--count', '-c', type=int,
        help='Analyze last N prints for banding patterns')
    parser.add_argument(
        '--material', '-m',
        help='Filter by material (PLA, PETG, etc.)')
    parser.add_argument(
        '--log-dir', '-d', default=LOG_DIR,
        help=f'Log directory (default: {LOG_DIR})')
    args = parser.parse_args()

    log_dir = os.path.expanduser(args.log_dir)

    # ------------------------------------------------------------------
    # BANDING ANALYSIS MODE
    # ------------------------------------------------------------------
    if args.count:
        if not os.path.exists(log_dir):
            print(f"Log directory not found: {log_dir}")
            return 1

        print(f"Searching for recent prints in: {log_dir}")
        if args.material:
            print(f"Filtering by material: {args.material}")

        sessions = find_recent_sessions(log_dir, args.count, args.material)

        if not sessions:
            print("No print sessions found")
            if args.material:
                print(f"  (tried material filter: {args.material})")
            return 1

        if len(sessions) < args.count:
            print(f"Warning: Only found {len(sessions)} prints "
                  f"(requested {args.count})")

        print(f"Analyzing {len(sessions)} prints...")
        agg = aggregate_banding_analysis(sessions)
        print_banding_report(agg)
        return 0

    # ------------------------------------------------------------------
    # SINGLE-PRINT STATS MODE
    # ------------------------------------------------------------------
    if args.summary_file:
        summary_path = args.summary_file
    else:
        if not os.path.exists(log_dir):
            print(f"Log directory not found: {log_dir}")
            print("Run a print first to generate logs.")
            return 1

        summary_path = find_latest_summary(log_dir)
        if not summary_path:
            print(f"No print logs found in {log_dir}")
            print("Run a print first to generate logs.")
            return 1

    summary = load_summary(summary_path)

    if summary.get('_error'):
        print(f"ERROR: {summary['_error']}")
        return 1

    if summary.get('samples', 0) == 0:
        print("WARNING: Summary contains 0 samples \u2014 print may have been too "
              "short, or logging wasn't active.")

    print_single_summary(summary, summary_path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
