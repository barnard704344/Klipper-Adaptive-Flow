#!/usr/bin/env python3
"""
Adaptive Flow Print Analyzer — Extrusion Quality Analysis & Print Stats

Statistical analysis of print logs to identify print quality issues
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

This file is the main entry point.  Analysis logic is split across:
  af_config.py    — config helpers, shared constants, CSV loader
  af_hardware.py  — hardware detection from printer.cfg
  af_slicer.py    — slicer settings parsing and slicer diagnostics
  af_analysis.py  — per-print statistical analysis functions
"""

import os
import sys
import json
import csv
import re
import glob
import math
import time
import logging
import statistics
import argparse
import http.server
import urllib.parse
import urllib.request
import socket
import subprocess
from pathlib import Path
from collections import defaultdict

# ---------------------------------------------------------------------------
# Import shared helpers from sub-modules.  These re-export all public names
# so the rest of this file can reference them without qualification.
# ---------------------------------------------------------------------------
from af_config import (
    LOG_DIR, CONFIG_DIR, GCODES_DIR, _CONFIG_CHANGE_LOG,
    _MATERIAL_VARS,
    _parse_config_variables, _config_paths_for, _get_config_value,
    _format_value, _log_config_change, _load_config_change_log,
    _last_change_for, _count_prints_since, _apply_config_change,
    _suggest_change, load_csv_rows,
)
from af_hardware import (
    _parse_klipper_config, _parse_all_klipper_configs,
    _safe_float, _safe_int, _safe_str,
    collect_printer_hardware,
)
from af_slicer import (
    extract_slicer_settings, _parse_slicer_value, _find_gcode_for_summary,
    analyze_slicer_vs_banding, generate_slicer_profile_advice,
    _E3D_REVO_FLOW, _REVO_HEATER_WATTAGE,
    _get_revo_variant, _get_revo_flow_limit,
    _SLICER_ACCEL_KEYS, _SLICER_SPEED_KEYS, _SLICER_OTHER_KEYS,
    _SLICER_ALL_KEYS, _SLICER_LINE_RE,
)
from af_analysis import (
    _cache_get, _cache_set, _cache_invalidate,
    find_latest_summary, load_summary, print_single_summary,
    find_recent_sessions,
    compute_extrusion_quality,
    analyze_csv_for_banding,
    _diagnose_fix, print_banding_report,
    analyze_z_banding, _bar, print_z_map,
    print_trends,
    analyze_thermal_lag, print_thermal_lag_report,
    analyze_heater_headroom, print_headroom_report,
    analyze_pa_stability, print_pa_stability_report,
    analyze_dynz_zones, print_dynz_map,
    analyze_speed_flow_distribution, print_distribution,
    analyze_boost_optimization,
)

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
    elif max_pwm >= 0.98 and lag_pct <= 5:
        # Hits 100% briefly but keeps up — this is normal PID ramp-up
        recs.append({
            'severity': 'good', 'category': 'Heater',
            'title': 'Heater keeping up well',
            'detail': f'Max PWM hit {max_pwm*100:.0f}% during ramp-up (normal PID behavior) but only {lag_pct:.1f}% lag time \u2014 the heater is reaching target quickly. Avg duty was {avg_pwm*100:.0f}%.',
            'action': 'No changes needed. Brief 100% PWM spikes are normal \u2014 Klipper\u2019s PID briefly goes full power on every temperature transition. What matters is average duty and thermal lag, both of which are healthy.',
        })
    elif max_pwm > 0 and avg_pwm < 0.60:
        recs.append({
            'severity': 'good', 'category': 'Heater',
            'title': 'Plenty of heater headroom',
            'detail': f'Avg PWM was only {avg_pwm*100:.0f}% \u2014 the heater barely broke a sweat.',
            'action': 'You could increase flow_k by 0.1\u20130.3 to get better flow adaptation, or print faster.',
        })
    elif max_pwm > 0 and avg_pwm < 0.85 and lag_pct <= 10:
        # Heater is working but coping fine — not saturated
        recs.append({
            'severity': 'good', 'category': 'Heater',
            'title': 'Heater is healthy',
            'detail': f'Avg PWM was {avg_pwm*100:.0f}% with {lag_pct:.0f}% lag time. Max/P95 hitting 100% in the graphs is normal PID ramp-up, not saturation.',
            'action': 'No changes needed. Your heater is keeping up with flow demand. The "Max PWM 100%" you see in graphs is Klipper\u2019s PID briefly going full-power during temperature transitions \u2014 this is expected and healthy.',
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
            # If the heater is actually coping (low avg PWM, low lag), this is
            # just high-flow brackets seeing brief PID ramp-up — not a problem.
            genuinely_struggling = avg_pwm >= 0.80 and lag_pct > 5
            if genuinely_struggling:
                rec = {
                    'severity': 'warn', 'category': 'Heater',
                    'title': f'Heater struggling above {first} mm\u00b3/s',
                    'detail': f'P95 PWM exceeds 95% in flow brackets: {", ".join(saturated_brackets)}, and avg duty is {avg_pwm*100:.0f}% with {lag_pct:.0f}% thermal lag \u2014 the heater genuinely can\u2019t keep up at these flow rates.',
                    'action': f'Reduce speeds in your slicer to keep flow under {first} mm\u00b3/s, or consider upgrading to a 60W heater.',
                }
                chg = _suggest_change('flow_k', 'reduce', 0.2, material=material, minimum=0.1)
                if chg:
                    rec['config_changes'] = [chg]
                recs.append(rec)
            else:
                # Heater coping fine overall — just informational
                recs.append({
                    'severity': 'info', 'category': 'Heater',
                    'title': f'Heater at capacity above {first} mm\u00b3/s (not a problem)',
                    'detail': f'P95 PWM is high in brackets: {", ".join(saturated_brackets)}. But avg duty is only {avg_pwm*100:.0f}% and thermal lag is {lag_pct:.0f}% \u2014 the heater is keeping up fine overall. Brief 100% spikes at high flow are normal PID behavior, not saturation.',
                    'action': f'No action needed \u2014 your print quality is not affected. A 60W heater would give more margin, but the 40W is handling this workload.',
                })

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
    elif lag_pct > 8:
        rec = {
            'severity': 'warn', 'category': 'Thermal',
            'title': f'Moderate thermal lag ({lag_pct:.0f}% of print)',
            'detail': f'The heater fell behind target for {lag_pct:.0f}% of the print ({len(episodes)} episodes, max {max_lag_val:.1f}\u00b0C). This may affect extrusion consistency.',
            'action': 'Try increasing ramp rate to pre-heat faster.',
        }
        c = _suggest_change('ramp_rate_rise', 'increase', 1.0, minimum=2.0, maximum=8.0)
        if c:
            rec['config_changes'] = [c]
        recs.append(rec)
    elif lag_pct > 3:
        # Minor lag — normal for small heaters, not worth alarming the user
        recs.append({
            'severity': 'info', 'category': 'Thermal',
            'title': f'Minor thermal lag ({lag_pct:.0f}% of print)',
            'detail': f'The heater briefly fell behind target in {len(episodes)} episodes (max {max_lag_val:.1f}\u00b0C). This is typical for smaller heaters and not visible in print quality.',
            'action': 'No action needed. Brief thermal lag during flow spikes is normal, especially with a 40W heater. A 60W heater would reduce this further.',
        })
    elif lag.get('total_samples', 0) > 50 and lag_pct < 1:
        recs.append({
            'severity': 'good', 'category': 'Thermal',
            'title': 'Excellent thermal tracking',
            'detail': f'Heater stayed within target throughout \u2014 only {lag_pct:.1f}% lag time.',
            'action': 'No changes needed. Thermal control is well-tuned.',
        })

    # --- Fan-induced thermal saturation ---
    # Detect when part cooling fan overwhelms the heater: PWM maxes out
    # and temp drops below target specifically when fan speed is high.
    timeline = data.get('timeline') or []
    if timeline:
        sat_fan = []      # fan % during saturation samples
        sat_drops = []     # temp drop (°C) during saturation
        normal_fan = []    # fan % when heater is coping fine
        active_pts = 0

        for pt in timeline:
            pw = pt.get('pw', 0)
            ta = pt.get('ta', 0)
            tt = pt.get('tt', 0)
            fn = pt.get('fn', 0)
            fl = pt.get('f', 0)

            if fl <= 0:
                continue  # skip idle / travel
            active_pts += 1
            temp_drop = tt - ta

            if pw >= 0.95 and temp_drop >= 1.5:
                sat_fan.append(fn)
                sat_drops.append(temp_drop)
            elif pw < 0.85 and temp_drop < 1.0:
                normal_fan.append(fn)

        if len(sat_fan) >= 5 and active_pts > 0:
            avg_sat_fan = statistics.mean(sat_fan)
            avg_normal_fan = statistics.mean(normal_fan) if normal_fan else 0
            max_drop = max(sat_drops)
            avg_drop = statistics.mean(sat_drops)
            sat_pct = len(sat_fan) / active_pts * 100

            # Fan-correlated: saturation happens while fan is delivering
            # significant cooling load.  We don't require fan to be *higher*
            # during saturation vs normal — PLA often runs fan near-constant,
            # and the saturation is caused by the combined fan + flow load.
            # A meaningful fan (>20% actual duty) during saturation means
            # reducing it will give the heater more thermal headroom.
            fan_correlated = avg_sat_fan > 20

            if fan_correlated:
                severity = 'bad' if max_drop > 2.5 or sat_pct > 5 else 'warn'

                rec = {
                    'severity': severity,
                    'category': 'Thermal',
                    'title': 'Part cooling fan is overwhelming the heater',
                    'detail': (
                        f'When the part fan exceeds ~{avg_sat_fan:.0f}%, the heater '
                        f'maxes out (PWM \u226595%) and temperature drops up to '
                        f'{max_drop:.1f}\u00b0C below target. This happened in '
                        f'{len(sat_fan)} samples ({sat_pct:.1f}% of print). '
                        f'Average fan during saturation: {avg_sat_fan:.0f}% vs '
                        f'{avg_normal_fan:.0f}% when the heater is coping. '
                        f'The heater cannot deliver enough power to compensate '
                        f'for fan cooling at high flow rates \u2014 this causes '
                        f'under-extrusion banding visible on walls.'
                    ),
                    'action': (
                        f'Reduce fan speed in your slicer for {material or "this material"} '
                        f'to limit part cooling to what the heater can sustain. '
                        f'Also verify PID was tuned with the fan running '
                        f'(run M106 S255 before PID_CALIBRATE).'
                    ),
                }
                recs.append(rec)

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
    # Use a 0.001 deadband so borderline values (e.g. 0.0455 displayed as 0.045)
    # don't trigger a confusing warning that looks like 0.045 > 0.045.
    if pa_val and pa_val > pa_typical_max + 0.001 and pa.get('samples', 0) > 50:
        suggested_pa = round(pa_typical_max - 0.005, 4)
        rec = {
            'severity': 'warn', 'category': 'Pressure Advance',
            'title': f'PA value ({pa_val:.4f}) may be too high for {material or "this material"}',
            'detail': (
                f'Your Pressure Advance is {pa_val:.4f}, which is above the typical '
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

    # --- Extrusion Quality Score (replaces old "banding risk" event count) ---
    eq = data.get('extrusion_quality') or {}
    eq_score = eq.get('score')
    eq_detail = eq.get('detail') or {}

    if eq_score is not None:
        # --- Overall quality ---
        if eq_score >= 85:
            recs.append({
                'severity': 'good', 'category': 'Quality',
                'title': f'Extrusion quality score: {eq_score}/100',
                'detail': (
                    f'Thermal stability {eq.get("thermal", 0)}/100, '
                    f'flow steadiness {eq.get("flow", 0)}/100, '
                    f'heater reserve {eq.get("heater", 0)}/100, '
                    f'pressure stability {eq.get("pressure", 0)}/100.'
                ),
                'action': 'Excellent extrusion consistency — no changes needed.',
            })
        elif eq_score >= 65:
            # Find weakest sub-score
            subs = {'Thermal stability': eq.get('thermal', 100),
                     'Flow steadiness': eq.get('flow', 100),
                     'Heater reserve': eq.get('heater', 100),
                     'Pressure stability': eq.get('pressure', 100)}
            weakest = min(subs, key=subs.get)
            recs.append({
                'severity': 'info', 'category': 'Quality',
                'title': f'Extrusion quality score: {eq_score}/100',
                'detail': (
                    f'Thermal stability {eq.get("thermal", 0)}/100, '
                    f'flow steadiness {eq.get("flow", 0)}/100, '
                    f'heater reserve {eq.get("heater", 0)}/100, '
                    f'pressure stability {eq.get("pressure", 0)}/100. '
                    f'Weakest area: {weakest} ({subs[weakest]}/100).'
                ),
                'action': f'Acceptable quality. {weakest} is the main area for improvement — see specific recommendations below.',
            })
        else:
            subs = {'Thermal stability': eq.get('thermal', 100),
                     'Flow steadiness': eq.get('flow', 100),
                     'Heater reserve': eq.get('heater', 100),
                     'Pressure stability': eq.get('pressure', 100)}
            weakest = min(subs, key=subs.get)
            recs.append({
                'severity': 'warn' if eq_score >= 45 else 'bad',
                'category': 'Quality',
                'title': f'Extrusion quality score: {eq_score}/100',
                'detail': (
                    f'Thermal stability {eq.get("thermal", 0)}/100, '
                    f'flow steadiness {eq.get("flow", 0)}/100, '
                    f'heater reserve {eq.get("heater", 0)}/100, '
                    f'pressure stability {eq.get("pressure", 0)}/100. '
                    f'Weakest area: {weakest} ({subs[weakest]}/100).'
                ),
                'action': f'Print quality is likely affected. Focus on {weakest} — see recommendations below.',
            })

        # --- Specific sub-score recommendations ---
        # Thermal stability
        ts = eq.get('thermal', 100)
        if ts < 60:
            rec = {
                'severity': 'warn' if ts >= 40 else 'bad',
                'category': 'Quality',
                'title': f'Thermal stability: {ts}/100 — temp off-target {100 - eq_detail.get("temp_in_band_pct", 100):.0f}% of extrusion time',
                'detail': (
                    f'Only {eq_detail.get("temp_in_band_pct", 0):.0f}% of extrusion time was within ±1°C of target '
                    f'(avg deviation {eq_detail.get("avg_temp_dev", 0):.1f}°C, max {eq_detail.get("max_temp_dev", 0):.1f}°C). '
                    f'Temperature deviation directly changes melt viscosity — '
                    f'each °C off target alters extrusion width, causing visible banding on walls.'
                ),
                'action': 'Re-tune PID with fan running (M106 S255 before PID_CALIBRATE). Reduce slicer fan speed if using high fan speeds.',
            }
            changes = []
            c = _suggest_change('flow_smoothing', 'increase', 0.1, minimum=0.3, maximum=0.8)
            if c:
                changes.append(c)
            if changes:
                rec['config_changes'] = changes
            recs.append(rec)

        # Flow steadiness
        fs = eq.get('flow', 100)
        if fs < 60:
            bjumps = eq_detail.get('big_jumps', 0)
            jitter = eq_detail.get('flow_jitter', 0)
            rec = {
                'severity': 'warn' if fs >= 40 else 'bad',
                'category': 'Quality',
                'title': f'Flow steadiness: {fs}/100 — {bjumps} large flow jumps (>{2} mm³/s)',
                'detail': (
                    f'Flow jitter index: {jitter:.3f} (ideal <0.05). '
                    f'{eq_detail.get("big_jump_pct", 0):.1f}% of samples had large flow changes. '
                    f'Average flow delta: {eq_detail.get("avg_flow_delta", 0):.1f} mm³/s at mean flow {eq_detail.get("mean_flow", 0):.1f} mm³/s. '
                    f'Large flow rate changes cause pressure transients in the melt zone that '
                    f'PA cannot fully compensate — each one leaves a mark on walls.'
                ),
                'action': 'Increase flow_smoothing to dampen flow spikes. In slicer, unify speeds for walls/infill to reduce abrupt flow changes.',
            }
            c = _suggest_change('flow_smoothing', 'increase', 0.15, minimum=0.3, maximum=0.8)
            if c:
                rec['config_changes'] = [c]
            recs.append(rec)

        # Heater reserve
        hs = eq.get('heater', 100)
        if hs < 60:
            rec = {
                'severity': 'warn' if hs >= 40 else 'bad',
                'category': 'Quality',
                'title': f'Heater reserve: {hs}/100 — PWM saturated {eq_detail.get("pwm_saturated_pct", 0):.0f}% of extrusion time',
                'detail': (
                    f'Heater was at ≥95% PWM for {eq_detail.get("pwm_saturated_pct", 0):.1f}% of active extrusion '
                    f'(avg PWM {eq_detail.get("avg_pwm", 0) * 100:.0f}%). '
                    f'When the heater has no reserve, it cannot respond to temperature demand '
                    f'changes from flow variation or fan cooling — temp drops below target '
                    f'and extrusion becomes inconsistent.'
                ),
                'action': 'Reduce fan speed in slicer to give the heater thermal headroom. Verify PID was tuned with fan on. Consider a 60W heater upgrade.',
            }
            changes = []
            c = _suggest_change('flow_k', 'reduce', 0.2, material=material, minimum=0.1)
            if c:
                changes.append(c)
            if changes:
                rec['config_changes'] = changes
            recs.append(rec)

        # Pressure stability
        ps = eq.get('pressure', 100)
        if ps < 60:
            rec = {
                'severity': 'warn' if ps >= 40 else 'bad',
                'category': 'Quality',
                'title': f'Pressure stability: {ps}/100 — {eq_detail.get("transient_count", 0)} accel-induced transients',
                'detail': (
                    f'{eq_detail.get("transient_pct", 0):.1f}% of samples had significant '
                    f'acceleration changes during high-flow extrusion (avg impact: '
                    f'{eq_detail.get("avg_transient_impact", 0):.2f}). '
                    f'Large acceleration changes at high flow create pressure waves in the '
                    f'melt zone that Pressure Advance cannot fully absorb — each transition '
                    f'shows as a faint line on the print surface.'
                ),
                'action': 'In slicer, unify acceleration values for all print moves (walls, infill, top surface) to the Y-axis shaper limit. Only travel should use a higher accel.',
            }
            c = _suggest_change('flow_smoothing', 'increase', 0.1, minimum=0.3, maximum=0.7)
            if c:
                rec['config_changes'] = [c]
            recs.append(rec)

    # --- Slicer-specific recommendations (from gcode analysis) ---
    slicer_diag = data.get('slicer_diagnosis') or {}
    slicer_issues = slicer_diag.get('issues', [])
    slicer_suggestions = slicer_diag.get('suggestions', [])

    if slicer_issues:
        # Build a combined detail from all issues
        issue_details = ' '.join(iss['detail'] for iss in slicer_issues)
        suggestion_lines = []
        for sg in slicer_suggestions:
            suggestion_lines.append(
                f"\u2022 {sg['setting'].replace('_', ' ').title()}: "
                f"{sg['current']} \u2192 {sg['suggested']} ({sg['reason']})"
            )
        action_text = 'In your slicer, change:\n' + '\n'.join(suggestion_lines) if suggestion_lines else (
            'Review your slicer acceleration settings to reduce the spread of values.'
        )

        # Severity depends on extrusion quality score
        _ps = eq.get('pressure', 100) if eq else 100
        if _ps < 50:
            sev = 'warn'
        elif _ps < 75:
            sev = 'info'
        else:
            sev = 'info'

        recs.append({
            'severity': sev, 'category': 'Slicer',
            'title': f'{len(slicer_issues)} slicer setting issue{"s" if len(slicer_issues) != 1 else ""} found',
            'detail': issue_details,
            'action': action_text,
        })
    elif data.get('slicer_settings') and (eq_score is None or eq_score >= 75):
        # We have slicer data but no issues — good news
        distinct = len(slicer_diag.get('distinct_accels', []))
        if distinct > 0:
            recs.append({
                'severity': 'good', 'category': 'Slicer',
                'title': 'Slicer settings look good',
                'detail': f'Parsed {len(data["slicer_settings"])} settings from gcode. {distinct} distinct accel values observed — no problematic gaps found.',
                'action': 'No slicer changes needed.',
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

        # --- Quality score trend ---
        eq_vals = [t.get('eq_score') for t in recent]
        eq_valid = [v for v in eq_vals if v is not None]
        if len(eq_valid) >= 3:
            if all(eq_valid[i] > eq_valid[i + 1] for i in range(len(eq_valid) - 1)):
                drop = eq_valid[0] - eq_valid[-1]
                if drop > 10:
                    recs.append({
                        'severity': 'warn', 'category': 'Trend',
                        'title': f'Quality declining ({eq_valid[0]} \u2192 {eq_valid[-1]} over 3 {mat_label} prints)',
                        'detail': 'Extrusion quality score has dropped each print. Something is degrading \u2014 nozzle wear, partial clog, or a config change made things worse.',
                        'action': 'Compare what changed between prints. If nothing was changed, inspect nozzle for wear or partial blockage. Cold pull recommended.',
                    })
            elif all(eq_valid[i] < eq_valid[i + 1] for i in range(len(eq_valid) - 1)):
                gain = eq_valid[-1] - eq_valid[0]
                if gain > 5:
                    recs.append({
                        'severity': 'good', 'category': 'Trend',
                        'title': f'Quality improving ({eq_valid[0]} \u2192 {eq_valid[-1]} across {mat_label})',
                        'detail': 'Extrusion quality score is climbing over recent prints \u2014 your changes are working.',
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

    elif len(same_mat) >= 2:
        # With just 2 same-material prints, check for a big quality change
        prev, curr = same_mat[-2], same_mat[-1]
        eq_prev = prev.get('eq_score')
        eq_curr = curr.get('eq_score')
        if eq_prev is not None and eq_curr is not None:
            eq_drop = eq_prev - eq_curr
            if eq_drop > 15:
                recs.append({
                    'severity': 'warn', 'category': 'Trend',
                    'title': f'Quality dropped {eq_drop} points since last print',
                    'detail': f'Previous: {eq_prev}/100, now: {eq_curr}/100.',
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

    # --- Hardware-aware recommendations ---
    printer_hw = data.get('printer_hw') or {}

    # Fan cap warning
    fan_hw = printer_hw.get('part_fan', {})
    fan_max = fan_hw.get('max_power', 1.0)
    if fan_max < 1.0:
        pct = int(fan_max * 100)
        recs.append({
            'severity': 'info', 'category': 'Hardware',
            'title': f'Part cooling fan hardware-limited to {pct}%',
            'detail': f'Your [fan] config has max_power: {fan_max}. The fan can never exceed {pct}%. '
                       f'If this is intentional (e.g. powerful CPAP fan), no action is needed.',
            'action': f'If this cap is unintentional, raise max_power in your [fan] section.',
        })

    # Firmware max_accel vs slicer
    fw_max_accel = printer_hw.get('firmware_max_accel')
    slicer = data.get('slicer_settings') or {}
    if fw_max_accel and slicer:
        for key in ('default_acceleration', 'outer_wall_acceleration', 'inner_wall_acceleration',
                    'sparse_infill_acceleration', 'travel_acceleration'):
            val = slicer.get(key)
            if val and isinstance(val, (int, float)) and val > fw_max_accel:
                recs.append({
                    'severity': 'warn', 'category': 'Hardware',
                    'title': f'Slicer {key.replace("_", " ")} exceeds firmware limit',
                    'detail': f'{key} is {int(val)} in slicer but firmware max_accel is {fw_max_accel}. '
                               f'Klipper silently clamps to {fw_max_accel}.',
                    'action': f'Either raise max_accel in printer.cfg or lower the slicer setting.',
                })
                break  # one warning is enough

    # Input shaper quality limit
    is_data = printer_hw.get('input_shaper', {})
    if is_data and slicer:
        shaper_limits = {}
        for axis in ('x', 'y'):
            rec_max = (is_data.get(axis) or {}).get('recommended_max_accel')
            if rec_max:
                shaper_limits[axis] = rec_max
        if shaper_limits:
            min_axis = min(shaper_limits, key=shaper_limits.get)
            min_limit = shaper_limits[min_axis]
            shaper_info = is_data.get(min_axis, {})
            wall_accel = slicer.get('outer_wall_acceleration') or slicer.get('inner_wall_acceleration')
            if wall_accel and isinstance(wall_accel, (int, float)) and wall_accel > min_limit:
                recs.append({
                    'severity': 'warn', 'category': 'Hardware',
                    'title': f'Wall accel exceeds input shaper limit ({min_axis.upper()})',
                    'detail': f'Wall accel {int(wall_accel)} exceeds the quality limit of '
                               f'{min_limit} for {shaper_info.get("type", "?").upper()} @ '
                               f'{shaper_info.get("freq", "?")}Hz on {min_axis.upper()} axis. '
                               f'This may cause visible ringing.',
                    'action': f'Reduce wall accel to \u2264{min_limit} or re-tune input shaper for a higher frequency.',
                })

    # Bowden vs direct drive detection
    ext_hw = printer_hw.get('extruder', {})
    drive_type = ext_hw.get('drive_type', 'unknown')
    if drive_type == 'bowden':
        recs.append({
            'severity': 'info', 'category': 'Hardware',
            'title': 'Bowden extruder detected',
            'detail': f'Rotation distance {ext_hw.get("rotation_distance")} indicates a Bowden setup. '
                       f'PA values and retraction distances differ significantly from direct drive.',
            'action': 'Use PA values in the 0.4\u20131.0 range (vs 0.02\u20130.06 for direct drive). '
                       'Set retraction to 3\u20136mm (vs 0.5\u20131.5mm).',
        })

    # Quad gantry context for Z banding
    z_count = printer_hw.get('z_steppers', 0)
    if z_count >= 4:
        zb = data.get('z_banding') or {}
        if zb:
            recs.append({
                'severity': 'info', 'category': 'Hardware',
                'title': f'Quad gantry ({z_count} Z steppers) detected',
                'detail': 'Quad gantry leveling (QGL) can introduce periodic Z artifacts if not well calibrated. '
                           'Z banding at regular intervals may be QGL-related rather than slicer/flow.',
                'action': 'Run QGL before prints. If Z banding persists at regular intervals, check lead screw alignment.',
            })

    # MMU presence
    if printer_hw.get('mmu_present'):
        recs.append({
            'severity': 'info', 'category': 'Hardware',
            'title': 'MMU (multi-material) detected',
            'detail': 'Happy Hare MMU config found. Multi-material prints need tip-shaping and purge settings.',
            'action': 'Ensure your slicer has tip-shaping/ramming and purge tower configured for multi-material prints.',
        })

    # --- Printer performance utilization ---
    kinematics = printer_hw.get('kinematics', 'unknown')
    is_fast_machine = kinematics in ('corexy', 'corexz')
    hotend_data = data.get('hotend_info') or {}
    safe_flow_val = hotend_data.get('safe_flow', 0)
    avg_flow_val = s.get('avg_flow', 0)
    max_flow_val = s.get('max_flow', 0)

    if is_fast_machine and safe_flow_val > 0 and avg_flow_val > 0:
        avg_utilization = avg_flow_val / safe_flow_val * 100
        peak_utilization = max_flow_val / safe_flow_val * 100 if max_flow_val else avg_utilization

        # Speed/utilization recommendations belong in the Slicer Profile tab
        # which already has per-setting details with proper context.
        pass

    # --- Boost optimization insights (from actual print data) ---
    bopt = data.get('boost_optimization')
    if bopt and bopt.get('verdict'):
        v = bopt['verdict']
        increase = bopt.get('speed_increase_pct', 0)
        suggestions = bopt.get('suggestions', [])
        can_increase = bopt.get('can_increase', [])

        if v == 'significant_headroom' and increase >= 25:
            rec = {
                'severity': 'info', 'category': 'Optimization',
                'title': f'Room to go ~{increase}% faster (based on actual print data)',
                'detail': bopt['verdict_text'],
                'action': 'Check the Slicer tab for per-setting speed suggestions based on this data.',
            }
            # If flow_k increase is suggested, add config change
            flow_k_sug = [sg for sg in suggestions if sg.get('config_var') == 'flow_k']
            if flow_k_sug:
                chg = _suggest_change('flow_k', 'increase', 0.15, material=material, maximum=2.5)
                if chg:
                    rec['config_changes'] = [chg]
            recs.append(rec)

        elif v == 'moderate_headroom' and increase >= 10:
            recs.append({
                'severity': 'info', 'category': 'Optimization',
                'title': f'Moderate room to optimize (~{increase}% headroom)',
                'detail': bopt['verdict_text'],
                'action': 'Check the Slicer tab for per-setting speed suggestions based on this data.',
            })

        elif v == 'at_limit':
            limiting = bopt.get('limiting_factors', [])
            recs.append({
                'severity': 'good', 'category': 'Optimization',
                'title': 'Speeds well-matched to hardware',
                'detail': f'{bopt["verdict_text"]} Limiting factor(s): {", ".join(limiting)}.',
                'action': 'Current settings are a good match for your hardware. '
                          'To go faster, you would need to upgrade the limiting component.',
            })

        elif v == 'well_tuned':
            recs.append({
                'severity': 'good', 'category': 'Optimization',
                'title': 'Print speeds are well-tuned',
                'detail': bopt['verdict_text'],
                'action': 'No speed changes needed — the printer is close to optimal.',
            })

    # --- All good ---
    if not recs or all(r['severity'] == 'good' for r in recs):
        recs.append({
            'severity': 'good', 'category': 'Overall',
            'title': 'Print looks well-tuned',
            'detail': 'No significant issues detected across heater, thermal lag, PA, and banding analysis.',
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


def collect_dashboard_data(log_dir, summary_path=None, material=None):
    """Gather all analysis data for the web dashboard."""
    data = {
        'summary': None, 'timeline': [], 'z_banding': {},
        'thermal_lag': None, 'headroom': None, 'pa_stability': None,
        'dynz_zones': {}, 'speed_flow': None, 'trends': None,
        'sessions': [], 'selected_file': '', 'is_live': False,
    }

    all_sessions = find_recent_sessions(log_dir, count=50, material=material)

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

    # --- Extrusion Quality Score (physics-based, replaces banding risk) ---
    data['extrusion_quality'] = compute_extrusion_quality(data['timeline'])

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
                'eq_score': None,  # filled below
            })

        # Compute extrusion quality score for each trend print (lightweight)
        for i, s in enumerate(reversed(all_sessions[:10])):
            csv_f = s.get('csv_file', '')
            if csv_f and os.path.exists(csv_f) and i < 5:  # cap to 5 newest
                try:
                    tl = read_csv_timeline(csv_f, max_points=400)
                    eq = compute_extrusion_quality(tl)
                    if eq:
                        trend_data[i]['eq_score'] = eq['score']
                except Exception:
                    pass

        data['trends'] = trend_data

    # === Slicer settings extraction & cross-reference with banding ===
    slicer = None
    slicer_diag = None
    if not data.get('is_live'):
        summary = data.get('summary') or {}
        gcode_path = _find_gcode_for_summary(summary)
        if gcode_path:
            slicer = extract_slicer_settings(gcode_path)
        if slicer:
            # Extract raw accel values from already-loaded CSV rows
            csv_accels = []
            for row in (csv_rows or []):
                try:
                    a = int(row.get('accel', 0))
                    if a > 0:
                        csv_accels.append(a)
                except (ValueError, TypeError):
                    pass
            # Run banding analysis for event data
            banding_csv = analyze_csv_for_banding(csv_path)
            slicer_diag = analyze_slicer_vs_banding(slicer, banding_csv, csv_accels)
    data['slicer_settings'] = slicer
    data['slicer_diagnosis'] = slicer_diag

    # --- Auto-detect printer hardware from config files ---
    printer_hw = collect_printer_hardware(CONFIG_DIR)
    data['printer_hw'] = printer_hw

    # --- Build hotend info from adaptive flow config ---
    hotend_info = None
    try:
        af_cfg = _parse_config_variables(os.path.join(CONFIG_DIR, 'auto_flow_user.cfg'))
        af_defaults = _parse_config_variables(os.path.join(CONFIG_DIR, 'auto_flow_defaults.cfg'))
        core_section = 'gcode_macro _AUTO_TEMP_CORE'
        def _af_val(key):
            vk = f'variable_{key}'
            for cfg in (af_cfg, af_defaults):
                v = cfg.get(core_section, {}).get(vk)
                if v is not None:
                    try:
                        sv = str(v).split('#')[0].strip()
                        if sv.lower() in ('true', 'false'):
                            return sv.lower() == 'true'
                        return float(sv)
                    except ValueError:
                        return v
            return None
        is_hf = _af_val('use_high_flow_nozzle')
        if is_hf is None:
            is_hf = True
        nozzle_type = 'HF' if is_hf else 'SF'
        max_safe = _af_val(f'max_safe_flow_{"hf" if is_hf else "std"}')
        if max_safe is None:
            max_safe = 25.0 if is_hf else 15.0
        wattage = _af_val('heater_wattage')
        if wattage is None:
            wattage = 40
        if isinstance(wattage, str):
            wattage = wattage.split('#')[0].strip()
            try:
                wattage = float(wattage)
            except ValueError:
                wattage = 40
        hotend_info = {
            'nozzle_type': nozzle_type,
            'max_safe_flow': float(max_safe),
            'heater_wattage': int(wattage),
        }
        # --- Overlay E3D Revo reference data ---
        # Use hardware-detected nozzle diameter as source of truth
        nozzle_dia = 0.4
        hw_nozzle = (printer_hw.get('extruder') or {}).get('nozzle_diameter')
        if hw_nozzle and isinstance(hw_nozzle, (int, float)):
            nozzle_dia = hw_nozzle
        elif slicer:
            nozzle_dia = slicer.get('nozzle_diameter', 0.4)
            if not isinstance(nozzle_dia, (int, float)):
                nozzle_dia = 0.4
        material = 'PLA'
        if data.get('summary'):
            material = (data['summary'].get('material') or 'PLA').strip().upper()
        e3d_limits = _get_revo_flow_limit(nozzle_dia, nozzle_type, material)
        hotend_info['safe_flow'] = e3d_limits['safe']
        hotend_info['peak_flow'] = e3d_limits['peak']
        hotend_info['nozzle_diameter'] = nozzle_dia
        hotend_info['material'] = material
        hotend_info['flow_source'] = 'E3D Revo published data'
    except Exception:
        pass
    data['hotend_info'] = hotend_info

    # --- Boost optimization analysis — "can I go faster?" ---
    # Computed BEFORE profile advice so its data-backed speed_increase_pct
    # can be passed to the per-setting speed recommendations, ensuring both
    # sections suggest the same target speeds.
    boost_opt = None
    if csv_path and hotend_info:
        boost_opt = analyze_boost_optimization(
            csv_path,
            summary=data.get('summary'),
            hotend_info=hotend_info,
            printer_hw=printer_hw,
            slicer_settings=slicer,
            rows=csv_rows,
        )
    data['boost_optimization'] = boost_opt

    # --- Comprehensive per-setting profile advice ---
    _boost_speed_pct = None
    if boost_opt:
        _boost_speed_pct = boost_opt.get('speed_increase_pct')
    profile_advice = None
    if slicer and hotend_info:
        profile_advice = generate_slicer_profile_advice(
            slicer, hotend_info,
            print_summary=data.get('summary'),
            printer_hw=printer_hw,
            boost_speed_increase_pct=_boost_speed_pct,
        )
    data['slicer_profile_advice'] = profile_advice

    data['vibration'] = None
    data['vibration_banding'] = []

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
.fly-tip{position:fixed;background:#1c2128;color:#c9d1d9;border:1px solid #30363d;
border-radius:6px;padding:8px 12px;font-size:11px;line-height:1.4;white-space:normal;
width:260px;z-index:99999;text-transform:none;letter-spacing:0;font-weight:400;
box-shadow:0 4px 12px rgba(0,0,0,.4);pointer-events:none;
transition:opacity .15s;opacity:0}
.fly-tip.show{opacity:1}
.fly-tip::after{content:'';position:absolute;border:5px solid transparent}
.tabs{display:flex;gap:0;padding:0 24px;border-bottom:1px solid #30363d;
background:#161b22;overflow-x:auto}
.tab{padding:10px 16px;font-size:13px;color:#8b949e;cursor:pointer;
border-bottom:2px solid transparent;white-space:nowrap;transition:all .2s}
.tab:hover{color:#c9d1d9}
.tab.active{color:#58a6ff;border-bottom-color:#58a6ff}
.area{padding:16px 24px}
.box{background:#161b22;border:1px solid #30363d;border-radius:8px;
padding:16px;margin-bottom:16px}
.box h3{font-size:14px;margin-bottom:4px;color:#8b949e}
.box-desc{font-size:11px;color:#484f58;margin-bottom:10px;line-height:1.4}
.box-desc .good{color:#3fb950}.box-desc .warn{color:#d29922}.box-desc .bad{color:#f85149}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.tab-wrap{position:relative;display:inline-flex;align-items:center;gap:4px}
.tab-tip{display:inline-flex;align-items:center;justify-content:center;
width:13px;height:13px;border-radius:50%;background:#30363d;color:#484f58;
font-size:8px;cursor:help;flex-shrink:0}
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
.sl-hdr{display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}
.sl-mat{display:inline-flex;align-items:center;gap:6px;background:#238636;color:#fff;
font-weight:600;font-size:14px;padding:6px 16px;border-radius:20px;letter-spacing:.3px}
.sl-mat .sl-icon{font-size:16px}
.sl-file{font-size:12px;color:#8b949e;word-break:break-all}
.pa-table{width:100%;border-collapse:collapse}
.pa-table th{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;
padding:8px 10px;text-align:left;border-bottom:1px solid #30363d}
.pa-table td{padding:8px 10px;border-bottom:1px solid #21262d;vertical-align:top;font-size:13px}
.pa-table tr:last-child td{border-bottom:none}
.pa-table td:first-child{text-transform:capitalize}
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

@media(max-width:768px){.row2{grid-template-columns:1fr}
.cards{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<div class="hdr">
<h1>\u26a1 Adaptive Flow Dashboard</h1>
<div class="ctrls">
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
// --- Fly-out tooltip system ---
var _tip=document.createElement('div');_tip.className='fly-tip';document.body.appendChild(_tip);
var _tipT=null;
function showTip(el){
var t=el.getAttribute('data-tip');if(!t)return;
_tip.textContent=t;
var r=el.getBoundingClientRect();
var above=r.top>180;
_tip.style.left='0';_tip.style.top='0';_tip.classList.add('show');
var tw=_tip.offsetWidth,th=_tip.offsetHeight;
var lx=r.left+r.width/2-tw/2;
if(lx<4)lx=4;if(lx+tw>window.innerWidth-4)lx=window.innerWidth-tw-4;
if(above){_tip.style.top=(r.top-th-8)+'px';_tip.style.setProperty('--arr','bottom');
_tip.style.cssText+='';}
else{_tip.style.top=(r.bottom+8)+'px';}
_tip.style.left=lx+'px';
}
function hideTip(){_tip.classList.remove('show');}
document.addEventListener('mouseover',function(e){
var el=e.target.closest('[data-tip]');if(el){clearTimeout(_tipT);showTip(el);}});
document.addEventListener('mouseout',function(e){
var el=e.target.closest('[data-tip]');if(el){_tipT=setTimeout(hideTip,80);}});
var isLive=D.is_live||false;

var sel=document.getElementById('ss');
var lvi=document.getElementById('lv');
var ftel=document.getElementById('ft');

// Show LIVE indicator
if(isLive)lvi.style.display='inline';

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
ftel.textContent='Adaptive Flow Dashboard'}

function pollData(){
var url='/api/data';
var cur=sel.value;
if(cur&&cur!=='__live__')url+='?session='+encodeURIComponent(cur);
fetch(url).then(function(r){return r.json()}).then(function(nd){
D=nd;isLive=D.is_live||false;
if(isLive)lvi.style.display='inline'; else lvi.style.display='none';
rc();rCh();
}).catch(function(){})}

function rc(){var c=document.getElementById('cds'),s=D.summary;
if(!s){c.innerHTML='<div class="cd"><div class="vl d">No data</div></div>';return}
var eq=D.extrusion_quality||{},dp=s.dynz_active_pct||0;
var liveBadge=s._live?'<span style="color:#3fb950;font-size:11px"> \u25cf PRINTING</span>':'';
var items;
{
var qs=eq.score!=null?eq.score:null;
var qc=qs!=null?(qs>=80?'#3fb950':qs>=60?'#d29922':'#f85149'):'#8b949e';
var qSub=qs!=null?'T:'+eq.thermal+' F:'+eq.flow+' H:'+eq.heater+' P:'+eq.pressure:'no data';
items=[
{l:'Material',v:(s.material||'?')+liveBadge,s:(s.duration_min||0).toFixed(1)+' min'+(s._live?' elapsed':''),
d:'Active material profile and total print duration.'},
{l:'Temp Boost',v:(s.avg_boost||0).toFixed(1)+'\u00b0C',s:'max '+(s.max_boost||0).toFixed(1)+'\u00b0C',
d:'Extra temperature added above base to meet flow demand. \u2022 0\u201310\u00b0C = light load (good) \u2022 10\u201325\u00b0C = moderate \u2022 25\u00b0C+ = heavy load, check if heater can keep up'},
{l:'Heater Duty',v:((s.avg_pwm||0)*100).toFixed(0)+'%',s:'max '+((s.max_pwm||0)*100).toFixed(0)+'%',w:(s.avg_pwm||0)>0.85,
d:'Average heater power. Max hitting 100% is normal during temp ramps (PID behavior). \u2022 Avg under 60% = lots of headroom (good) \u2022 60\u201380% = healthy \u2022 80%+ avg with thermal lag = heater struggling'},
{l:'DynZ',v:dp>0?dp+'%':'Off',s:dp>0?'min accel '+(s.accel_min||0):'inactive',
d:'% of layers where accel was reduced for tricky geometry. \u2022 0% = simple print, no intervention needed (good) \u2022 1\u201315% = normal for curves/overhangs \u2022 15%+ = very complex geometry'},
{l:'Quality',v:qs!=null?'<span style="color:'+qc+'">'+qs+'/100</span>':'\u2014',s:qSub,w:qs!=null&&qs<60,
d:'Extrusion quality score based on 4 physics metrics. \u2022 T = Thermal stability (temp on target) \u2022 F = Flow steadiness (flow jitter) \u2022 H = Heater reserve (PWM headroom) \u2022 P = Pressure stability (accel transients). \u2022 85+ = excellent \u2022 65\u201384 = acceptable \u2022 <65 = likely visible issues'}];
c.innerHTML=items.map(function(x){return '<div class="cd"><div class="lb">'+
x.l+(x.d?'<span class="tip" data-tip="'+x.d+'">?</span>':'')+
'</div><div class="vl'+(x.w?' w':'')+'">'+x.v+
'</div><div class="sb">'+x.s+'</div></div>'}).join('')}}
rc();

var allTabs=[
{id:'sl',l:'\u2702 Slicer',tip:'Shows slicer settings extracted from your G-code file. Cross-references acceleration values with banding data to identify specific settings causing issues.'},
{id:'tl',l:'Timeline',tip:'Real-time temperature, flow rate and speed plotted over the entire print. See how your heater responds to flow demands.'},
{id:'zh',l:'Z-Height',tip:'Shows which layers had the most thermal stress. Tall bars = layers where banding is most likely.'},
{id:'ht',l:'Heater',tip:'Is your heater keeping up? Shows power usage at different flow rates. Bars near 100% mean the heater is maxed out.'},
{id:'pa',l:'PA',tip:'Pressure Advance value over time. A flat line means stable extrusion. Wobbling means the system is hunting.'},
{id:'dz',l:'DynZ',tip:'Dynamic Z-offset adjustments for first layers and overhangs. Shows where acceleration was reduced to protect quality.'},
{id:'ds',l:'Distribution',tip:'How your print spent its time across different speeds and flow rates. Helps identify if you are pushing too hard.'}];
var at='sl',tb=document.getElementById('tb'),ca=document.getElementById('ca');
function buildTabs(){
var tabs=allTabs;

tb.innerHTML=tabs.map(function(t){
return '<div class="tab'+(t.id===at?' active':'')+
'" onclick="sTab(this.dataset.t)" data-t="'+t.id+'"><span class="tab-wrap">'+t.l+
(t.tip?'<span class="tab-tip" data-tip="'+t.tip+'">?</span>':'')+'</span></div>'}).join('')}
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
if(at==='sl')rSlicer();
else if(at==='tl')rTimeline(tl);
else if(at==='zh')rZH();
else if(at==='ht')rHt();
else if(at==='pa')rPA(tl);
else if(at==='dz')rDZ();
else if(at==='ds')rDist()}

function rSlicer(){
if(isLive&&!D.slicer_settings){
ca.innerHTML='<div class="box" style="text-align:center;padding:32px 16px">'+
'<div style="font-size:28px;margin-bottom:12px">\u2702\ufe0f</div>'+
'<div style="font-size:15px;font-weight:600;color:#c9d1d9;margin-bottom:8px">Slicer analysis available after the print finishes</div>'+
'<div style="font-size:12px;color:#8b949e;max-width:440px;margin:0 auto;line-height:1.5">'+
'Slicer settings are read from the end of the G-code file once the print completes. '+
'Check back when the print is done for a full profile breakdown and recommendations.</div></div>';
return}
var ss=D.slicer_settings;
if(!ss){
ca.innerHTML='<div class="box"><p>No slicer settings found. The G-code file may have been deleted, or this slicer does not embed settings in the footer.</p>'+
'<p style="color:#484f58;font-size:12px;margin-top:8px">Supported: OrcaSlicer, BambuStudio, PrusaSlicer, SuperSlicer</p></div>';
return}

var h='';
var mat=(D.summary||{}).material||'Unknown';
var fname=(D.summary||{}).filename||'';
var pa=D.slicer_profile_advice||[];
var hi=D.hotend_info;

/* --- Build lookup from profile advice keyed by setting name --- */
var advMap={};
pa.forEach(function(a){advMap[a.setting]=a});

/* Merge boost optimization per-setting suggestions into advMap */
var bo=D.boost_optimization;
if(bo&&bo.suggestions){
bo.suggestions.forEach(function(sg){
if(sg.per_setting){
sg.per_setting.forEach(function(ps){
var a=advMap[ps.key];
if(a&&!a.suggestion){
a.suggestion=ps.suggested+' mm/s';
a.verdict='warn';
a.reason='\ud83d\ude80 Boost data: increase to '+ps.suggested+' mm/s. '+a.reason}
else if(a&&a.suggestion){
a.reason='\ud83d\ude80 Boost data agrees: '+ps.suggested+' mm/s. '+a.reason}
})}})}

/* --- Unified printer/hotend card --- */
h+='<div class="box" style="padding:14px 16px">';

/* Hotend info row */
if(hi){
var sfLabel=hi.safe_flow||hi.max_safe_flow||'?';
var pkLabel=hi.peak_flow||'?';
var ndLabel=hi.nozzle_diameter||'0.4';
h+='<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding-bottom:10px;border-bottom:1px solid #21262d;margin-bottom:10px">'+
'<span style="background:linear-gradient(135deg,#1f6feb,#58a6ff);border-radius:8px;padding:5px 12px;font-weight:700;color:#fff;font-size:13px">Revo '+(hi.nozzle_type||'HF')+'</span>'+
'<span style="color:#e6edf3;font-size:13px">E3D Revo '+(hi.nozzle_type||'HF')+' '+ndLabel+'mm \u2022 '+(hi.heater_wattage||'?')+'W \u2022 '+mat+'</span>'+
'<span style="color:#8b949e;font-size:12px">Safe: <b style="color:#3fb950">'+sfLabel+'</b> \u2022 Peak: <b style="color:#d29922">'+pkLabel+'</b> mm\u00b3/s</span>'+
'</div>'}

/* Printer Hardware grid */
var phw=D.printer_hw;
if(phw&&Object.keys(phw).length>0){
var ext=phw.extruder||{};
var fan=phw.part_fan||{};
var is_hw=phw.input_shaper||{};
var isx=is_hw.x||{};
var isy=is_hw.y||{};
var fanPct=fan.max_power!==undefined?Math.round(fan.max_power*100):100;
var fanClr=fanPct<100?'#f85149':'#3fb950';
/* Compute practical limits from input shaper (the real constraint) */
var shaperMinAccel=Math.min(isx.recommended_max_accel||99999,isy.recommended_max_accel||99999);
var shaperMaxAccel=Math.max(isx.recommended_max_accel||0,isy.recommended_max_accel||0);
var hasShaper=shaperMinAccel<99999;
var practicalAccel=hasShaper?shaperMinAccel:phw.firmware_max_accel;
h+='<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:6px;font-size:12px">';
if(phw.kinematics)h+='<div><span style="color:#8b949e">Kinematics:</span> <b>'+phw.kinematics.toUpperCase()+'</b></div>';
if(phw.build_volume)h+='<div><span style="color:#8b949e">Build:</span> <b>'+phw.build_volume.join('\u00d7')+'mm</b></div>';
if(hasShaper){
h+='<div><span style="color:#8b949e">Quality Max Accel:</span> <b style="color:#3fb950">'+shaperMinAccel+'</b> <span style="color:#484f58">(Y limit)</span></div>';
h+='<div style="grid-column:span 2"><span style="color:#8b949e">Input Shaper:</span>'+
'<div style="display:inline-grid;grid-template-columns:auto auto auto;gap:0 6px;vertical-align:middle;margin-left:6px;font-size:12px">'+
'<b style="color:#58a6ff">X:</b><b>'+(isx.type||'?').toUpperCase()+' @ '+(isx.freq||'?')+' Hz</b><span style="color:#484f58">(max accel '+isx.recommended_max_accel+')</span>'+
'<b style="color:#3fb950">Y:</b><b>'+(isy.type||'?').toUpperCase()+' @ '+(isy.freq||'?')+' Hz</b><span style="color:#484f58">(max accel '+isy.recommended_max_accel+')</span>'+
'</div></div>';
}
if(phw.firmware_max_accel)h+='<div><span style="color:#8b949e">Firmware Ceiling:</span> <span style="color:#484f58">'+phw.firmware_max_accel+' accel / '+(phw.firmware_max_velocity||'?')+' mm/s</span></div>';
if(ext.drive_type)h+='<div><span style="color:#8b949e">Extruder:</span> <b>'+ext.drive_type+'</b> (rot_dist: '+ext.rotation_distance+')</div>';
if(ext.nozzle_diameter)h+='<div><span style="color:#8b949e">Nozzle:</span> <b>'+ext.nozzle_diameter+'mm</b></div>';
if(ext.motor)h+='<div><span style="color:#8b949e">Motor:</span> <b>'+ext.motor+'</b></div>';
h+='<div><span style="color:#8b949e">Part Fan:</span> <b style="color:'+fanClr+'">'+fanPct+'%</b> max_power</div>';
if(phw.z_steppers>=4)h+='<div><span style="color:#8b949e">Z:</span> <b>Quad Gantry</b> ('+phw.z_steppers+' steppers)</div>';
if(phw.probe_type)h+='<div><span style="color:#8b949e">Probe:</span> <b>'+phw.probe_type+'</b></div>';
if(phw.mmu_present)h+='<div><span style="color:#d29922">MMU Present</span></div>';
h+='</div>'}

h+='</div>';

/* --- Performance/Utilization summary items (only show bad/warn, skip info/good as redundant with Boost panel) --- */
var perfItems=pa.filter(function(a){return a.category==='Performance'&&(a.verdict==='bad'||a.verdict==='warn')});
if(perfItems.length){
h+='<div class="box">';
perfItems.forEach(function(a){
var clrMap={'bad':'#f85149','warn':'#d29922'};
var bgMap={'bad':'rgba(248,81,73,0.1)','warn':'rgba(210,153,34,0.1)'};
var iconMap={'bad':'\u26a1','warn':'\u26a0\ufe0f'};
h+='<div style="padding:10px 16px;border-left:3px solid '+clrMap[a.verdict]+';background:'+bgMap[a.verdict]+';border-radius:4px;margin-bottom:4px">'+
'<div style="font-weight:700;color:'+clrMap[a.verdict]+';font-size:13px">'+iconMap[a.verdict]+' '+a.current+'</div>'+
'<div style="color:#c9d1d9;font-size:12px;margin-top:4px">'+a.reason+'</div></div>'});
h+='</div>'}

/* --- Ordered list of all settings to show --- */
var allKeys=[
{k:'nozzle_diameter',g:'Geometry'},{k:'layer_height',g:'Geometry'},
{k:'first_layer_height',g:'Geometry'},
{k:'outer_wall_line_width',g:'Geometry'},{k:'inner_wall_line_width',g:'Geometry'},
{k:'sparse_infill_line_width',g:'Geometry'},{k:'top_surface_line_width',g:'Geometry'},
{k:'initial_layer_line_width',g:'Geometry'},{k:'support_line_width',g:'Geometry'},
{k:'default_acceleration',g:'Acceleration'},{k:'outer_wall_acceleration',g:'Acceleration'},
{k:'inner_wall_acceleration',g:'Acceleration'},{k:'bridge_acceleration',g:'Acceleration'},
{k:'sparse_infill_acceleration',g:'Acceleration'},{k:'internal_solid_infill_acceleration',g:'Acceleration'},
{k:'top_surface_acceleration',g:'Acceleration'},{k:'travel_acceleration',g:'Acceleration'},
{k:'initial_layer_acceleration',g:'Acceleration'},
{k:'outer_wall_speed',g:'Speed'},{k:'inner_wall_speed',g:'Speed'},
{k:'bridge_speed',g:'Speed'},{k:'sparse_infill_speed',g:'Speed'},
{k:'internal_solid_infill_speed',g:'Speed'},{k:'top_surface_speed',g:'Speed'},
{k:'travel_speed',g:'Speed'},{k:'gap_infill_speed',g:'Speed'},
{k:'initial_layer_speed',g:'Speed'},{k:'internal_bridge_speed',g:'Speed'},
{k:'support_speed',g:'Speed'},
{k:'bridge_flow',g:'Quality'},{k:'wall_loops',g:'Quality'},
{k:'wall_sequence',g:'Quality'},
{k:'overhang_1_4_speed',g:'Quality'},{k:'overhang_2_4_speed',g:'Quality'},
{k:'overhang_3_4_speed',g:'Quality'},{k:'overhang_4_4_speed',g:'Quality'},
{k:'small_perimeter_speed',g:'Quality'},{k:'filament_max_volumetric_speed',g:'Quality'}
];

/* Also include summary-level advice items (accel_spread, flow_headroom) */
var summaryItems=pa.filter(function(a){return a.category==='Summary'});

/* --- Count issues --- */
var changeCount=0;
allKeys.forEach(function(e){var a=advMap[e.k];if(a&&a.suggestion)changeCount++});
summaryItems.forEach(function(a){if(a.suggestion)changeCount++});

/* --- Summary banner --- */
if(changeCount>0){
h+='<div class="box" style="border-left:3px solid #d29922;padding:10px 16px">'+
'<span style="color:#d29922;font-weight:700">\u26a0 '+changeCount+' recommended change'+(changeCount>1?'s':'')+'</span>'+
'<span style="color:#8b949e;font-size:12px;margin-left:8px">Settings marked with a suggested value should be updated in OrcaSlicer.</span></div>'}
else{
h+='<div class="box" style="border-left:3px solid #3fb950;padding:10px 16px">'+
'<span style="color:#3fb950;font-weight:700">\u2705 Slicer profile looks good</span>'+
'<span style="color:#8b949e;font-size:12px;margin-left:8px">No changes recommended.</span></div>'}

/* --- Summary-level items (accel spread, flow headroom) --- */
if(summaryItems.length){
h+='<div class="box"><table class="pa-table"><tr><th>Check</th><th>Value</th><th></th><th>Suggested</th><th>Details</th></tr>';
summaryItems.forEach(function(a){
var icon=a.verdict==='good'?'\u2705':a.verdict==='bad'?'\u274c':a.verdict==='warn'?'\u26a0\ufe0f':'\u2139\ufe0f';
var clr=a.verdict==='good'?'#3fb950':a.verdict==='bad'?'#f85149':a.verdict==='warn'?'#d29922':'#58a6ff';
var setting=a.setting.replace(/^_/,'').replace(/_/g,' ');
h+='<tr><td style="font-weight:600;white-space:nowrap">'+icon+' '+setting+'</td>'+
'<td style="font-weight:600">'+a.current+'</td>'+
'<td style="text-align:center;color:#484f58">'+(a.suggestion?'\u2192':'')+'</td>'+
'<td style="font-weight:600;color:#3fb950">'+(a.suggestion||'')+'</td>'+
'<td style="font-size:12px;color:#8b949e">'+a.reason+'</td></tr>'});
h+='</table></div>'}

/* --- Single unified settings table --- */
h+='<div class="box"><table class="pa-table"><tr><th>Setting</th><th>Current</th><th></th><th>Suggested</th><th>Details</th></tr>';
var lastGroup='';
allKeys.forEach(function(e){
var val=ss[e.k];
if(val===undefined||val===null)return;
/* Group header row */
if(e.g!==lastGroup){
lastGroup=e.g;
h+='<tr><td colspan="5" style="padding:10px 10px 4px;font-size:11px;font-weight:700;'+
'color:#58a6ff;text-transform:uppercase;letter-spacing:1px;border-bottom:2px solid #21262d">'+e.g+'</td></tr>'}
var a=advMap[e.k];
var icon='';var clr='#c9d1d9';var sug='';var detail='';var arrow='';
if(a){
icon=a.verdict==='good'?'\u2705 ':a.verdict==='bad'?'\u274c ':a.verdict==='warn'?'\u26a0\ufe0f ':'\u2139\ufe0f ';
clr=a.verdict==='good'?'#3fb950':a.verdict==='bad'?'#f85149':a.verdict==='warn'?'#d29922':'#58a6ff';
if(a.suggestion){sug=a.suggestion;arrow='\u2192'}
detail=a.reason||'';
if(a.flow_mm3s!==undefined)detail=a.flow_mm3s+' mm\u00b3/s \u2014 '+detail}
var valClr=(sug?'#f85149':'#c9d1d9');
h+='<tr><td style="font-weight:600;white-space:nowrap">'+icon+e.k.replace(/_/g,' ')+'</td>'+
'<td style="font-weight:600;color:'+valClr+'">'+val+'</td>'+
'<td style="text-align:center;color:#484f58">'+arrow+'</td>'+
'<td style="font-weight:600;color:#3fb950">'+sug+'</td>'+
'<td style="font-size:12px;color:#8b949e;line-height:1.4">'+detail+'</td></tr>'});
h+='</table></div>';

/* --- Boost Optimization Analysis panel --- */
var bo=D.boost_optimization;
if(bo){
var vColors={'significant_headroom':'#3fb950','moderate_headroom':'#58a6ff','at_limit':'#d29922','well_tuned':'#3fb950'};
var vIcons={'significant_headroom':'\ud83d\ude80','moderate_headroom':'\u2139\ufe0f','at_limit':'\u2705','well_tuned':'\u2705'};
var vClr=vColors[bo.verdict]||'#8b949e';
var vIcon=vIcons[bo.verdict]||'';
h+='<div class="box" style="border-left:3px solid '+vClr+'">';
h+='<div style="font-weight:700;font-size:14px;color:'+vClr+';margin-bottom:6px">'+vIcon+' Optimization Analysis <span style="font-size:11px;font-weight:400;color:#8b949e">(based on actual print data)</span></div>';
h+='<div style="color:#c9d1d9;font-size:13px;margin-bottom:10px">'+bo.verdict_text+'</div>';

/* Headroom gauges */
h+='<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;margin-bottom:10px">';
var gauges=[
{label:'Heater',val:bo.thermal.headroom_pct,unit:'% PWM free',limit:bo.thermal.at_limit},
{label:'Flow',val:bo.flow.headroom_pct,unit:'% headroom',limit:bo.flow.at_limit},
{label:'Boost',val:bo.boost.headroom_pct,unit:'% range free',limit:false}];
if(bo.accel)gauges.push({label:'Accel',val:(100-bo.accel.pct_used),unit:'% unused',limit:bo.accel.at_limit});
gauges.push({label:'Fan',val:bo.fan.at_limit?0:(100-bo.fan.max_fan),unit:'% free',limit:bo.fan.at_limit});
gauges.forEach(function(g){
var pct=Math.max(0,Math.min(100,g.val||0));
var barClr=g.limit?'#f85149':pct>30?'#3fb950':pct>15?'#d29922':'#f85149';
h+='<div style="background:#161b22;border-radius:6px;padding:10px 12px">'+
'<div style="font-size:11px;color:#8b949e;margin-bottom:4px">'+g.label+'</div>'+
'<div style="font-size:18px;font-weight:700;color:'+(g.limit?'#f85149':'#c9d1d9')+'">'+Math.round(pct)+'<span style="font-size:11px;font-weight:400;color:#8b949e">'+g.unit+'</span></div>'+
'<div style="background:#21262d;border-radius:3px;height:6px;margin-top:6px;overflow:hidden">'+
'<div style="width:'+pct+'%;height:100%;background:'+barClr+';border-radius:3px"></div></div></div>'});
h+='</div>';

/* Can increase list */
if(bo.can_increase&&bo.can_increase.length){
h+='<div style="margin-bottom:12px">';
h+='<div style="font-weight:600;font-size:12px;color:#58a6ff;margin-bottom:6px">AVAILABLE HEADROOM</div>';
bo.can_increase.forEach(function(ci){
h+='<div style="padding:6px 10px;background:rgba(88,166,255,0.06);border-radius:4px;margin-bottom:4px;font-size:12px">'+
'<span style="color:#58a6ff;font-weight:600">'+ci.aspect+':</span> '+
'<span style="color:#c9d1d9">'+ci.headroom+'</span>'+
'<div style="color:#8b949e;font-size:11px;margin-top:2px">'+ci.detail+'</div></div>'});
h+='</div>'}

/* Suggestions */
if(bo.suggestions&&bo.suggestions.length){
h+='<div style="margin-bottom:8px">';
h+='<div style="font-weight:600;font-size:12px;color:#3fb950;margin-bottom:6px">SUGGESTIONS</div>';
bo.suggestions.forEach(function(sg){
var impactClr=sg.impact==='cooling constraint'?'#d29922':'#3fb950';
/* Split detail on newlines: first line is summary, rest are per-setting values */
var detParts=(sg.detail||'').split('\\n');
var detHtml='<div style="color:#8b949e;font-size:11px;margin-top:2px">'+detParts[0]+'</div>';
if(detParts.length>1){
detHtml+='<div style="margin-top:4px;padding:4px 8px;background:rgba(139,148,158,0.08);border-radius:3px;font-family:monospace;font-size:11px;color:#c9d1d9;line-height:1.6">';
for(var di=1;di<detParts.length;di++){
var ln=detParts[di].replace(/→/g,'<span style="color:#3fb950;font-weight:600"> → </span>');
detHtml+=ln+(di<detParts.length-1?'<br>':'');}
detHtml+='</div>';}
var applyBtn='';
if(sg.config_var&&sg.suggested_value!=null){
var bId='boost_apply_'+sg.config_var;
applyBtn='<button id="'+bId+'" class="cfg-btn" style="font-size:11px;padding:4px 14px;flex-shrink:0;align-self:center" '+
'onclick="applyChange(\\''+bId+'\\',\\''+sg.config_var+'\\','+sg.suggested_value+',\\''+(sg.material||'')+'\\')"'+
'>Apply '+sg.config_var+' = '+sg.suggested_value+'</button>';}
h+='<div style="display:flex;align-items:flex-start;gap:10px;padding:8px 12px;background:rgba(63,185,80,0.06);border-radius:4px;margin-bottom:4px;font-size:12px">'+
'<div style="flex:1;min-width:0">'+
'<div style="color:#3fb950;font-weight:600">'+sg.what+' <span style="font-size:10px;color:'+impactClr+';font-weight:400">'+sg.impact+'</span></div>'+
detHtml+'</div>'+applyBtn+'</div>'});
h+='</div>'}

/* Per-bracket table */
var bk=bo.brackets;
if(bk&&Object.keys(bk).length>1){
h+='<details style="margin-top:4px"><summary style="cursor:pointer;color:#58a6ff;font-size:12px;font-weight:600">Per-Flow-Bracket Breakdown</summary>';
h+='<table class="pa-table" style="margin-top:8px;font-size:11px"><tr><th>Flow (mm\u00b3/s)</th><th>% Time</th><th>Avg Boost</th><th>Avg PWM</th><th>Avg Speed</th><th>Max Speed</th><th>Status</th></tr>';
Object.keys(bk).sort(function(a,b){return parseFloat(a)-parseFloat(b)}).forEach(function(k){
var br=bk[k];
var ok=br.thermal_ok&&br.boost_ok&&br.flow_ok;
var stIcon=ok?'<span style="color:#3fb950">\u2705</span>':'<span style="color:#d29922">\u26a0\ufe0f</span>';
var reasons=[];
if(!br.thermal_ok)reasons.push('heater');
if(!br.boost_ok)reasons.push('boost');
if(!br.flow_ok)reasons.push('flow');
var stText=ok?'OK':reasons.join(', ')+' stressed';
h+='<tr><td style="font-weight:600">'+k+'</td><td>'+br.pct_time+'%</td>'+
'<td>'+br.avg_boost+'\u00b0C</td><td>'+(br.avg_pwm*100).toFixed(0)+'%</td>'+
'<td>'+br.avg_speed+'</td><td>'+br.max_speed+'</td>'+
'<td>'+stIcon+' '+stText+'</td></tr>'});
h+='</table></details>'}

h+='</div>'}

ca.innerHTML=h}

function rTimeline(tl){
if(!tl.length){ca.innerHTML='<p>No timeline data.</p>';return}
ca.innerHTML='<div class="box"><h3>Temperature</h3>'+
'<p class="box-desc">Blue = what the system is asking for. Red = what your hotend actually reads. Yellow = extra \u00b0C boost added for flow demand. '+
'<span class="good">Good:</span> red tracks blue closely. <span class="warn">Watch:</span> red consistently below blue means heater can\u2019t keep up.</p>'+mc('c1')+
'</div><div class="box"><h3>Flow &amp; Speed</h3>'+
'<p class="box-desc">Green = material flow (mm\u00b3/s). Orange = print speed (mm/s). Purple = heater power (PWM). '+
'High flow + high PWM = heavy thermal load. If PWM is always near 100%, consider slowing down or upgrading your heater.</p>'+mc('c2')+'</div>';
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
ca.innerHTML='<div class="box"><h3>Banding Risk by Z-Height</h3>'+
'<p class="box-desc">Each bar is a layer range. Taller bars = more thermal/PA stress at that height, increasing banding risk. '+
'<span class="good">Green</span> = low risk. <span class="warn">Yellow</span> = moderate. <span class="bad">Red</span> = high \u2014 check print surface at those layers.</p>'+mc('c3')+'</div>';
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
ca.innerHTML+='<div class="box"><h3>Heater Headroom by Flow Rate</h3>'+
'<p class="box-desc">Shows heater power at different flow rates. Blue = average (most important), Orange = 95th percentile, Red = peak. '+
'<strong>Max and P95 hitting 100% is normal</strong> \u2014 Klipper\u2019s PID controller briefly goes full power during every temperature change. '+
'What matters is the <span class="good">blue (average) bar</span>: under 80% = healthy, 80\u201390% = working hard, 90%+ = struggling.</p>'+mc('c4')+'</div>';
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
var h='<h3>Thermal Lag</h3><p class="box-desc">How far behind the actual temperature is from the target. '+
'Small lag (1\u20132\u00b0C) is normal. Sustained lag above 5\u00b0C means under-extrusion risk.</p><p>Avg: '+lg.avg_lag.toFixed(1)+
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
ca.innerHTML+='<div class="box"><h3>Pressure Advance over Time</h3>'+
'<p class="box-desc">PA controls how the extruder compensates for pressure in the nozzle. A flat line = stable, consistent extrusion. '+
'Wobbles mean the system is adjusting to changing conditions. Large jumps may cause surface artifacts.</p>'+mc('c5')+'</div>';
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
var h='<h3>PA Stability</h3><p class="box-desc">How consistent PA stayed during the print. '+
'A small range and low change count = rock-solid extrusion. Oscillation zones flag periods where PA was hunting.</p><p>Range: '+pa.pa_min.toFixed(4)+
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
ca.innerHTML='<div class="box"><h3>DynZ Activation by Z-Height</h3>'+
'<p class="box-desc">Shows where the system reduced acceleration to protect print quality (e.g. overhangs, curves). '+
'Yellow = % of time active at each height. Red = how often it toggled on/off. Frequent transitions can cause artifacts.</p>'+mc('c6')+'</div>';
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
'<p class="box-desc">How your print time was distributed across speeds. '+
'Most time at low speeds = fine detail. Most time at high speeds = fast but harder on the heater.</p>'+
mc('c7')+'</div><div class="box"><h3>Time by Flow Rate</h3>'+
'<p class="box-desc">How your print time was distributed across flow rates. '+
'High flow = more thermal demand. If most time is at high flow, ensure your heater can handle it.</p>'+mc('c8')+'</div></div>';
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
    (with allow_nan=False) won't choke.  Also strip surrogate characters
    from strings to prevent UnicodeEncodeError when encoding to UTF-8."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, str):
        # Remove surrogate characters (U+D800–U+DFFF) that can't be
        # encoded to UTF-8 — they sneak in from files opened without
        # explicit encoding error handling.
        return obj.encode('utf-8', errors='replace').decode('utf-8')
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
            payload = json.dumps(data, default=str).encode('utf-8', errors='replace')
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
            html = generate_dashboard_html(data).encode('utf-8', errors='replace')
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
        for s_info in sessions:
            csv_f = s_info.get('csv_file', '')
            if csv_f:
                banding = analyze_csv_for_banding(csv_f)
                if banding:
                    print_banding_report(banding)
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
