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
    python3 analyze_print.py --z-map                 # Z-height banding heatmap
    python3 analyze_print.py --trend 10              # Print-over-print trends
    python3 analyze_print.py --lag                   # Thermal lag report
    python3 analyze_print.py --headroom              # Heater headroom analysis
    python3 analyze_print.py --pa-stability          # PA stability analysis
    python3 analyze_print.py --dynz-map              # DynZ zone map
    python3 analyze_print.py --distribution          # Speed/flow distribution
    python3 analyze_print.py --serve                 # Web dashboard on port 7127
"""

import os
import sys
import json
import csv
import math
import time
import statistics
import argparse
import http.server
import urllib.parse
import socket
from pathlib import Path
from collections import defaultdict

# =============================================================================
# CONFIGURATION
# =============================================================================
LOG_DIR = os.path.expanduser('~/printer_data/logs/adaptive_flow')
CONFIG_DIR = os.path.expanduser('~/printer_data/config')


# =============================================================================
# CONFIG FILE HELPERS  (read / write user config for Apply button)
# =============================================================================

# Material-specific variables live in material_profiles_user.cfg;
# everything else lives in auto_flow_user.cfg.
_MATERIAL_VARS = frozenset(['flow_k', 'pa_boost_k', 'sc_flow_k'])


def _parse_config_variables(filepath):
    """Parse a Klipper config file into {section: {variable: value_str}}.

    Only reads ``variable_*`` lines (not commented-out ones).
    """
    result = {}
    current_section = None
    if not os.path.exists(filepath):
        return result
    try:
        with open(filepath) as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith('[') and ']' in stripped:
                    current_section = stripped[1:stripped.index(']')]
                    result.setdefault(current_section, {})
                elif current_section and stripped.startswith('variable_'):
                    parts = stripped.split(':', 1)
                    if len(parts) == 2:
                        result[current_section][parts[0].strip()] = parts[1].strip()
    except (IOError, OSError):
        pass
    return result


def _config_paths_for(variable, material=None):
    """Return (user_file, defaults_file, section) for a variable."""
    if variable in _MATERIAL_VARS:
        mat = (material or '').strip().upper()
        section = f'gcode_macro _AF_PROFILE_{mat}' if mat else None
        return (
            os.path.join(CONFIG_DIR, 'material_profiles_user.cfg'),
            os.path.join(CONFIG_DIR, 'material_profiles_defaults.cfg'),
            section,
        )
    return (
        os.path.join(CONFIG_DIR, 'auto_flow_user.cfg'),
        os.path.join(CONFIG_DIR, 'auto_flow_defaults.cfg'),
        'gcode_macro _AUTO_TEMP_CORE',
    )


def _get_config_value(variable, material=None):
    """Get the current value of a config variable (user override → default).

    Returns a float, or *None* if the variable was not found.
    """
    user_file, defaults_file, section = _config_paths_for(variable, material)
    if section is None:
        return None
    var_key = f'variable_{variable}'
    for filepath in (user_file, defaults_file):
        cfg = _parse_config_variables(filepath)
        val_str = cfg.get(section, {}).get(var_key)
        if val_str is not None:
            try:
                return float(val_str)
            except ValueError:
                return None
    return None


def _format_value(value):
    """Format a numeric value for config file output."""
    if isinstance(value, float):
        if value == int(value) and abs(value) >= 10:
            return str(int(value))
        if abs(value) < 0.01:
            return f'{value:.4f}'
        if abs(value) < 1:
            return f'{value:.3f}'
        return f'{value:.2f}'
    return str(value)


# =============================================================================
# CONFIG CHANGE LOG — track when Apply button is used
# =============================================================================
_CONFIG_CHANGE_LOG = os.path.join(LOG_DIR, 'config_changes_log.json')


def _log_config_change(variable, old_value, new_value, material=None):
    """Append a record to the config change log."""
    entry = {
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'variable': variable,
        'old_value': old_value,
        'new_value': new_value,
        'material': (material or '').strip().upper() or None,
    }
    entries = _load_config_change_log()
    entries.append(entry)
    try:
        os.makedirs(os.path.dirname(_CONFIG_CHANGE_LOG), exist_ok=True)
        with open(_CONFIG_CHANGE_LOG, 'w') as f:
            json.dump(entries, f, indent=2, default=str)
    except (IOError, OSError) as exc:
        print(f'Warning: could not write config change log: {exc}')


def _load_config_change_log():
    """Load the config change log. Returns list of dicts."""
    if not os.path.exists(_CONFIG_CHANGE_LOG):
        return []
    try:
        with open(_CONFIG_CHANGE_LOG) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError, OSError):
        return []


def _last_change_for(variable, material=None):
    """Find the most recent config change entry for a variable+material.

    Returns ``(timestamp_str, old_value, new_value)`` or ``(None, None, None)``.
    """
    mat_upper = (material or '').strip().upper() or None
    entries = _load_config_change_log()
    for entry in reversed(entries):
        if entry.get('variable') == variable:
            entry_mat = (entry.get('material') or '').strip().upper() or None
            if mat_upper == entry_mat:
                return (
                    entry.get('timestamp'),
                    entry.get('old_value'),
                    entry.get('new_value'),
                )
    return None, None, None


def _count_prints_since(log_dir, material, since_timestamp):
    """Count prints of *material* started after *since_timestamp*."""
    if not since_timestamp:
        return 0
    count = 0
    for f in Path(log_dir).glob('*_summary.json'):
        try:
            with open(f) as fh:
                s = json.load(fh)
            if material and (s.get('material', '').upper() != material.upper()):
                continue
            if (s.get('start_time', '') or '') > since_timestamp:
                count += 1
        except Exception:
            continue
    return count


def _apply_config_change(variable, new_value, material=None):
    """Write a single variable change to the appropriate user config file.

    Creates the file / section if they don't exist.
    Returns ``(success, message)``.
    """
    user_file, _defaults, section = _config_paths_for(variable, material)
    if section is None:
        return False, 'Material name is required for this parameter.'

    var_key = f'variable_{variable}'
    val_str = _format_value(new_value)
    new_line = f'{var_key}: {val_str}'
    section_header = f'[{section}]'

    # ---- Save old value BEFORE writing ------------------------------------
    old_val = _get_config_value(variable, material)

    # ---- Read existing file ------------------------------------------------
    lines = []
    if os.path.exists(user_file):
        try:
            with open(user_file) as f:
                lines = f.readlines()
        except (IOError, OSError) as exc:
            return False, f'Cannot read {os.path.basename(user_file)}: {exc}'

    # ---- Locate section & variable ----------------------------------------
    section_idx = None
    var_idx = None
    commented_var_idx = None
    next_section_idx = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == section_header:
            section_idx = i
        elif section_idx is not None and next_section_idx is None:
            if stripped.startswith('[') and ']' in stripped:
                next_section_idx = i
            elif stripped == f'{var_key}:' or stripped.startswith(f'{var_key}:'):
                var_idx = i
            elif stripped.startswith(f'# {var_key}:'):
                commented_var_idx = i

    # ---- Apply edit -------------------------------------------------------
    if var_idx is not None:
        lines[var_idx] = new_line + '\n'
    elif commented_var_idx is not None:
        lines[commented_var_idx] = new_line + '\n'
    elif section_idx is not None:
        insert_at = section_idx + 1
        lines.insert(insert_at, new_line + '\n')
    else:
        if lines and not lines[-1].endswith('\n'):
            lines.append('\n')
        lines.append(f'\n{section_header}\n')
        lines.append(new_line + '\n')

    # ---- Write back -------------------------------------------------------
    try:
        os.makedirs(os.path.dirname(user_file), exist_ok=True)
        with open(user_file, 'w') as f:
            f.writelines(lines)
    except (IOError, OSError) as exc:
        return False, f'Cannot write {os.path.basename(user_file)}: {exc}'

    # ---- Record in change log ------------------------------------------
    _log_config_change(variable, old_val, new_value, material)

    return True, (
        f'Saved {variable} = {val_str} to {os.path.basename(user_file)}. '
        f'Restart Klipper to activate.'
    )


def _suggest_change(variable, direction, amount, material=None,
                    minimum=None, maximum=None):
    """Build a config_change dict for a recommendation.

    *direction* is ``'reduce'`` or ``'increase'``.
    Returns ``None`` if the current value can't be read.
    """
    current = _get_config_value(variable, material)
    if current is None:
        return None
    if direction == 'reduce':
        suggested = round(current - amount, 4)
    else:
        suggested = round(current + amount, 4)
    if minimum is not None and suggested < minimum:
        suggested = minimum
    if maximum is not None and suggested > maximum:
        suggested = maximum
    if suggested == current:
        return None
    return {
        'variable': variable,
        'current': current,
        'suggested': suggested,
        'material': material or '',
        'description': f'{variable}: {_format_value(current)} \u2192 {_format_value(suggested)}',
    }


# =============================================================================
# SINGLE-PRINT STATS
# =============================================================================

# =============================================================================
# SHARED CSV LOADER — read once, feed all analyzers
# =============================================================================

def load_csv_rows(csv_file):
    """Read a CSV file once into a list of row dicts.

    Returns an empty list on any I/O or parse error.  The result
    should be passed to all ``analyze_*`` functions via the ``rows``
    parameter so they don't each re-open the same file.
    """
    try:
        with open(csv_file, 'r') as f:
            return list(csv.DictReader(f))
    except Exception as exc:
        print(f"Warning: Could not read {csv_file}: {exc}")
        return []


# =============================================================================
# TTL RESULT CACHE — avoid re-analyzing on rapid refreshes
# =============================================================================

import threading as _threading

_cache_lock = _threading.Lock()
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
        print(f"Banding  : {hr} high-risk events \u2014 cause: {_culprit_name(culprit)}")
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
# WEB DASHBOARD
# =============================================================================

def read_csv_timeline(csv_file, max_points=800, rows=None):
    """Read CSV and downsample for timeline charts."""
    out = []
    all_rows = rows if rows is not None else load_csv_rows(csv_file)
    if not all_rows:
        return []

    step = max(1, len(all_rows) // max_points)
    for i in range(0, len(all_rows), step):
        row = all_rows[i]
        try:
            out.append({
                't': round(float(row['elapsed_s']), 1),
                'ta': round(float(row['temp_actual']), 1),
                'tt': round(float(row['temp_target']), 1),
                'b': round(float(row.get('boost', 0)), 1),
                'f': round(float(row['flow']), 1),
                's': round(float(row['speed']), 1),
                'pw': round(float(row['pwm']), 3),
                'pa': round(float(row.get('pa', 0)), 4),
                'z': round(float(row.get('z_height', 0)), 2),
                'a': int(float(row.get('accel', 0))),
                'dz': int(row.get('dynz_active', 0)),
                'fn': round(float(row.get('fan_pct', 0)), 1),
            })
        except (KeyError, ValueError):
            continue
    return out


def find_active_print_csv(log_dir):
    """Find a CSV that has no matching summary JSON — i.e., a print in progress.

    Returns the CSV path or None.
    """
    csvs = sorted(
        Path(log_dir).glob('*.csv'),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for csv_path in csvs[:5]:  # check most recent 5
        summary_path = str(csv_path).replace('.csv', '_summary.json')
        if not os.path.exists(summary_path):
            # Confirm it's being actively written (modified in last 120s)
            try:
                age = time.time() - csv_path.stat().st_mtime
                if age < 120:
                    return str(csv_path)
            except OSError:
                continue
    return None


def synthesize_live_summary(csv_path, rows=None):
    """Build a summary dict from a live CSV (no summary JSON exists yet)."""
    filename = os.path.basename(csv_path)
    # Extract material from filename.
    # Filename pattern: YYYYMMDD_HHMMSS_PrintName_MATERIAL_duration.gcode.csv
    # Material is a short uppercase token (PLA, PETG, ABS, ASA, TPU, etc.)
    # embedded in the gcode name. Scan parts from the end for a known pattern.
    _KNOWN_MATERIALS = {
        'PLA', 'PETG', 'ABS', 'ASA', 'TPU', 'PA', 'PC', 'NYLON',
        'HIPS', 'PVA', 'PP', 'PEI', 'PCTG', 'CPE',
    }
    parts = filename.replace('.gcode.csv', '').replace('.csv', '').split('_')
    material = 'Unknown'
    # Scan from the end (material is typically second-to-last before duration)
    for part in reversed(parts[2:]):  # skip date & time
        upper = part.strip().upper()
        if upper in _KNOWN_MATERIALS:
            material = upper
            break

    temps = []
    boosts = []
    pwms = []
    flows = []
    speeds = []
    pa_vals = []
    high_risk = 0
    dynz_active = 0
    accel_min = 99999
    total = 0
    start_time = ''
    duration_s = 0

    try:
        _rows = rows if rows is not None else load_csv_rows(csv_path)
        for row in _rows:
                try:
                    total += 1
                    boosts.append(float(row.get('boost', 0)))
                    pwms.append(float(row.get('pwm', 0)))
                    flows.append(float(row.get('flow', 0)))
                    speeds.append(float(row.get('speed', 0)))
                    if float(row.get('pa', 0)) > 0:
                        pa_vals.append(float(row['pa']))
                    if int(row.get('dynz_active', 0)):
                        dynz_active += 1
                    accel = float(row.get('accel', 99999))
                    if accel > 0:
                        accel_min = min(accel_min, accel)
                    if int(row.get('banding_risk', 0)) >= 5:
                        high_risk += 1
                    duration_s = float(row.get('elapsed_s', 0))
                except (KeyError, ValueError):
                    continue
    except Exception:
        return None

    if total == 0:
        return None

    return {
        'material': material,
        'filename': filename,
        'start_time': '',
        'duration_min': round(duration_s / 60, 1),
        'samples': total,
        'avg_boost': round(statistics.mean(boosts), 1) if boosts else 0,
        'max_boost': round(max(boosts), 1) if boosts else 0,
        'avg_pwm': round(statistics.mean(pwms), 3) if pwms else 0,
        'max_pwm': round(max(pwms), 3) if pwms else 0,
        'dynz_active_pct': round(dynz_active / total * 100, 1) if total else 0,
        'accel_min': int(accel_min) if accel_min < 99999 else 0,
        'banding_analysis': {
            'high_risk_events': high_risk,
            'likely_culprit': 'in_progress',
            'accel_changes': 0,
            'pa_changes': 0,
            'dynz_transitions': 0,
            'temp_overshoots': 0,
        },
        '_live': True,
    }


# Map internal banding culprit codes to human-readable descriptions and fixes
_CULPRIT_INFO = {
    'slicer_accel_control': {
        'name': 'Slicer acceleration changes',
        'explain': 'Your slicer is sending frequent acceleration changes (SET_VELOCITY_LIMIT commands). Each change can cause a brief extrusion inconsistency that shows as a line on the print.',
        'fix': 'In your slicer, reduce the number of different acceleration values. OrcaSlicer/PrusaSlicer: try disabling per-feature acceleration overrides, or use fewer distinct values. Reducing max acceleration by 10\u201320% can also help.',
    },
    'pa_oscillation': {
        'name': 'Pressure Advance oscillation',
        'explain': 'The PA value is changing too frequently as temperature fluctuates. Each PA change slightly alters extrusion pressure, leaving a mark on walls.',
        'fix': 'Increase pa_deadband to 0.006\u20130.010 to filter out small PA adjustments. If still oscillating, reduce pa_boost_k by 20\u201330%.',
    },
    'temp_instability': {
        'name': 'Temperature swings',
        'explain': 'The nozzle temperature is changing rapidly \u2014 ramping up and down too aggressively. Each swing affects melt viscosity and shows as banding.',
        'fix': 'Reduce ramp_up_rate by 1\u20132 \u00b0C/s (try 2.0\u20133.0). Increase flow_smoothing to 0.5+ to dampen flow spikes that trigger temp changes.',
    },
    'dynz_accel_switching': {
        'name': 'DynZ acceleration switching',
        'explain': 'The Dynamic Z-Window system is frequently changing acceleration limits as it detects stress zones. Each switch can cause a visible line.',
        'fix': 'Increase dynz_min_accel to soften the acceleration drops (try 2000\u20133000). Or increase dynz_activate_score so DynZ only triggers on clearly stressed zones.',
    },
    'dynz_temp_swings': {
        'name': 'DynZ temperature swings',
        'explain': 'DynZ is using temperature reduction for stress relief, causing frequent temp target changes.',
        'fix': 'Increase dynz_activate_score so it triggers less often. Or switch dynz_relief_method to "accel_limit" if you prefer speed reduction over temp changes.',
    },
    'no_obvious_culprit': {
        'name': 'No clear cause',
        'explain': 'Banding events detected but no single cause dominates. Could be a combination of factors or something mechanical.',
        'fix': 'Check belt tension, Z-axis binding, and filament consistency. If the print looks fine, these may be false positives.',
    },
    'likely_mechanical': {
        'name': 'Likely mechanical issue',
        'explain': 'Banding detected but no software-related cause found. This usually points to a mechanical problem.',
        'fix': 'Check belt tension and idler bearings. Inspect Z-axis lead screws for binding. Verify frame rigidity.',
    },
    'insufficient_data': {
        'name': 'Not enough data',
        'explain': 'Print was too short or had too few samples to determine a cause.',
        'fix': 'Run a longer test print for better diagnostics.',
    },
    'print_too_short': {
        'name': 'Print too short',
        'explain': 'Print duration was too short to reliably detect patterns.',
        'fix': 'Run a longer test print (10+ minutes) for meaningful analysis.',
    },
}


def _culprit_name(code):
    """Translate an internal culprit code to a human-readable name."""
    info = _CULPRIT_INFO.get(code)
    if info:
        return info['name']
    return code.replace('_', ' ').title() if code else 'Unknown'


def _culprit_fix(code):
    """Get the fix action for a culprit code."""
    info = _CULPRIT_INFO.get(code)
    if info:
        return info['fix']
    return f'Investigate the "{code}" cause in the banding analysis.'


def _culprit_explain(code):
    """Get the plain-English explanation for a culprit code."""
    info = _CULPRIT_INFO.get(code)
    if info:
        return info['explain']
    return f'The banding analysis identified "{code}" as a possible cause.'


def generate_recommendations(data):
    """Analyze dashboard data and produce actionable tuning recommendations.

    Returns a list of dicts: {severity, category, title, detail, action}
    severity: 'good', 'info', 'warn', 'bad'
    Optional key ``config_changes`` is a list of param dicts the UI can apply.
    """
    recs = []
    s = data.get('summary') or {}
    lag = data.get('thermal_lag') or {}
    headroom = data.get('headroom') or {}
    pa = data.get('pa_stability') or {}
    ba = s.get('banding_analysis') or {}
    dynz_pct = s.get('dynz_active_pct', 0)
    material = (s.get('material') or '').strip().upper() or None

    # --- Heater saturation ---
    # IMPORTANT: max_pwm hitting 100% is NORMAL during PID ramp-up when the
    # target temperature changes.  A 40W heater will briefly go to full power
    # every time the script requests a new temp — this is expected PID behavior,
    # not true saturation.
    #
    # True saturation = sustained high PWM AND the heater can't reach the target.
    # We use thermal lag data (% time behind target) + avg PWM to distinguish
    # "heater genuinely can't keep up" from "heater momentarily ramping".
    max_pwm = s.get('max_pwm', 0)
    avg_pwm = s.get('avg_pwm', 0)
    lag_pct = lag.get('lag_pct', 0)
    max_lag_val = lag.get('max_lag', 0)

    if avg_pwm >= 0.85 and lag_pct > 10:
        # High average duty AND significant time behind target = real saturation
        rec = {
            'severity': 'bad', 'category': 'Heater',
            'title': f'Heater can\u2019t keep up (avg {avg_pwm*100:.0f}% PWM, {lag_pct:.0f}% lag)',
            'detail': f'The heater averaged {avg_pwm*100:.0f}% duty and fell behind target for {lag_pct:.0f}% of the print (max lag: {max_lag_val:.1f}\u00b0C). This is genuine heater saturation, not just PID ramp-up.',
            'action': 'Reduce flow_k to lower temperature demand. If boost is already moderate, the heater may be undersized \u2014 consider a 60W heater cartridge.',
        }
        chg = _suggest_change('flow_k', 'reduce', 0.3, material=material, minimum=0.1)
        if chg:
            rec['config_changes'] = [chg]
        recs.append(rec)
    elif avg_pwm >= 0.80 and lag_pct > 5:
        # Moderate concern
        rec = {
            'severity': 'warn', 'category': 'Heater',
            'title': f'Heater working hard (avg {avg_pwm*100:.0f}% PWM)',
            'detail': f'Avg heater duty is {avg_pwm*100:.0f}% with {lag_pct:.0f}% lag time. The heater is coping but has little margin.',
            'action': 'Consider reducing flow_k for more headroom, especially if pushing higher speeds.',
        }
        chg = _suggest_change('flow_k', 'reduce', 0.15, material=material, minimum=0.1)
        if chg:
            rec['config_changes'] = [chg]
        recs.append(rec)
    elif max_pwm >= 0.98 and lag_pct <= 2:
        # Hits 100% briefly but keeps up — this is normal PID ramp-up
        recs.append({
            'severity': 'good', 'category': 'Heater',
            'title': 'Heater keeping up well',
            'detail': f'Max PWM hit {max_pwm*100:.0f}% during ramp-up (normal PID behavior) but only {lag_pct:.1f}% lag time \u2014 the heater is reaching target quickly.',
            'action': 'No changes needed. Brief 100% PWM during temperature transitions is expected.',
        })
    elif max_pwm > 0 and avg_pwm < 0.60:
        recs.append({
            'severity': 'good', 'category': 'Heater',
            'title': 'Plenty of heater headroom',
            'detail': f'Avg PWM was only {avg_pwm*100:.0f}% \u2014 the heater barely broke a sweat.',
            'action': 'You could increase flow_k by 0.1\u20130.3 to get better flow adaptation, or print faster.',
        })

    # --- Heater headroom by flow bracket ---
    # Only flag saturation in meaningful flow brackets (>=8 mm³/s).
    # Low-flow brackets often show high PWM during ramp-up, retractions,
    # or travel moves — not actual flow-related heater limits.
    MIN_FLOW_FOR_SATURATION = 8  # mm³/s — below this, high PWM is normal
    if headroom:
        saturated_brackets = []
        for bracket_key, hd in headroom.items():
            p95 = hd.get('p95_pwm', 0)
            if p95 >= 0.95 and hd.get('count', 0) >= 5:
                # Parse the lower bound from the bracket key (e.g. "10-12")
                try:
                    lo = float(bracket_key.split('-')[0])
                except (ValueError, IndexError):
                    lo = 0
                if lo >= MIN_FLOW_FOR_SATURATION:
                    saturated_brackets.append(bracket_key)
        if saturated_brackets:
            first = saturated_brackets[0]
            rec = {
                'severity': 'warn', 'category': 'Heater',
                'title': f'Heater saturates above {first} mm\u00b3/s',
                'detail': f'P95 PWM exceeds 95% in flow brackets: {", ".join(saturated_brackets)}.',
                'action': f'You\u2019re hitting heater limits at {first}+ mm\u00b3/s. Reduce flow_k to lower temperature demand, or cap speeds in your slicer to stay under this flow rate.',
            }
            chg = _suggest_change('flow_k', 'reduce', 0.2, material=material, minimum=0.1)
            if chg:
                rec['config_changes'] = [chg]
            recs.append(rec)

    # --- Thermal lag ---
    lag_pct = lag.get('lag_pct', 0)
    max_lag_val = lag.get('max_lag', 0)
    episodes = lag.get('episodes', [])
    if lag_pct > 15:
        rec = {
            'severity': 'bad', 'category': 'Thermal',
            'title': f'Significant thermal lag ({lag_pct:.0f}% of print)',
            'detail': f'The heater fell behind target temperature for {lag_pct:.0f}% of the print (max lag: {max_lag_val:.1f}\u00b0C).',
            'action': 'Increase ramp rate to pre-heat faster, or reduce flow_k so less temperature is demanded.',
        }
        changes = []
        c = _suggest_change('ramp_rate_rise', 'increase', 1.0, minimum=2.0, maximum=8.0)
        if c:
            changes.append(c)
        c = _suggest_change('flow_k', 'reduce', 0.2, material=material, minimum=0.1)
        if c:
            changes.append(c)
        if changes:
            rec['config_changes'] = changes
        recs.append(rec)
    elif lag_pct > 5:
        rec = {
            'severity': 'warn', 'category': 'Thermal',
            'title': f'Moderate thermal lag ({lag_pct:.0f}% of print)',
            'detail': f'The heater occasionally fell behind target ({len(episodes)} lag episodes, max {max_lag_val:.1f}\u00b0C).',
            'action': 'Try increasing ramp rate to pre-heat faster.',
        }
        c = _suggest_change('ramp_rate_rise', 'increase', 1.0, minimum=2.0, maximum=8.0)
        if c:
            rec['config_changes'] = [c]
        recs.append(rec)
    elif lag.get('total_samples', 0) > 50 and lag_pct < 1:
        recs.append({
            'severity': 'good', 'category': 'Thermal',
            'title': 'Excellent thermal tracking',
            'detail': f'Heater stayed within target throughout \u2014 only {lag_pct:.1f}% lag time.',
            'action': 'No changes needed. Thermal control is well-tuned.',
        })

    # --- PA stability ---
    pa_zones = pa.get('oscillation_zones', [])
    pa_range = pa.get('pa_range', 0)
    pa_changes = pa.get('change_count', 0)
    if len(pa_zones) > 5:
        rec = {
            'severity': 'bad', 'category': 'Pressure Advance',
            'title': f'PA oscillating ({len(pa_zones)} unstable zones)',
            'detail': f'PA changed significantly {pa_changes} times with {len(pa_zones)} oscillation zones. This causes fine ribbing/banding on walls.',
            'action': 'Increase pa_deadband to filter small PA changes. If still oscillating, reduce pa_boost_k.',
        }
        changes = []
        c = _suggest_change('pa_deadband', 'increase', 0.003, minimum=0.004, maximum=0.012)
        if c:
            changes.append(c)
        c = _suggest_change('pa_boost_k', 'reduce', 0.0002, material=material, minimum=0.0002)
        if c:
            changes.append(c)
        if changes:
            rec['config_changes'] = changes
        recs.append(rec)
    elif len(pa_zones) > 0:
        rec = {
            'severity': 'info', 'category': 'Pressure Advance',
            'title': f'{len(pa_zones)} PA oscillation zone(s)',
            'detail': f'Minor PA instability detected. PA range: {pa_range:.4f}, changes: {pa_changes}.',
            'action': 'If you see ribbing at specific Z-heights, increase pa_deadband slightly.',
        }
        c = _suggest_change('pa_deadband', 'increase', 0.001, minimum=0.003, maximum=0.008)
        if c:
            rec['config_changes'] = [c]
        recs.append(rec)
    elif pa.get('samples', 0) > 50 and pa_range < 0.01:
        recs.append({
            'severity': 'good', 'category': 'Pressure Advance',
            'title': 'PA is stable',
            'detail': f'PA stayed within a {pa_range:.4f} range with no oscillation zones.',
            'action': 'No changes needed. PA tuning is good.',
        })

    # --- PA absolute value check ---
    # Detect when PA is above typical range for the material, which causes
    # bulging/rough corners even though oscillation stability is fine.
    _PA_TYPICAL_MAX = {
        'PLA': 0.040, 'PETG': 0.045, 'ABS': 0.045, 'ASA': 0.045,
        'TPU': 0.070, 'PA': 0.045, 'PC': 0.050, 'NYLON': 0.045,
        'HIPS': 0.050,
    }
    pa_val = pa.get('pa_min')  # when stable, pa_min == pa_max
    if pa_val is None and pa.get('pa_max'):
        pa_val = pa.get('pa_max')
    pa_typical_max = _PA_TYPICAL_MAX.get(material, 0.050)
    if pa_val and pa_val > pa_typical_max and pa.get('samples', 0) > 50:
        suggested_pa = round(pa_typical_max - 0.005, 4)
        rec = {
            'severity': 'warn', 'category': 'Pressure Advance',
            'title': f'PA value ({pa_val:.3f}) may be too high for {material or "this material"}',
            'detail': (
                f'Your Pressure Advance is {pa_val:.3f}, which is above the typical '
                f'range for {material or "this material"} (≤{pa_typical_max:.3f}). '
                f'High PA causes over-compensation at corners — the extruder pushes '
                f'too much filament into direction changes, producing bulging or rough '
                f'corners. This is separate from PA oscillation (which is stable).'
            ),
            'action': (
                f'Lower default_pa to ~{suggested_pa:.3f} using the Apply button below. '
                f'Adaptive Flow will use this as the base and dynamically adjust from there.'
            ),
        }
        c = _suggest_change('default_pa', 'reduce',
                            round(pa_val - suggested_pa, 4),
                            material=material,
                            minimum=0.020, maximum=pa_val - 0.002)
        if c:
            rec['config_changes'] = [c]
        recs.append(rec)

    # --- Banding risk ---
    high_risk = ba.get('high_risk_events', 0)
    culprit = ba.get('likely_culprit', '')
    culprit_friendly = _culprit_name(culprit) if culprit else 'Unknown'

    def _culprit_config_changes(code):
        """Build config_changes list appropriate for a banding culprit."""
        changes = []
        if not code:
            return changes
        if 'temp' in code:
            c = _suggest_change('ramp_rate_rise', 'reduce', 1.0, minimum=2.0, maximum=6.0)
            if c:
                changes.append(c)
            c = _suggest_change('flow_smoothing', 'increase', 0.15, minimum=0.2, maximum=0.8)
            if c:
                changes.append(c)
        elif 'pa' in code:
            c = _suggest_change('pa_deadband', 'increase', 0.003, minimum=0.004, maximum=0.012)
            if c:
                changes.append(c)
        elif 'dynz_accel' in code:
            c = _suggest_change('dynz_activate_score', 'increase', 2.0, minimum=4.0, maximum=12.0)
            if c:
                changes.append(c)
        elif 'slicer_accel' in code:
            c = _suggest_change('flow_smoothing', 'increase', 0.1, minimum=0.2, maximum=0.6)
            if c:
                changes.append(c)
        return changes

    _banding_fallback = 'Too many events \u2014 visible banding is very likely.'
    if high_risk > 50:
        detail_explain = _culprit_explain(culprit) if culprit else _banding_fallback
        rec = {
            'severity': 'bad', 'category': 'Banding',
            'title': f'{high_risk} high-risk banding events',
            'detail': f'Cause: {culprit_friendly}. {detail_explain}',
            'action': _culprit_fix(culprit) if culprit else 'Investigate belt tension, Z-axis, and nozzle condition.',
        }
        changes = _culprit_config_changes(culprit)
        if changes:
            rec['config_changes'] = changes
        recs.append(rec)
    elif high_risk > 20:
        detail_explain = _culprit_explain(culprit) if culprit else 'May be visible on smooth surfaces.'
        action_fix = _culprit_fix(culprit) if culprit else 'If visible, check mechanical and slicer settings.'
        rec = {
            'severity': 'warn', 'category': 'Banding',
            'title': f'{high_risk} banding risk events',
            'detail': f'Moderate banding risk. Cause: {culprit_friendly}. {detail_explain}',
            'action': f'Inspect the print at flagged Z-heights. {action_fix}',
        }
        changes = _culprit_config_changes(culprit)
        if changes:
            rec['config_changes'] = changes
        recs.append(rec)
    elif high_risk <= 5 and s.get('duration_min', 0) > 5:
        recs.append({
            'severity': 'good', 'category': 'Banding',
            'title': 'Minimal banding risk',
            'detail': f'Only {high_risk} risk events \u2014 excellent for print quality.',
            'action': 'No changes needed.',
        })

    # --- Temp boost ---
    avg_boost = s.get('avg_boost', 0)
    max_boost = s.get('max_boost', 0)
    if max_boost > 30:
        rec = {
            'severity': 'warn', 'category': 'Temperature',
            'title': f'Very high temp boost (max {max_boost:.0f}\u00b0C)',
            'detail': 'Large temperature swings can cause oozing, stringing, and inconsistent extrusion.',
            'action': 'Reduce flow_k to lower temp demand, or lower max_boost_limit to cap the boost.',
        }
        chg = _suggest_change('flow_k', 'reduce', 0.3, material=material, minimum=0.1)
        if chg:
            rec['config_changes'] = [chg]
        recs.append(rec)
    elif avg_boost > 0 and avg_boost < 3 and max_boost < 8:
        recs.append({
            'severity': 'info', 'category': 'Temperature',
            'title': 'Low temp boost needed',
            'detail': f'Average boost was only {avg_boost:.1f}\u00b0C \u2014 this print didn\u2019t push the hotend hard.',
            'action': 'Flow demands were low. You could increase flow_k slightly if you want more responsiveness at higher speeds.',
        })

    # --- DynZ ---
    if dynz_pct > 20:
        recs.append({
            'severity': 'info', 'category': 'DynZ',
            'title': f'DynZ active {dynz_pct}% of layers',
            'detail': 'Very complex geometry required frequent acceleration reduction.',
            'action': 'This is normal for domes/spheres. If print quality is fine, no changes needed. If too slow, increase dynz_min_accel.',
        })

    # =====================================================================
    # CROSS-PRINT TREND ANALYSIS (same material only)
    # =====================================================================
    trends = data.get('trends') or []
    current_material = (s.get('material') or '').strip().upper()

    # Filter to same material — comparing PLA vs ABS would be meaningless
    if current_material:
        same_mat = [t for t in trends
                    if (t.get('material') or '').strip().upper() == current_material]
    else:
        same_mat = trends  # unknown material, use all

    mat_label = current_material or 'this material'

    if len(same_mat) >= 3:
        recent = same_mat[-3:]  # last 3 same-material prints (chronological)

        # --- Banding trend ---
        risk_vals = [t.get('high_risk', 0) for t in recent]
        if all(risk_vals[i] < risk_vals[i + 1] for i in range(len(risk_vals) - 1)):
            delta = risk_vals[-1] - risk_vals[0]
            if delta > 5:
                recs.append({
                    'severity': 'warn', 'category': 'Trend',
                    'title': f'Banding risk climbing ({risk_vals[0]} \u2192 {risk_vals[-1]} over 3 {mat_label} prints)',
                    'detail': 'Risk events have increased each print. Something is degrading \u2014 nozzle wear, partial clog, or a config change made things worse.',
                    'action': 'Compare what changed between prints. If nothing was changed, inspect nozzle for wear or partial blockage. Cold pull recommended.',
                })
        elif all(risk_vals[i] > risk_vals[i + 1] for i in range(len(risk_vals) - 1)):
            if risk_vals[0] > 10:
                recs.append({
                    'severity': 'good', 'category': 'Trend',
                    'title': f'Banding improving ({risk_vals[0]} \u2192 {risk_vals[-1]} across {mat_label})',
                    'detail': 'Risk events dropping over recent prints \u2014 your changes are working.',
                    'action': 'Keep current settings. Continue monitoring.',
                })

        # --- Heater duty trend ---
        pwm_vals = [t.get('max_pwm', 0) for t in recent]
        if all(pwm_vals[i] < pwm_vals[i + 1] for i in range(len(pwm_vals) - 1)):
            if pwm_vals[-1] > 0.85:
                recs.append({
                    'severity': 'warn', 'category': 'Trend',
                    'title': f'Heater duty trending up for {mat_label} ({pwm_vals[0]*100:.0f}% \u2192 {pwm_vals[-1]*100:.0f}%)',
                    'detail': 'Max PWM has increased over recent prints. Could indicate nozzle wear, partial clog increasing back-pressure, or ambient temp drop.',
                    'action': 'Check nozzle for blockage. If nozzle is clean, the heater may be losing efficiency \u2014 check thermistor and heater cartridge connections.',
                })

        # --- Boost trend ---
        boost_vals = [t.get('avg_boost', 0) for t in recent]
        if all(boost_vals[i] < boost_vals[i + 1] for i in range(len(boost_vals) - 1)):
            delta = boost_vals[-1] - boost_vals[0]
            if delta > 3:
                recs.append({
                    'severity': 'info', 'category': 'Trend',
                    'title': f'Avg boost climbing for {mat_label} ({boost_vals[0]:.1f} \u2192 {boost_vals[-1]:.1f}\u00b0C)',
                    'detail': 'Prints are requiring more temperature boost. Could be printing faster/thicker, or increased back-pressure from wear.',
                    'action': 'If intentional (faster prints), this is fine. If not, check nozzle condition.',
                })

        # --- Repeated same culprit ---
        culprits = [t.get('culprit', '-') for t in recent]
        culprits_real = [c for c in culprits if c and c != '-' and c.lower() != 'none']
        if len(culprits_real) >= 2 and len(set(culprits_real)) == 1:
            repeated = culprits_real[0]
            repeated_name = _culprit_name(repeated)
            rec = {
                'severity': 'warn', 'category': 'Trend',
                'title': f'Same cause repeating for {mat_label}: {repeated_name}',
                'detail': f'"{repeated_name}" has appeared in your last {len(culprits_real)} {mat_label} prints. {_culprit_explain(repeated)}',
                'action': _culprit_fix(repeated),
            }
            changes = _culprit_config_changes(repeated)
            if changes:
                rec['config_changes'] = changes
            recs.append(rec)

    elif len(same_mat) >= 2:
        # With just 2 same-material prints, check for a big jump
        prev, curr = same_mat[-2], same_mat[-1]
        risk_jump = curr.get('high_risk', 0) - prev.get('high_risk', 0)
        if risk_jump > 20:
            recs.append({
                'severity': 'warn', 'category': 'Trend',
                'title': f'Banding risk jumped +{risk_jump} since last print',
                'detail': f'Previous: {prev.get("high_risk", 0)} events, now: {curr.get("high_risk", 0)}.',
                'action': 'Something changed between these prints. Review config changes, material, or slicer settings.',
            })
        pwm_jump = curr.get('max_pwm', 0) - prev.get('max_pwm', 0)
        if pwm_jump > 0.10:
            recs.append({
                'severity': 'info', 'category': 'Trend',
                'title': f'Heater duty jumped +{pwm_jump*100:.0f}% since last print',
                'detail': f'Previous max PWM: {prev.get("max_pwm", 0)*100:.0f}%, now: {curr.get("max_pwm", 0)*100:.0f}%.',
                'action': 'Higher flow demand or ambient temp change? If unexpected, check nozzle condition.',
            })

    # --- No trend data note ---
    if len(same_mat) < 2 and s.get('duration_min', 0) > 0:
        extra = ''
        if current_material and len(trends) >= 2:
            extra = f' ({len(trends)} total prints on record, but only {len(same_mat)} with {mat_label})'
        recs.append({
            'severity': 'info', 'category': 'Trend',
            'title': f'Not enough {mat_label} prints for trends',
            'detail': f'Only {len(same_mat)} {mat_label} print(s) on record.{extra} Need at least 2 for comparisons, 3+ for trend detection.',
            'action': f'Keep printing with {mat_label} \u2014 recommendations will get smarter as data accumulates.',
        })

    # --- All good ---
    if not recs or all(r['severity'] == 'good' for r in recs):
        recs.append({
            'severity': 'good', 'category': 'Overall',
            'title': 'Print looks well-tuned',
            'detail': 'No significant issues detected across heater, thermal lag, PA, or banding analysis.',
            'action': 'Keep current settings. If you want to push speed/flow higher, do it incrementally and check the next print\u2019s dashboard.',
        })

    # Sort: bad first, then warn, info, good
    severity_order = {'bad': 0, 'warn': 1, 'info': 2, 'good': 3}
    recs.sort(key=lambda r: severity_order.get(r['severity'], 9))

    return recs


def annotate_recommendations(recs, log_dir, material=None):
    """Post-process recommendations to detect recently-applied config changes.

    Uses the config change log to determine whether the user has already
    acted on a variable.  If a change was logged for the same variable +
    material and fewer than 5 prints have completed since, the
    recommendation is downgraded to 'info' (monitoring) or 'good'
    (resolved) and the Apply button is replaced with a status badge.

    After 5+ prints the old data has fully cycled out; if the
    recommendation still fires at *bad/warn* it means the change wasn't
    enough and a fresh Apply button is shown.

    Modifies *recs* in place and returns them.
    """
    change_log = _load_config_change_log()
    if not change_log:
        return recs  # nothing ever applied — skip annotation

    for rec in recs:
        changes = rec.get('config_changes')
        if not changes:
            continue

        all_applied = True
        any_applied = False

        for chg in changes:
            var = chg.get('variable')
            # Use the material from the config_change itself ('' means
            # global/non-material-specific).  Only fall back to the
            # function-level material when the key is completely absent.
            chg_mat = chg.get('material')
            if chg_mat is None:
                mat = material  # key absent — fall back
            elif chg_mat == '':
                mat = None       # explicitly global variable
            else:
                mat = chg_mat    # material-specific variable

            # Look up the most recent dashboard-applied change for this var
            ts, old_val, new_val = _last_change_for(var, mat)

            if ts is None:
                # Never applied through the dashboard
                chg['applied'] = False
                all_applied = False
                continue

            # Count prints of this material since the change
            prints_since = _count_prints_since(log_dir, mat, ts) if log_dir else 0

            if prints_since >= 5:
                # Enough new data — if the rec still fires, the old change
                # was insufficient.  Show a fresh Apply button.
                chg['applied'] = False
                all_applied = False
            else:
                chg['applied'] = True
                chg['applied_at'] = ts
                chg['prints_since'] = prints_since
                chg['applied_old'] = old_val
                chg['applied_new'] = new_val
                any_applied = True

        if all_applied and changes:
            best_prints = max(c.get('prints_since', 0) for c in changes)
            if best_prints >= 1:
                rec['severity'] = 'info'
                rec['title'] = '\u231b ' + rec['title'] + ' (monitoring)'
                rec['detail'] += (
                    f' \\u2014 Settings applied, {best_prints} of ~5 prints completed. '
                    f'Recommendation will update as more data arrives.'
                )
            else:
                rec['severity'] = 'info'
                rec['title'] = '\u2713 ' + rec['title'] + ' (applied, awaiting prints)'
                rec['detail'] += (
                    ' \\u2014 Settings saved but no prints completed yet with the new config. '
                    'Print with this material to see the effect.'
                )
        elif any_applied and changes:
            rec['title'] = rec['title'] + ' (partially applied)'

    # Re-sort after severity changes
    severity_order = {'bad': 0, 'warn': 1, 'info': 2, 'good': 3}
    recs.sort(key=lambda r: severity_order.get(r['severity'], 9))

    return recs


# =========================================================================
# MATERIAL-AGGREGATED ANALYSIS
# =========================================================================

def _weighted_avg(values, weights):
    """Return weighted average, or 0 if no data."""
    total_w = sum(weights)
    if total_w == 0:
        return 0
    return sum(v * w for v, w in zip(values, weights)) / total_w


def _merge_headroom(all_headroom):
    """Merge heater headroom dicts from multiple prints."""
    merged = {}
    for hr in all_headroom:
        for bracket, data in hr.items():
            if bracket not in merged:
                merged[bracket] = {'count': 0, 'sum_avg_pwm': 0, 'sum_p95': 0,
                                   'sum_max': 0, 'total_count': 0}
            n = data.get('count', 0)
            merged[bracket]['total_count'] += n
            merged[bracket]['count'] += 1
            merged[bracket]['sum_avg_pwm'] += data.get('avg_pwm', 0) * n
            merged[bracket]['sum_p95'] += data.get('p95_pwm', 0) * n
            merged[bracket]['sum_max'] = max(merged[bracket]['sum_max'],
                                             data.get('max_pwm', 0))
    result = {}
    for bracket, m in merged.items():
        tc = m['total_count']
        if tc == 0:
            continue
        result[bracket] = {
            'count': tc,
            'avg_pwm': round(m['sum_avg_pwm'] / tc, 4),
            'p95_pwm': round(m['sum_p95'] / tc, 4),
            'max_pwm': m['sum_max'],
        }
    return result


def _merge_z_banding(all_zb):
    """Merge z-banding dicts from multiple prints."""
    merged = {}
    for zb in all_zb:
        for zkey, data in zb.items():
            if zkey not in merged:
                merged[zkey] = {'samples': 0, 'risk_sum': 0, 'high_risk': 0,
                                'accel_changes': 0, 'pa_changes': 0,
                                'dynz_transitions': 0, 'events': []}
            merged[zkey]['samples'] += data.get('samples', 0)
            merged[zkey]['risk_sum'] += data.get('risk_sum', 0)
            merged[zkey]['high_risk'] += data.get('high_risk', 0)
            merged[zkey]['accel_changes'] += data.get('accel_changes', 0)
            merged[zkey]['pa_changes'] += data.get('pa_changes', 0)
            merged[zkey]['dynz_transitions'] += data.get('dynz_transitions', 0)
            # Keep a capped list of events
            merged[zkey]['events'].extend(data.get('events', [])[:5])
    return merged


def _merge_speed_flow(all_sf):
    """Merge speed/flow distribution dicts from multiple prints."""
    merged = {'speed': {}, 'flow': {}}
    for sf in all_sf:
        for kind in ('speed', 'flow'):
            for bracket, data in sf.get(kind, {}).items():
                if bracket not in merged[kind]:
                    merged[kind][bracket] = {'count': 0, 'pct_sum': 0,
                                             'boost_sum': 0, 'pa_sum': 0,
                                             'pwm_sum': 0, 'n': 0}
                n = data.get('count', 0)
                merged[kind][bracket]['count'] += n
                merged[kind][bracket]['n'] += 1
                merged[kind][bracket]['pct_sum'] += data.get('pct', 0)
                merged[kind][bracket]['boost_sum'] += data.get('avg_boost', 0) * n
                merged[kind][bracket]['pa_sum'] += data.get('avg_pa', 0) * n
                merged[kind][bracket]['pwm_sum'] += data.get('avg_pwm', 0) * n
    result = {'speed': {}, 'flow': {}}
    for kind in ('speed', 'flow'):
        for bracket, m in merged[kind].items():
            tc = m['count']
            if tc == 0:
                continue
            result[kind][bracket] = {
                'count': tc,
                'pct': round(m['pct_sum'] / m['n'], 1) if m['n'] else 0,
                'avg_boost': round(m['boost_sum'] / tc, 1),
                'avg_pa': round(m['pa_sum'] / tc, 4),
                'avg_pwm': round(m['pwm_sum'] / tc, 3),
            }
    return result


def collect_material_overview(log_dir, material):
    """Aggregate analysis data across all prints of a given material.

    Returns a dict with the same shape as collect_dashboard_data() but with
    merged/averaged values across all sessions of *material*.
    """
    sessions = find_recent_sessions(log_dir, count=50, material=material)
    if not sessions:
        return {'error': f'No sessions found for {material}',
                'material': material, 'session_count': 0}

    n = len(sessions)
    summaries = [s['summary'] for s in sessions]
    csv_files = [s['csv_file'] for s in sessions]

    # --- Aggregated summary stats (weighted by sample count) ---
    samples_list = [s.get('samples', 1) for s in summaries]
    total_samples = sum(samples_list)
    total_duration = sum(s.get('duration_min', 0) for s in summaries)

    agg_summary = {
        'material': material,
        'session_count': n,
        'duration_min': round(total_duration, 1),
        'samples': total_samples,
        'avg_boost': round(_weighted_avg(
            [s.get('avg_boost', 0) for s in summaries], samples_list), 1),
        'max_boost': round(max((s.get('max_boost', 0) for s in summaries), default=0), 1),
        'avg_pwm': round(_weighted_avg(
            [s.get('avg_pwm', 0) for s in summaries], samples_list), 3),
        'max_pwm': round(max((s.get('max_pwm', 0) for s in summaries), default=0), 2),
        'avg_flow': round(_weighted_avg(
            [s.get('avg_flow', 0) for s in summaries], samples_list), 1),
        'max_flow': round(max((s.get('max_flow', 0) for s in summaries), default=0), 1),
        'max_speed': round(max((s.get('max_speed', 0) for s in summaries), default=0), 1),
        'avg_thermal_lag': round(_weighted_avg(
            [s.get('avg_thermal_lag', 0) for s in summaries], samples_list), 2),
        'dynz_active_pct': round(_weighted_avg(
            [s.get('dynz_active_pct', 0) for s in summaries], samples_list), 1),
        'banding_analysis': {
            'high_risk_events': sum(
                s.get('banding_analysis', {}).get('high_risk_events', 0)
                for s in summaries),
            'avg_high_risk_per_print': round(sum(
                s.get('banding_analysis', {}).get('high_risk_events', 0)
                for s in summaries) / n, 1),
            'likely_culprit': _agg_most_common_culprit(summaries),
        },
    }

    # --- Per-CSV deep analysis (heavier — only latest N) ---
    MAX_CSVS = 5  # cap to avoid very slow aggregation on low-power hardware
    recent_csvs = csv_files[:MAX_CSVS]

    # Pre-load each CSV once (single I/O per file)
    csv_row_cache = {c: load_csv_rows(c) for c in recent_csvs}

    # Thermal lag
    all_lag = [analyze_thermal_lag(c, rows=csv_row_cache[c]) for c in recent_csvs]
    all_lag = [l for l in all_lag if l]
    if all_lag:
        lag_samples = [l.get('total_samples', 1) for l in all_lag]
        agg_lag = {
            'avg_lag': round(_weighted_avg(
                [l['avg_lag'] for l in all_lag], lag_samples), 2),
            'max_lag': round(max(l['max_lag'] for l in all_lag), 1),
            'lag_pct': round(_weighted_avg(
                [l['lag_pct'] for l in all_lag], lag_samples), 1),
            'total_samples': sum(lag_samples),
            'episodes': [],  # flatten top episodes
        }
        all_eps = []
        for l in all_lag:
            all_eps.extend(l.get('episodes', [])[:5])
        all_eps.sort(key=lambda e: e.get('max_lag', 0), reverse=True)
        agg_lag['episodes'] = all_eps[:10]
    else:
        agg_lag = None

    # Heater headroom
    all_hr = [analyze_heater_headroom(c, rows=csv_row_cache[c]) for c in recent_csvs]
    all_hr_fmt = []
    for hr in all_hr:
        if hr:
            all_hr_fmt.append({f"{k[0]}-{k[1]}": v for k, v in hr.items()})
    agg_headroom = _merge_headroom(all_hr_fmt) if all_hr_fmt else None

    # PA stability
    all_pa = [analyze_pa_stability(c, rows=csv_row_cache[c]) for c in recent_csvs]
    all_pa = [p for p in all_pa if p and 'pa_min' in p]
    if all_pa:
        pa_samples = [p.get('samples', 1) for p in all_pa]
        agg_pa = {
            'samples': sum(pa_samples),
            'pa_min': round(min(p['pa_min'] for p in all_pa), 4),
            'pa_max': round(max(p['pa_max'] for p in all_pa), 4),
            'pa_range': round(max(p['pa_max'] for p in all_pa) -
                              min(p['pa_min'] for p in all_pa), 4),
            'pa_stdev': round(_weighted_avg(
                [p['pa_stdev'] for p in all_pa], pa_samples), 5),
            'change_count': sum(p.get('change_count', 0) for p in all_pa),
            'oscillation_zones': [],
        }
        all_zones = []
        for p in all_pa:
            all_zones.extend(p.get('oscillation_zones', [])[:5])
        all_zones.sort(key=lambda z: z.get('changes', 0), reverse=True)
        agg_pa['oscillation_zones'] = all_zones[:15]
    else:
        agg_pa = None

    # Z-banding
    all_zb = []
    for c in recent_csvs:
        raw = analyze_z_banding(c, bin_size=0.5, rows=csv_row_cache[c])
        all_zb.append({str(k): v for k, v in raw.items()})
    agg_zb = _merge_z_banding(all_zb) if all_zb else {}

    # DynZ zones
    all_dz = []
    for c in recent_csvs:
        raw = analyze_dynz_zones(c, bin_size=0.5, rows=csv_row_cache[c])
        all_dz.append({str(k): v for k, v in raw.items()})
    agg_dynz = {}
    for dz in all_dz:
        for zk, data in dz.items():
            if zk not in agg_dynz:
                agg_dynz[zk] = {'samples': 0, 'active_sum': 0, 'transitions': 0,
                                'accel_sum': 0, 'stress_sum': 0}
            n_s = data.get('samples', 0)
            agg_dynz[zk]['samples'] += n_s
            agg_dynz[zk]['active_sum'] += data.get('active_pct', 0) * n_s
            agg_dynz[zk]['transitions'] += data.get('transitions', 0)
            agg_dynz[zk]['accel_sum'] += data.get('avg_accel', 0) * n_s
            agg_dynz[zk]['stress_sum'] += data.get('avg_stress', 0) * n_s
    for zk in agg_dynz:
        s_n = agg_dynz[zk]['samples']
        if s_n:
            agg_dynz[zk] = {
                'samples': s_n,
                'active_pct': round(agg_dynz[zk]['active_sum'] / s_n, 1),
                'transitions': agg_dynz[zk]['transitions'],
                'avg_accel': round(agg_dynz[zk]['accel_sum'] / s_n),
                'avg_stress': round(agg_dynz[zk]['stress_sum'] / s_n, 1),
            }

    # Speed/flow distribution
    all_sf = []
    for c in recent_csvs:
        raw = analyze_speed_flow_distribution(c, rows=csv_row_cache[c])
        if raw:
            all_sf.append({
                'speed': {f"{k[0]}-{k[1]}": v for k, v in raw['speed'].items()},
                'flow': {f"{k[0]}-{k[1]}": v for k, v in raw['flow'].items()},
            })
    agg_sf = _merge_speed_flow(all_sf) if all_sf else None

    # Trends (for the selected material)
    trend_data = []
    for s_info in reversed(sessions[:10]):
        sm = s_info['summary']
        ba = sm.get('banding_analysis', {})
        ts = sm.get('start_time', '')
        trend_data.append({
            'date': ts[:10] if len(ts) >= 10 else ts,
            'material': sm.get('material', ''),
            'avg_boost': sm.get('avg_boost', 0),
            'max_boost': sm.get('max_boost', 0),
            'avg_pwm': sm.get('avg_pwm', 0),
            'max_pwm': sm.get('max_pwm', 0),
            'high_risk': ba.get('high_risk_events', 0),
            'culprit': ba.get('likely_culprit', '-'),
        })

    # Session list (this material only)
    session_list = [
        {
            'filename': s['summary'].get('filename', ''),
            'material': s['summary'].get('material', ''),
            'start_time': s['summary'].get('start_time', ''),
            'summary_file': os.path.basename(s['summary_file']),
        }
        for s in sessions
    ]

    data = {
        'summary': agg_summary,
        'timeline': [],  # no combined timeline for aggregate
        'z_banding': agg_zb,
        'thermal_lag': agg_lag,
        'headroom': agg_headroom,
        'pa_stability': agg_pa,
        'dynz_zones': agg_dynz,
        'speed_flow': agg_sf,
        'trends': trend_data,
        'sessions': session_list,
        'selected_file': '',
        'is_live': False,
        'is_aggregate': True,
        'aggregate_material': material,
        'aggregate_sessions': n,
    }

    # Generate recommendations from aggregated data
    data['recommendations'] = generate_recommendations(data)
    annotate_recommendations(data['recommendations'], log_dir, material)

    return data


def _agg_most_common_culprit(summaries):
    """Find the most repeated banding culprit across summaries."""
    counts = defaultdict(int)
    for s in summaries:
        c = s.get('banding_analysis', {}).get('likely_culprit', '')
        if c and c != '-' and c.lower() != 'none':
            counts[c] += 1
    if not counts:
        return 'none'
    return max(counts, key=counts.get)


def collect_dashboard_data(log_dir, summary_path=None, material=None):
    """Gather all analysis data for the web dashboard."""
    data = {
        'summary': None, 'timeline': [], 'z_banding': {},
        'thermal_lag': None, 'headroom': None, 'pa_stability': None,
        'dynz_zones': {}, 'speed_flow': None, 'trends': None,
        'sessions': [], 'selected_file': '', 'is_live': False,
        'materials': [],
    }

    all_sessions = find_recent_sessions(log_dir, count=50, material=material)

    # Build list of unique materials across all sessions (unfiltered)
    all_unfiltered = find_recent_sessions(log_dir, count=100)
    mat_set = set()
    for s in all_unfiltered:
        m = (s['summary'].get('material') or '').strip().upper()
        if m:
            mat_set.add(m)
    data['materials'] = sorted(mat_set)

    data['sessions'] = [
        {
            'filename': s['summary'].get('filename', ''),
            'material': s['summary'].get('material', ''),
            'start_time': s['summary'].get('start_time', ''),
            'summary_file': os.path.basename(s['summary_file']),
        }
        for s in all_sessions
    ]

    # Check for an active (live) print first — a CSV with no summary JSON
    csv_path = None
    csv_rows = None   # will be loaded once when csv_path is known
    if summary_path is None:
        live_csv = find_active_print_csv(log_dir)
        if live_csv:
            csv_path = live_csv
            csv_rows = load_csv_rows(live_csv)
            live_summary = synthesize_live_summary(live_csv, rows=csv_rows)
            if live_summary:
                data['summary'] = live_summary
                data['selected_file'] = os.path.basename(live_csv)
                data['is_live'] = True

    # Fall back to completed print
    if data['summary'] is None:
        if summary_path is None:
            summary_path = find_latest_summary(log_dir)
        if summary_path is None:
            return data

        data['selected_file'] = os.path.basename(summary_path)
        summary = load_summary(summary_path)
        if summary.get('_error'):
            data['error'] = summary['_error']
            return data
        data['summary'] = summary
        csv_path = summary_path.replace('_summary.json', '.csv')

    if csv_path is None or not os.path.exists(csv_path):
        return data

    # === Load CSV once and pass to all analyzers ===
    if csv_rows is None:
        csv_rows = load_csv_rows(csv_path)

    data['timeline'] = read_csv_timeline(csv_path, rows=csv_rows)

    data['z_banding'] = {
        str(k): v for k, v in analyze_z_banding(csv_path, bin_size=0.5, rows=csv_rows).items()
    }

    lag = analyze_thermal_lag(csv_path, rows=csv_rows)
    if lag:
        lag['episodes'] = [
            {'start_s': ep['start_s'],
             'end_s': ep.get('end_s', ep['start_s']),
             'max_lag': ep['max_lag'], 'max_flow': ep['max_flow'],
             'z_start': ep['z_start']}
            for ep in lag['episodes'][:20]
        ]
    data['thermal_lag'] = lag

    headroom_raw = analyze_heater_headroom(csv_path, rows=csv_rows)
    if headroom_raw:
        data['headroom'] = {
            f"{k[0]}-{k[1]}": v for k, v in headroom_raw.items()
        }

    pa = analyze_pa_stability(csv_path, rows=csv_rows)
    if pa:
        pa['oscillation_zones'] = [
            {'start_s': z['start_s'],
             'end_s': z.get('end_s', z['start_s']),
             'pa_min': z['pa_min'], 'pa_max': z['pa_max'],
             'z_start': z['z_start'], 'changes': z['changes']}
            for z in pa['oscillation_zones'][:20]
        ]
    data['pa_stability'] = pa

    data['dynz_zones'] = {
        str(k): v for k, v in analyze_dynz_zones(csv_path, rows=csv_rows).items()
    }

    sfd = analyze_speed_flow_distribution(csv_path, rows=csv_rows)
    if sfd:
        data['speed_flow'] = {
            'speed': {f"{k[0]}-{k[1]}": v for k, v in sfd['speed'].items()},
            'flow': {f"{k[0]}-{k[1]}": v for k, v in sfd['flow'].items()},
        }

    if len(all_sessions) >= 2:
        trend_data = []
        for s in reversed(all_sessions[:10]):
            sm = s['summary']
            ba = sm.get('banding_analysis', {})
            ts = sm.get('start_time', '')
            trend_data.append({
                'date': ts[:10] if len(ts) >= 10 else ts,
                'material': sm.get('material', ''),
                'avg_boost': sm.get('avg_boost', 0),
                'max_boost': sm.get('max_boost', 0),
                'avg_pwm': sm.get('avg_pwm', 0),
                'max_pwm': sm.get('max_pwm', 0),
                'high_risk': ba.get('high_risk_events', 0),
                'culprit': ba.get('likely_culprit', '-'),
            })
        data['trends'] = trend_data

    # Generate actionable recommendations based on all collected data
    data['recommendations'] = generate_recommendations(data)
    mat = (data.get('summary') or {}).get('material') or material
    annotate_recommendations(data['recommendations'], log_dir, mat)

    return data


DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Adaptive Flow Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4" onerror="document.title='Chart.js FAILED to load'"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:#0d1117;color:#c9d1d9;line-height:1.5}
.hdr{background:#161b22;border-bottom:1px solid #30363d;padding:14px 24px;
display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}
.hdr h1{font-size:18px;font-weight:600}
.hdr select{background:#21262d;color:#c9d1d9;border:1px solid #30363d;
padding:6px 12px;border-radius:6px;font-size:13px;max-width:420px}
.ctrls{display:flex;gap:12px;align-items:center}
.ctrls label{font-size:13px;color:#8b949e;cursor:pointer}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
gap:12px;padding:16px 24px}
.cd{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px}
.cd .lb{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;
display:flex;align-items:center;gap:4px}
.cd .vl{font-size:22px;font-weight:600;margin-top:4px}
.cd .sb{font-size:12px;color:#8b949e;margin-top:2px}
.cd .tip{position:relative;display:inline-flex;align-items:center;justify-content:center;
width:14px;height:14px;border-radius:50%;background:#30363d;color:#8b949e;
font-size:9px;cursor:help;flex-shrink:0}
.cd .tip:hover::after{content:attr(data-tip);position:absolute;bottom:calc(100% + 6px);
left:50%;transform:translateX(-50%);background:#1c2128;color:#c9d1d9;border:1px solid #30363d;
border-radius:6px;padding:6px 10px;font-size:11px;line-height:1.4;white-space:normal;
width:240px;z-index:10;text-transform:none;letter-spacing:0;font-weight:400;
box-shadow:0 4px 12px rgba(0,0,0,.4)}
.cd .tip:hover::before{content:'';position:absolute;bottom:calc(100% + 2px);
left:50%;transform:translateX(-50%);border:4px solid transparent;
border-top-color:#30363d;z-index:11}
.tabs{display:flex;gap:0;padding:0 24px;border-bottom:1px solid #30363d;
background:#161b22;overflow-x:auto}
.tab{padding:10px 16px;font-size:13px;color:#8b949e;cursor:pointer;
border-bottom:2px solid transparent;white-space:nowrap;transition:all .2s}
.tab:hover{color:#c9d1d9}
.tab.active{color:#58a6ff;border-bottom-color:#58a6ff}
.area{padding:16px 24px}
.box{background:#161b22;border:1px solid #30363d;border-radius:8px;
padding:16px;margin-bottom:16px}
.box h3{font-size:14px;margin-bottom:12px;color:#8b949e}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
canvas{max-height:350px}
.w{color:#d29922}.d{color:#f85149}.g{color:#3fb950}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:6px 10px;color:#8b949e;border-bottom:1px solid #30363d}
td{padding:6px 10px;border-bottom:1px solid #21262d}
.foot{text-align:center;padding:16px;font-size:11px;color:#484f58}
.rec{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;
margin-bottom:12px;border-left:4px solid #30363d}
.rec.sev-bad{border-left-color:#f85149}.rec.sev-warn{border-left-color:#d29922}
.rec.sev-info{border-left-color:#58a6ff}.rec.sev-good{border-left-color:#3fb950}
.rec .rec-hd{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.rec .rec-badge{font-size:10px;font-weight:700;text-transform:uppercase;padding:2px 8px;
border-radius:4px;letter-spacing:.5px}
.sev-bad .rec-badge{background:rgba(248,81,73,.15);color:#f85149}
.sev-warn .rec-badge{background:rgba(210,153,34,.15);color:#d29922}
.sev-info .rec-badge{background:rgba(88,166,255,.15);color:#58a6ff}
.sev-good .rec-badge{background:rgba(63,185,80,.15);color:#3fb950}
.rec .rec-cat{font-size:11px;color:#8b949e}
.rec .rec-title{font-size:15px;font-weight:600}
.rec .rec-detail{font-size:13px;color:#8b949e;margin:4px 0 8px}
.rec .rec-action{font-size:13px;color:#c9d1d9;background:#0d1117;border-radius:6px;
padding:10px 14px;display:flex;gap:8px;align-items:flex-start}
.rec .rec-action::before{content:'\\2192';color:#58a6ff;font-weight:700;flex-shrink:0}
.cfg-changes{margin-top:10px;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:12px 14px}
.cfg-changes .cfg-hd{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}
.cfg-row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:6px 0;
border-bottom:1px solid #21262d}
.cfg-row:last-child{border-bottom:none}
.cfg-desc{font-size:13px;color:#c9d1d9;font-family:'SF Mono',Consolas,monospace}
.cfg-btn{background:#238636;color:#fff;border:none;border-radius:6px;padding:5px 14px;
font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap;transition:background .15s}
.cfg-btn:hover{background:#2ea043}
.cfg-btn:disabled{background:#21262d;color:#484f58;cursor:default}
.cfg-btn.applied{background:#1a7f37}
.cfg-applied-badge{display:inline-block;background:#1a7f37;color:#3fb950;border-radius:6px;
padding:4px 12px;font-size:12px;font-weight:600;white-space:nowrap}
.cfg-monitor{display:inline-block;font-size:11px;color:#8b949e;margin-left:8px;font-style:italic}
.cfg-toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);
background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 20px;
font-size:13px;color:#c9d1d9;box-shadow:0 4px 24px rgba(0,0,0,.5);z-index:9999;
opacity:0;transition:opacity .3s}
.cfg-toast.show{opacity:1}
.cfg-toast.ok{border-color:#3fb950}.cfg-toast.err{border-color:#f85149}
.pulse{animation:pulse 1.5s infinite}@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.mat-sel{display:flex;gap:4px;align-items:center;flex-wrap:wrap}
.mat-btn{background:#21262d;color:#8b949e;border:1px solid #30363d;border-radius:6px;
padding:4px 12px;font-size:12px;cursor:pointer;transition:all .15s;white-space:nowrap}
.mat-btn:hover{color:#c9d1d9;border-color:#58a6ff}
.mat-btn.active{background:#1f6feb;color:#fff;border-color:#1f6feb}
.mat-btn.agg{background:#0d419d;color:#58a6ff;border-color:#1f6feb}
.agg-badge{display:inline-block;background:rgba(88,166,255,.15);color:#58a6ff;
font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;
letter-spacing:.5px;margin-left:6px;text-transform:uppercase}
@media(max-width:768px){.row2{grid-template-columns:1fr}
.cards{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<div class="hdr">
<h1>\u26a1 Adaptive Flow Dashboard</h1>
<div class="ctrls">
<div class="mat-sel" id="matsel"></div>
<span id="lv" style="display:none;font-size:12px;color:#3fb950">
<span class="pulse">\u25cf</span> LIVE</span>
<label><input type="checkbox" id="ar"> Auto-refresh</label>
<select id="ss" onchange="go(this.value)"></select>
</div></div>
<div class="cards" id="cds"><div class="cd"><div class="vl" style="color:#8b949e">Loading\u2026</div></div></div>
<div class="tabs" id="tb"></div>
<div class="area" id="ca"></div>
<noscript><div style="margin:24px;padding:16px;background:#2d1214;border:1px solid #f85149;border-radius:8px;color:#f85149">JavaScript is disabled or failed to load. The dashboard requires JavaScript.</div></noscript>
<div id="_err" style="display:none;margin:24px;padding:16px;background:#2d1214;border:1px solid #f85149;border-radius:8px;color:#f85149;font-family:monospace;white-space:pre-wrap"></div>
<div class="foot" id="ft">Adaptive Flow Dashboard</div>
<script>
var D;
try{D=__DASHBOARD_DATA__}catch(e){
document.getElementById('_err').style.display='block';
document.getElementById('_err').textContent='Data parse error: '+e;throw e}
try{
var isLive=D.is_live||false;
var isAgg=D.is_aggregate||false;
var sel=document.getElementById('ss');
var lvi=document.getElementById('lv');
var ftel=document.getElementById('ft');
var matSelEl=document.getElementById('matsel');
var activeMat=null;

// Show LIVE indicator
if(isLive)lvi.style.display='inline';

// --- Material buttons ---
function renderMatBtns(){
var mats=D.materials||[];
if(mats.length<2){matSelEl.style.display='none';return}
matSelEl.style.display='';
var h='<span style="font-size:11px;color:#484f58;margin-right:4px">View:</span>';
h+='<button class="mat-btn'+(activeMat?'':' active')+'" onclick="setMat(null)">Per-Print</button>';
mats.forEach(function(m){
h+='<button class="mat-btn'+(activeMat===m?' active agg':'')+'" onclick="setMat(\\''+m+'\\')">'+m+' Aggregate</button>'});
matSelEl.innerHTML=h}
renderMatBtns();

function setMat(m){
if(m===activeMat)return;
activeMat=m;
renderMatBtns();
if(m){
sel.style.display='none';
fetchAgg(m)}
else{
sel.style.display='';
window.location.href='/'}}

function fetchAgg(m){
ftel.textContent='Loading '+m+' aggregate...';
fetch('/api/material-data?material='+encodeURIComponent(m))
.then(function(r){return r.json()}).then(function(nd){
D=nd;isLive=false;isAgg=true;
sel.style.display='none';
ftel.textContent=m+ ' \u2014 Aggregated across '+nd.aggregate_sessions+' prints';
rc();buildTabs();rCh()})
.catch(function(e){ftel.textContent='Error loading: '+e})}

// Populate session selector
if(!isLive){
(D.sessions||[]).forEach(function(s){var o=document.createElement('option');
o.value=s.summary_file;
o.textContent=(s.start_time||'').slice(0,16)+' | '+s.material+' | '+s.filename;
if(s.summary_file===D.selected_file)o.selected=true;sel.appendChild(o)});
} else {
var o=document.createElement('option');o.value='__live__';
o.textContent='\u25cf LIVE PRINT';o.selected=true;sel.appendChild(o);
(D.sessions||[]).forEach(function(s){var o2=document.createElement('option');
o2.value=s.summary_file;
o2.textContent=(s.start_time||'').slice(0,16)+' | '+s.material+' | '+s.filename;
sel.appendChild(o2)});
}

function go(f){
if(f==='__live__')window.location.href='/';
else window.location.href='/?session='+encodeURIComponent(f)}

// Auto-refresh: 5s when live, 30s otherwise
var arCb=document.getElementById('ar');
var rt=null;
if(isLive){arCb.checked=true; startPoll()}
arCb.addEventListener('change',function(){
if(this.checked)startPoll(); else stopPoll()});

function startPoll(){
stopPoll();
var iv=isLive?5000:30000;
rt=setInterval(pollData,iv);
ftel.textContent='Auto-refresh '+(iv/1000)+'s'}
function stopPoll(){if(rt)clearInterval(rt);rt=null;
ftel.textContent=isAgg?(D.aggregate_material+' aggregate'):'Adaptive Flow Dashboard'}

function pollData(){
if(isAgg&&activeMat){fetchAgg(activeMat);return}
var url='/api/data';
var cur=sel.value;
if(cur&&cur!=='__live__')url+='?session='+encodeURIComponent(cur);
fetch(url).then(function(r){return r.json()}).then(function(nd){
D=nd;isLive=D.is_live||false;isAgg=D.is_aggregate||false;
if(isLive)lvi.style.display='inline'; else lvi.style.display='none';
rc();rCh();
}).catch(function(){})}

function rc(){var c=document.getElementById('cds'),s=D.summary;
if(!s){c.innerHTML='<div class="cd"><div class="vl d">No data</div></div>';return}
var ba=s.banding_analysis||{},dp=s.dynz_active_pct||0;
var liveBadge=s._live?'<span style="color:#3fb950;font-size:11px"> \u25cf PRINTING</span>':'';
var aggBadge=isAgg?'<span class="agg-badge">'+s.session_count+' prints</span>':'';
var items;
if(isAgg){
items=[
{l:'Material',v:(s.material||'?')+aggBadge,s:s.session_count+' prints, '+(s.duration_min||0).toFixed(0)+' min total',
d:'Aggregated data across all prints with this material.'},
{l:'Avg Boost',v:(s.avg_boost||0).toFixed(1)+'\u00b0C',s:'max '+(s.max_boost||0).toFixed(1)+'\u00b0C across all prints',
d:'Weighted average temp boost across all prints of this material.'},
{l:'Heater Duty',v:((s.avg_pwm||0)*100).toFixed(0)+'%',s:'max '+((s.max_pwm||0)*100).toFixed(0)+'%',w:(s.avg_pwm||0)>0.85,
d:'Weighted average heater duty across all prints.'},
{l:'Avg Banding/Print',v:''+(ba.avg_high_risk_per_print!=null?ba.avg_high_risk_per_print.toFixed(0):(ba.high_risk_events||0)),
s:'total '+(ba.high_risk_events||0)+', culprit: '+(ba.likely_culprit||'none'),w:(ba.high_risk_events||0)>50,
d:'Average banding risk events per print. Total across all prints shown.'},
{l:'DynZ',v:dp>0?dp+'%':'Off',s:dp>0?'averaged':'inactive across prints',
d:'Weighted average DynZ activation across prints.'}]}
else{
items=[
{l:'Material',v:(s.material||'?')+liveBadge,s:(s.duration_min||0).toFixed(1)+' min'+(s._live?' elapsed':''),
d:'Active material profile and total print duration.'},
{l:'Temp Boost',v:(s.avg_boost||0).toFixed(1)+'\u00b0C',s:'max '+(s.max_boost||0).toFixed(1)+'\u00b0C',
d:'Extra temperature added above base to meet flow demand. \u2022 0\u201310\u00b0C = light load (good) \u2022 10\u201325\u00b0C = moderate \u2022 25\u00b0C+ = heavy load, check if heater can keep up'},
{l:'Heater Duty',v:((s.avg_pwm||0)*100).toFixed(0)+'%',s:'max '+((s.max_pwm||0)*100).toFixed(0)+'%',w:(s.avg_pwm||0)>0.85,
d:'Average heater power. Max hitting 100% is normal during temp ramps (PID behavior). \u2022 Avg under 60% = lots of headroom (good) \u2022 60\u201380% = healthy \u2022 80%+ avg with thermal lag = heater struggling'},
{l:'DynZ',v:dp>0?dp+'%':'Off',s:dp>0?'min accel '+(s.accel_min||0):'inactive',
d:'% of layers where accel was reduced for tricky geometry. \u2022 0% = simple print, no intervention needed (good) \u2022 1\u201315% = normal for curves/overhangs \u2022 15%+ = very complex geometry'},
{l:'Banding',v:''+(ba.high_risk_events||0),s:s._live?'in progress':ba.likely_culprit||'none',w:(ba.high_risk_events||0)>10,
d:'Samples flagged as banding risk from rapid temp/PA/accel changes. \u2022 0\u20135 = excellent \u2022 5\u201320 = minor, unlikely visible \u2022 20\u201350 = moderate, check print quality \u2022 50+ = high, likely visible banding'}]}
c.innerHTML=items.map(function(x){return '<div class="cd"><div class="lb">'+
x.l+(x.d?'<span class="tip" data-tip="'+x.d+'">?</span>':'')+
'</div><div class="vl'+(x.w?' w':'')+'">'+x.v+
'</div><div class="sb">'+x.s+'</div></div>'}).join('')}
rc();

var allTabs=[{id:'rx',l:'\u2699 Recommendations'},{id:'tl',l:'Timeline'},{id:'zh',l:'Z-Height'},{id:'ht',l:'Heater'},
{id:'pa',l:'PA'},{id:'dz',l:'DynZ'},{id:'ds',l:'Distribution'},{id:'tr',l:'Trends'}];
var at='rx',tb=document.getElementById('tb'),ca=document.getElementById('ca');
function buildTabs(){
var tabs=allTabs;
if(isAgg)tabs=allTabs.filter(function(t){return t.id!=='tl'});
if(isAgg&&at==='tl')at='rx';
tb.innerHTML=tabs.map(function(t){
return '<div class="tab'+(t.id===at?' active':'')+
'" onclick="sTab(this.dataset.t)" data-t="'+t.id+'">'+t.l+'</div>'}).join('')}
function rTabs(){buildTabs()}
function sTab(id){at=id;rTabs();rCh()}
rTabs();

if(typeof Chart!=='undefined'){
Chart.defaults.color='#8b949e';
Chart.defaults.borderColor='#30363d';
Chart.defaults.font.size=12;}
var CH={};
function dCh(){for(var k in CH){if(CH[k]&&CH[k].destroy)CH[k].destroy()}CH={}}
function mc(id){return '<canvas id="'+id+'"></canvas>'}

function rCh(){dCh();var tl=D.timeline||[];
if(at==='rx')rRec();
else if(at==='tl')rTimeline(tl);
else if(at==='zh')rZH();
else if(at==='ht')rHt();
else if(at==='pa')rPA(tl);
else if(at==='dz')rDZ();
else if(at==='ds')rDist();
else if(at==='tr')rTr()}

function rTimeline(tl){
if(!tl.length){ca.innerHTML='<p>No timeline data.</p>';return}
ca.innerHTML='<div class="box"><h3>Temperature</h3>'+mc('c1')+
'</div><div class="box"><h3>Flow &amp; Speed</h3>'+mc('c2')+'</div>';
var lb=tl.map(function(r){return r.t});
CH.c1=new Chart(document.getElementById('c1'),{type:'line',data:{labels:lb,
datasets:[
{label:'Target',data:tl.map(function(r){return r.tt}),borderColor:'#58a6ff',
borderWidth:1.5,pointRadius:0,fill:false},
{label:'Actual',data:tl.map(function(r){return r.ta}),borderColor:'#f85149',
borderWidth:1.5,pointRadius:0,fill:false},
{label:'Boost',data:tl.map(function(r){return r.b}),borderColor:'#d29922',
borderWidth:1,pointRadius:0,fill:true,
backgroundColor:'rgba(210,153,34,0.1)',yAxisID:'y1'}]},
options:{responsive:true,animation:false,
interaction:{intersect:false,mode:'index'},
scales:{x:{title:{display:true,text:'Time (s)'},ticks:{maxTicksLimit:15}},
y:{title:{display:true,text:'\u00b0C'},position:'left'},
y1:{title:{display:true,text:'Boost \u00b0C'},position:'right',
grid:{drawOnChartArea:false}}},
plugins:{legend:{position:'top'}}}});
CH.c2=new Chart(document.getElementById('c2'),{type:'line',data:{labels:lb,
datasets:[
{label:'Flow (mm\u00b3/s)',data:tl.map(function(r){return r.f}),
borderColor:'#3fb950',borderWidth:1.5,pointRadius:0,fill:true,
backgroundColor:'rgba(63,185,80,0.1)',yAxisID:'y'},
{label:'Speed (mm/s)',data:tl.map(function(r){return r.s}),
borderColor:'#d29922',borderWidth:1,pointRadius:0,fill:false,yAxisID:'y1'},
{label:'PWM',data:tl.map(function(r){return r.pw}),
borderColor:'#bc8cff',borderWidth:1,pointRadius:0,fill:false,yAxisID:'y2'}]},
options:{responsive:true,animation:false,
interaction:{intersect:false,mode:'index'},
scales:{x:{title:{display:true,text:'Time (s)'},ticks:{maxTicksLimit:15}},
y:{title:{display:true,text:'mm\u00b3/s'},position:'left'},
y1:{title:{display:true,text:'mm/s'},position:'right',
grid:{drawOnChartArea:false}},
y2:{display:false}}}})}

function rZH(){
var zb=D.z_banding;
if(!zb||!Object.keys(zb).length){ca.innerHTML='<p>No Z-height data.</p>';return}
ca.innerHTML='<div class="box"><h3>Banding Risk by Z-Height</h3>'+mc('c3')+'</div>';
var ks=Object.keys(zb).sort(function(a,b){return parseFloat(a)-parseFloat(b)});
var risks=ks.map(function(k){return zb[k].samples?zb[k].risk_sum/zb[k].samples:0});
var cols=risks.map(function(r){return r>=4?'#f85149':r>=2?'#d29922':'#3fb950'});
CH.c3=new Chart(document.getElementById('c3'),{type:'bar',data:{
labels:ks.map(function(k){return parseFloat(k).toFixed(1)+'mm'}),
datasets:[{label:'Avg Risk',data:risks,backgroundColor:cols}]},
options:{responsive:true,animation:false,indexAxis:'y',
scales:{x:{title:{display:true,text:'Avg Risk Score'}}}}})}

function rHt(){
ca.innerHTML='';
var hr=D.headroom;
if(hr&&Object.keys(hr).length){
ca.innerHTML+='<div class="box"><h3>Heater Headroom by Flow Rate</h3>'+mc('c4')+'</div>';
var ks=Object.keys(hr).sort(function(a,b){return parseFloat(a)-parseFloat(b)});
ks=ks.filter(function(k){return hr[k].count>=3});
setTimeout(function(){
CH.c4=new Chart(document.getElementById('c4'),{type:'bar',data:{
labels:ks.map(function(k){return k.indexOf('inf')>=0?k.split('-')[0]+'+':k}),
datasets:[
{label:'Avg PWM',data:ks.map(function(k){return hr[k].avg_pwm*100}),
backgroundColor:'#58a6ff'},
{label:'P95 PWM',data:ks.map(function(k){return hr[k].p95_pwm*100}),
backgroundColor:'#d29922'},
{label:'Max PWM',data:ks.map(function(k){return hr[k].max_pwm*100}),
backgroundColor:'#f85149'}]},
options:{responsive:true,animation:false,
scales:{x:{title:{display:true,text:'Flow (mm\u00b3/s)'}},
y:{title:{display:true,text:'PWM %'},max:100}}}})},0)}
var lg=D.thermal_lag;
if(lg){var el=document.createElement('div');el.className='box';
var h='<h3>Thermal Lag</h3><p>Avg: '+lg.avg_lag.toFixed(1)+
'\u00b0C | Max: '+lg.max_lag.toFixed(1)+
'\u00b0C | Time in lag: '+lg.lag_pct.toFixed(1)+'%</p>';
if(lg.episodes.length){
h+='<table><tr><th>#</th><th>Time</th><th>Max Lag</th><th>Flow</th><th>Z</th></tr>';
lg.episodes.slice(0,8).forEach(function(e,i){
h+='<tr><td>'+(i+1)+'</td><td>'+e.start_s.toFixed(0)+
's</td><td class="'+(e.max_lag>=5?'d':'w')+'">'+
e.max_lag.toFixed(1)+'\u00b0C</td><td>'+
e.max_flow.toFixed(1)+'</td><td>'+e.z_start.toFixed(1)+'mm</td></tr>'});
h+='</table>'}else{h+='<p class="g">\u2713 No significant lag</p>'}
el.innerHTML=h;ca.appendChild(el)}
if(!hr&&!lg)ca.innerHTML='<p>No heater data.</p>'}

function rPA(tl){
ca.innerHTML='';
if(tl.length){
ca.innerHTML+='<div class="box"><h3>Pressure Advance over Time</h3>'+mc('c5')+'</div>';
setTimeout(function(){
CH.c5=new Chart(document.getElementById('c5'),{type:'line',data:{
labels:tl.map(function(r){return r.t}),
datasets:[{label:'PA',data:tl.map(function(r){return r.pa}),
borderColor:'#58a6ff',borderWidth:1.5,pointRadius:0,fill:true,
backgroundColor:'rgba(88,166,255,0.1)'}]},
options:{responsive:true,animation:false,
interaction:{intersect:false,mode:'index'},
scales:{x:{title:{display:true,text:'Time (s)'},ticks:{maxTicksLimit:15}},
y:{title:{display:true,text:'PA Value'}}}}})},0)}
var pa=D.pa_stability;
if(pa&&pa.samples>=10){var el=document.createElement('div');el.className='box';
var h='<h3>PA Stability</h3><p>Range: '+pa.pa_min.toFixed(4)+
' \u2014 '+pa.pa_max.toFixed(4)+' (span '+pa.pa_range.toFixed(4)+
') | Stdev: '+pa.pa_stdev.toFixed(5)+' | Changes: '+pa.change_count+'</p>';
if(pa.oscillation_zones.length){
h+='<table><tr><th>#</th><th>Time</th><th>Dur</th><th>PA span</th><th>Changes</th><th>Z</th></tr>';
pa.oscillation_zones.slice(0,8).forEach(function(z,i){
h+='<tr><td>'+(i+1)+'</td><td>'+z.start_s.toFixed(0)+
's</td><td>'+(z.end_s-z.start_s).toFixed(0)+
's</td><td>'+(z.pa_max-z.pa_min).toFixed(4)+
'</td><td>'+z.changes+'</td><td>'+z.z_start.toFixed(1)+'mm</td></tr>'});
h+='</table>'}else{h+='<p class="g">\u2713 PA is stable</p>'}
el.innerHTML=h;ca.appendChild(el)}
if(!tl.length&&!pa)ca.innerHTML='<p>No PA data.</p>'}

function rDZ(){
var dz=D.dynz_zones;
if(!dz||!Object.keys(dz).length){
ca.innerHTML='<p>No DynZ data (may be inactive).</p>';return}
ca.innerHTML='<div class="box"><h3>DynZ Activation by Z-Height</h3>'+mc('c6')+'</div>';
var ks=Object.keys(dz).sort(function(a,b){return parseFloat(a)-parseFloat(b)});
CH.c6=new Chart(document.getElementById('c6'),{type:'bar',data:{
labels:ks.map(function(k){return parseFloat(k).toFixed(1)+'mm'}),
datasets:[
{label:'Active %',data:ks.map(function(k){return dz[k].active_pct}),
backgroundColor:'#d29922',yAxisID:'y'},
{label:'Transitions',data:ks.map(function(k){return dz[k].transitions}),
backgroundColor:'#f85149',yAxisID:'y1'}]},
options:{responsive:true,animation:false,
scales:{x:{title:{display:true,text:'Z Height'}},
y:{title:{display:true,text:'Active %'},position:'left'},
y1:{title:{display:true,text:'Transitions'},position:'right',
grid:{drawOnChartArea:false}}}}})}

function rDist(){
var sf=D.speed_flow;
if(!sf){ca.innerHTML='<p>No distribution data.</p>';return}
ca.innerHTML='<div class="row2"><div class="box"><h3>Time by Speed</h3>'+
mc('c7')+'</div><div class="box"><h3>Time by Flow Rate</h3>'+mc('c8')+'</div></div>';
if(sf.speed){
var sk=Object.keys(sf.speed).sort(function(a,b){return parseFloat(a)-parseFloat(b)});
CH.c7=new Chart(document.getElementById('c7'),{type:'bar',data:{
labels:sk.map(function(k){return k.indexOf('999')>=0?k.split('-')[0]+'+':k}),
datasets:[{label:'% Time',data:sk.map(function(k){return sf.speed[k].pct}),
backgroundColor:'#d29922'}]},
options:{responsive:true,animation:false,
scales:{x:{title:{display:true,text:'Speed (mm/s)'}},
y:{title:{display:true,text:'% of Print'}}}}})}
if(sf.flow){
var fk=Object.keys(sf.flow).sort(function(a,b){return parseFloat(a)-parseFloat(b)});
CH.c8=new Chart(document.getElementById('c8'),{type:'bar',data:{
labels:fk.map(function(k){return k.indexOf('999')>=0?k.split('-')[0]+'+':k}),
datasets:[{label:'% Time',data:fk.map(function(k){return sf.flow[k].pct}),
backgroundColor:'#3fb950'}]},
options:{responsive:true,animation:false,
scales:{x:{title:{display:true,text:'Flow (mm\u00b3/s)'}},
y:{title:{display:true,text:'% of Print'}}}}})}}

function rTr(){
var tr=D.trends;
if(!tr||tr.length<2){ca.innerHTML='<p>Need at least 2 prints for trends.</p>';return}
var rows='';tr.forEach(function(t){
rows+='<tr><td>'+t.date+'</td><td>'+t.material+
'</td><td>'+t.avg_boost.toFixed(1)+'\u00b0C</td><td class="'+
(t.max_pwm>0.95?'d':'')+'">'+(t.max_pwm*100).toFixed(0)+
'%</td><td class="'+(t.high_risk>10?'w':'')+'">'+t.high_risk+
'</td><td>'+t.culprit+'</td></tr>'});
ca.innerHTML='<div class="box"><h3>Print-over-Print Trends</h3>'+mc('c9')+
'</div><div class="box"><h3>Details</h3><table><tr><th>Date</th><th>Material</th>'+
'<th>Boost</th><th>Max PWM</th><th>Risk</th><th>Culprit</th></tr>'+rows+'</table></div>';
CH.c9=new Chart(document.getElementById('c9'),{type:'line',data:{
labels:tr.map(function(t){return t.date}),
datasets:[
{label:'Avg Boost (\u00b0C)',data:tr.map(function(t){return t.avg_boost}),
borderColor:'#d29922',borderWidth:2,pointRadius:4,fill:false,yAxisID:'y'},
{label:'Risk Events',data:tr.map(function(t){return t.high_risk}),
borderColor:'#f85149',borderWidth:2,pointRadius:4,fill:false,yAxisID:'y1'},
{label:'Avg PWM (%)',data:tr.map(function(t){return t.avg_pwm*100}),
borderColor:'#bc8cff',borderWidth:2,pointRadius:4,fill:false,yAxisID:'y'}]},
options:{responsive:true,animation:false,
interaction:{intersect:false,mode:'index'},
scales:{x:{title:{display:true,text:'Print Date'}},
y:{title:{display:true,text:'\u00b0C / %'},position:'left'},
y1:{title:{display:true,text:'Events'},position:'right',
grid:{drawOnChartArea:false}}}}})}

function rRec(){
var recs=D.recommendations||[];
if(!recs.length){ca.innerHTML='<div class="box"><p>No data available for recommendations yet.</p></div>';return}
var sevLabel={bad:'Issue',warn:'Warning',info:'Note',good:'Good'};
var badCount=recs.filter(function(r){return r.severity==='bad'}).length;
var warnCount=recs.filter(function(r){return r.severity==='warn'}).length;
var h='<div style="margin-bottom:16px"><h3 style="color:#c9d1d9;font-size:16px;margin-bottom:4px">';
if(badCount>0)h+='<span style="color:#f85149">'+badCount+' issue'+(badCount>1?'s':'')+'</span> found';
else if(warnCount>0)h+='<span style="color:#d29922">'+warnCount+' warning'+(warnCount>1?'s':'')+'</span> to review';
else h+='<span style="color:#3fb950">All looking good</span>';
h+='</h3><p style="font-size:12px;color:#484f58">Based on heater, thermal lag, PA, banding, and DynZ analysis</p></div>';
h+=recs.map(function(r,ri){
var html='<div class="rec sev-'+r.severity+'">'+
'<div class="rec-hd">'+
'<span class="rec-badge">'+(sevLabel[r.severity]||r.severity)+'</span>'+
'<span class="rec-cat">'+r.category+'</span>'+
'</div>'+
'<div class="rec-title">'+r.title+'</div>'+
'<div class="rec-detail">'+r.detail+'</div>'+
'<div class="rec-action">'+r.action+'</div>';
if(r.config_changes&&r.config_changes.length){
html+='<div class="cfg-changes"><div class="cfg-hd">Suggested config changes</div>';
r.config_changes.forEach(function(c,ci){
var id='cfg_'+ri+'_'+ci;
html+='<div class="cfg-row">';
html+='<span class="cfg-desc">'+c.description+'</span>';
if(c.applied){
html+='<span class="cfg-applied-badge">\\u2713 Applied</span>';
if(c.prints_since>0){
html+='<span class="cfg-monitor">'+c.prints_since+' print'+(c.prints_since>1?'s':'')+' since change</span>'}
else{html+='<span class="cfg-monitor">Awaiting new prints</span>'}}
else{html+='<button class="cfg-btn" id="'+id+'" onclick="applyChange(\\''+id+'\\',\\''+
c.variable+'\\','+c.suggested+',\\''+(c.material||'')+'\\')">Apply</button>'}
html+='</div>'});
html+='<div style="font-size:11px;color:#484f58;margin-top:6px">' +
'Saves to your user config. Restart Klipper to activate.</div></div>'}
html+='</div>';return html}).join('');
ca.innerHTML=h}

function showToast(msg,ok){
var t=document.createElement('div');t.className='cfg-toast '+(ok?'ok':'err');
t.textContent=msg;document.body.appendChild(t);
setTimeout(function(){t.classList.add('show')},10);
setTimeout(function(){t.classList.remove('show');setTimeout(function(){t.remove()},300)},4000)}

function applyChange(btnId,variable,value,material){
var btn=document.getElementById(btnId);if(!btn||btn.disabled)return;
btn.disabled=true;btn.textContent='Applying...';
fetch('/api/apply-config',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({variable:variable,value:value,material:material||null})})
.then(function(r){return r.json()})
.then(function(d){
if(d.success){btn.textContent='\u2713 Applied';btn.classList.add('applied');showToast(d.message,true)}
else{btn.textContent='Apply';btn.disabled=false;showToast('Error: '+d.message,false)}})
.catch(function(e){btn.textContent='Apply';btn.disabled=false;showToast('Request failed: '+e,false)})}

rCh();
}catch(e){
document.getElementById('_err').style.display='block';
document.getElementById('_err').textContent='Dashboard error: '+e+'\\n'+(e.stack||'');
console.error('Dashboard error',e)}
</script>
</body>
</html>"""


def _sanitize_floats(obj):
    """Recursively replace NaN/Infinity floats with None so json.dumps
    (with allow_nan=False) won't choke."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_floats(v) for v in obj]
    return obj


def _safe_json_for_html(obj):
    """Serialize *obj* to JSON safe for embedding inside <script>.

    Escapes '</script>' and '<!--' sequences that would break the HTML
    parser, and converts NaN/Infinity floats to null.
    """
    clean = _sanitize_floats(obj)
    raw = json.dumps(clean, default=str, allow_nan=False)
    # Prevent premature </script> or HTML comment injection
    raw = raw.replace('</', '<\\/')
    raw = raw.replace('<!--', '<\\!--')
    return raw


def generate_dashboard_html(data):
    """Generate a self-contained HTML dashboard with embedded Chart.js."""
    data_json = _safe_json_for_html(data)
    return DASHBOARD_TEMPLATE.replace('__DASHBOARD_DATA__', data_json)


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for the Adaptive Flow dashboard."""
    log_dir = LOG_DIR
    material = None
    timeout = 30  # seconds – prevents stale connections from blocking the server

    def _resolve_session(self, params):
        """Resolve session file from query params to summary path."""
        session_file = params.get('session', [None])[0]
        if session_file:
            candidate = os.path.join(self.log_dir, session_file)
            if os.path.exists(candidate):
                return candidate
        return None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == '/api/data':
            # JSON API for live polling — returns fresh analysis data
            summary_path = self._resolve_session(params)
            cache_key = f"api_data:{summary_path}:{self.material}"
            data = _cache_get(cache_key)
            if data is None:
                try:
                    data = collect_dashboard_data(
                        self.log_dir,
                        summary_path=summary_path,
                        material=self.material,
                    )
                except Exception as exc:
                    import traceback
                    traceback.print_exc()
                    data = {'error': str(exc)}
                # Don't cache live prints (data changes every second)
                if not data.get('is_live'):
                    _cache_set(cache_key, data)
            payload = json.dumps(data, default=str).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(payload)

        elif parsed.path == '/api/material-data':
            # Aggregated data for a single material across all prints
            mat = params.get('material', [None])[0]
            if not mat:
                data = {'error': 'material parameter required'}
            else:
                mat_upper = mat.strip().upper()
                cache_key = f"mat_data:{mat_upper}"
                data = _cache_get(cache_key)
                if data is None:
                    try:
                        data = collect_material_overview(self.log_dir, mat_upper)
                    except Exception as exc:
                        import traceback
                        traceback.print_exc()
                        data = {'error': str(exc)}
                    _cache_set(cache_key, data)
            payload = json.dumps(data, default=str).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(payload)

        elif parsed.path in ('/', ''):
            summary_path = self._resolve_session(params)
            try:
                data = collect_dashboard_data(
                    self.log_dir,
                    summary_path=summary_path,
                    material=self.material,
                )
            except Exception as exc:
                import traceback
                traceback.print_exc()
                data = {'error': str(exc), 'summary': None, 'recommendations': [],
                        'timeline': [], 'trends': [], 'sessions': [],
                        'is_live': False, 'selected_file': '',
                        'z_banding': {}, 'thermal_lag': None,
                        'headroom': None, 'pa_stability': None,
                        'dynz_zones': {}, 'speed_flow': None}
            html = generate_dashboard_html(data).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(html)))
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            self.end_headers()
            self.wfile.write(html)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == '/api/apply-config':
            length = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except (json.JSONDecodeError, ValueError):
                body = {}

            variable = body.get('variable', '')
            value = body.get('value')
            mat = body.get('material') or None

            if not variable or value is None:
                resp = {'success': False, 'message': 'Missing variable or value.'}
            else:
                ok, msg = _apply_config_change(variable, value, material=mat)
                if ok:
                    _cache_invalidate()  # config changed — stale analysis
                resp = {'success': ok, 'message': msg}

            payload = json.dumps(resp).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress default request logging


def serve_dashboard(port, log_dir, material=None):
    """Start the dashboard web server."""
    DashboardHandler.log_dir = log_dir
    DashboardHandler.material = material

    server = http.server.ThreadingHTTPServer(('0.0.0.0', port), DashboardHandler)

    hostname = socket.gethostname()
    try:
        ip = socket.gethostbyname(hostname)
    except socket.gaierror:
        ip = '0.0.0.0'

    print(f"\n{'=' * 60}")
    print(f"  Adaptive Flow Dashboard")
    print(f"{'=' * 60}")
    print(f"\n  Local:   http://localhost:{port}")
    print(f"  Network: http://{ip}:{port}")
    print(f"\n  Log dir: {log_dir}")
    if material:
        print(f"  Material filter: {material}")
    print(f"\n  Press Ctrl+C to stop")
    print(f"{'=' * 60}\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
        server.server_close()


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

  Z-height banding heatmap (latest or specific print):
    python3 analyze_print.py --z-map
    python3 analyze_print.py --z-map my_print_summary.json

  Print-over-print trends:
    python3 analyze_print.py --trend 10
    python3 analyze_print.py --trend 10 --material PLA

  Thermal lag report (latest or specific print):
    python3 analyze_print.py --lag

  Heater headroom analysis:
    python3 analyze_print.py --headroom

  PA stability analysis:
    python3 analyze_print.py --pa-stability

  DynZ zone map:
    python3 analyze_print.py --dynz-map

  Speed/flow distribution:
    python3 analyze_print.py --distribution

  Web dashboard (open in browser):
    python3 analyze_print.py --serve
    python3 analyze_print.py --serve --port 8080
        """,
    )
    parser.add_argument(
        'summary_file', nargs='?',
        help='Path to summary JSON (default: most recent)')
    parser.add_argument(
        '--count', '-c', type=int,
        help='Analyze last N prints for banding patterns')
    parser.add_argument(
        '--z-map', action='store_true',
        help='Show Z-height banding heatmap for a single print')
    parser.add_argument(
        '--z-bin', type=float, default=0.5,
        help='Z-height bin size in mm for --z-map (default: 0.5)')
    parser.add_argument(
        '--trend', '-t', type=int,
        help='Show trends across last N prints')
    parser.add_argument(
        '--lag', action='store_true',
        help='Show thermal lag report for a single print')
    parser.add_argument(
        '--lag-threshold', type=float, default=3.0,
        help='Thermal lag threshold in \u00b0C (default: 3.0)')
    parser.add_argument(
        '--headroom', action='store_true',
        help='Show heater headroom analysis for a single print')
    parser.add_argument(
        '--pa-stability', action='store_true',
        help='Show PA stability analysis for a single print')
    parser.add_argument(
        '--dynz-map', action='store_true',
        help='Show DynZ activation zone map for a single print')
    parser.add_argument(
        '--distribution', action='store_true',
        help='Show speed/flow time distribution for a single print')
    parser.add_argument(
        '--serve', action='store_true',
        help='Start web dashboard server')
    parser.add_argument(
        '--port', type=int, default=7127,
        help='Port for web dashboard (default: 7127)')
    parser.add_argument(
        '--material', '-m',
        help='Filter by material (PLA, PETG, etc.)')
    parser.add_argument(
        '--log-dir', '-d', default=LOG_DIR,
        help=f'Log directory (default: {LOG_DIR})')
    args = parser.parse_args()

    log_dir = os.path.expanduser(args.log_dir)

    # ------------------------------------------------------------------
    # WEB DASHBOARD MODE
    # ------------------------------------------------------------------
    if args.serve:
        if not os.path.exists(log_dir):
            print(f"Log directory not found: {log_dir}")
            print("Run a print first to generate logs.")
            return 1
        serve_dashboard(args.port, log_dir, material=args.material)
        return 0

    # ------------------------------------------------------------------
    # PRINT-OVER-PRINT TRENDS MODE
    # ------------------------------------------------------------------
    if args.trend:
        if not os.path.exists(log_dir):
            print(f"Log directory not found: {log_dir}")
            return 1

        sessions = find_recent_sessions(log_dir, args.trend, args.material)
        if not sessions:
            print("No print sessions found")
            return 1

        if len(sessions) < 2:
            print("Need at least 2 prints for trend analysis.")
            return 1

        print_trends(sessions)
        return 0

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

    # Z-map mode: show heatmap after the summary
    if args.z_map:
        csv_path = summary_path.replace('_summary.json', '.csv')
        if not os.path.exists(csv_path):
            print(f"CSV log not found: {csv_path}")
            return 1
        bins = analyze_z_banding(csv_path, bin_size=args.z_bin)
        print_z_map(bins, bin_size=args.z_bin)

    # Thermal lag report
    if args.lag:
        csv_path = summary_path.replace('_summary.json', '.csv')
        if not os.path.exists(csv_path):
            print(f"CSV log not found: {csv_path}")
            return 1
        lag_data = analyze_thermal_lag(csv_path, lag_threshold=args.lag_threshold)
        print_thermal_lag_report(lag_data, threshold=args.lag_threshold)

    # Heater headroom analysis
    if args.headroom:
        csv_path = summary_path.replace('_summary.json', '.csv')
        if not os.path.exists(csv_path):
            print(f"CSV log not found: {csv_path}")
            return 1
        headroom = analyze_heater_headroom(csv_path)
        print_headroom_report(headroom)

    # PA stability analysis
    if args.pa_stability:
        csv_path = summary_path.replace('_summary.json', '.csv')
        if not os.path.exists(csv_path):
            print(f"CSV log not found: {csv_path}")
            return 1
        pa_data = analyze_pa_stability(csv_path)
        print_pa_stability_report(pa_data)

    # DynZ zone map
    if args.dynz_map:
        csv_path = summary_path.replace('_summary.json', '.csv')
        if not os.path.exists(csv_path):
            print(f"CSV log not found: {csv_path}")
            return 1
        zones = analyze_dynz_zones(csv_path, bin_size=args.z_bin)
        print_dynz_map(zones, bin_size=args.z_bin)

    # Speed/flow distribution
    if args.distribution:
        csv_path = summary_path.replace('_summary.json', '.csv')
        if not os.path.exists(csv_path):
            print(f"CSV log not found: {csv_path}")
            return 1
        dist = analyze_speed_flow_distribution(csv_path)
        print_distribution(dist)

    return 0


if __name__ == '__main__':
    sys.exit(main())
