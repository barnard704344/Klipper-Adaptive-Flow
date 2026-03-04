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
import re
import glob
import math
import time
import statistics
import argparse
import http.server
import urllib.parse
import urllib.request
import socket
import subprocess
from pathlib import Path
from collections import defaultdict

# =============================================================================
# CONFIGURATION
# =============================================================================
LOG_DIR = os.path.expanduser('~/printer_data/logs/adaptive_flow')
CONFIG_DIR = os.path.expanduser('~/printer_data/config')
GCODES_DIR = os.path.expanduser('~/printer_data/gcodes')


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
# HARDWARE-AWARE CONFIG PARSING
# =============================================================================

def _parse_klipper_config(filepath):
    """Parse a Klipper INI-like config file into {section: {key: value_str}}.

    Handles both ``key: value`` and ``key = value`` separators.
    Strips inline comments (``# ...``).  Skips comment-only lines.
    """
    result = {}
    current_section = None
    if not os.path.exists(filepath):
        return result
    try:
        with open(filepath) as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith('#') or stripped.startswith(';'):
                    continue
                if stripped.startswith('[') and ']' in stripped:
                    current_section = stripped[1:stripped.index(']')].strip()
                    result.setdefault(current_section, {})
                elif current_section:
                    # Try colon separator first, then equals
                    if ':' in stripped:
                        key, _, val = stripped.partition(':')
                    elif '=' in stripped:
                        key, _, val = stripped.partition('=')
                    else:
                        continue
                    key = key.strip()
                    val = val.split('#')[0].strip()  # strip inline comments
                    if key and not key.startswith('#'):
                        result[current_section][key] = val
    except (IOError, OSError):
        pass
    return result


def _parse_all_klipper_configs(config_dir):
    """Parse printer.cfg and all [include ...] files into a merged dict."""
    printer_cfg = os.path.join(config_dir, 'printer.cfg')
    merged = _parse_klipper_config(printer_cfg)

    # Follow [include ...] directives from printer.cfg only (one level)
    include_files = []
    try:
        with open(printer_cfg) as f:
            for line in f:
                s = line.strip()
                if s.startswith('[include ') and s.endswith(']'):
                    fname = s[9:-1].strip().split('#')[0].strip()
                    if fname:
                        include_files.append(fname)
    except (IOError, OSError):
        pass

    for fname in include_files:
        fpath = os.path.join(config_dir, fname)
        if os.path.isdir(fpath):
            continue
        inc = _parse_klipper_config(fpath)
        for section, keys in inc.items():
            merged.setdefault(section, {}).update(keys)

    return merged


def _safe_float(d, key, default=None):
    """Extract a float from a dict, returning *default* on any failure."""
    v = d.get(key)
    if v is None:
        return default
    try:
        return float(str(v).split('#')[0].strip())
    except (ValueError, TypeError):
        return default


def _safe_int(d, key, default=None):
    """Extract an int from a dict, returning *default* on any failure."""
    v = d.get(key)
    if v is None:
        return default
    try:
        return int(float(str(v).split('#')[0].strip()))
    except (ValueError, TypeError):
        return default


def _safe_str(d, key, default=''):
    """Extract a stripped string from a dict."""
    v = d.get(key)
    if v is None:
        return default
    return str(v).split('#')[0].strip()


def collect_printer_hardware(config_dir=None):
    """Auto-detect printer hardware from Klipper config files.

    Reads printer.cfg and all [include] files.  Returns a normalized dict
    of hardware capabilities.  Gracefully returns an empty dict on failure.
    """
    if config_dir is None:
        config_dir = CONFIG_DIR
    try:
        cfg = _parse_all_klipper_configs(config_dir)
    except Exception:
        return {}

    hw = {}

    # --- [printer] ---
    printer = cfg.get('printer', {})
    if printer:
        hw['kinematics'] = _safe_str(printer, 'kinematics', 'unknown')
        hw['firmware_max_velocity'] = _safe_int(printer, 'max_velocity')
        hw['firmware_max_accel'] = _safe_int(printer, 'max_accel')
        hw['square_corner_velocity'] = _safe_float(printer, 'square_corner_velocity')

    # --- Build volume from stepper position_max ---
    build = {}
    for axis, stepper in [('x', 'stepper_x'), ('y', 'stepper_y'), ('z', 'stepper_z')]:
        sec = cfg.get(stepper, {})
        pmax = _safe_float(sec, 'position_max')
        if pmax is not None:
            build[axis] = pmax
    if build:
        hw['build_volume'] = (build.get('x', 0), build.get('y', 0), build.get('z', 0))

    # --- Z stepper count (quad gantry detection) ---
    z_count = sum(1 for s in cfg if s.startswith('stepper_z'))
    if z_count:
        hw['z_steppers'] = z_count

    # --- [extruder] ---
    ext = cfg.get('extruder', {})
    if ext:
        rot_dist = _safe_float(ext, 'rotation_distance')
        hw['extruder'] = {
            'rotation_distance': rot_dist,
            'drive_type': 'direct' if (rot_dist and rot_dist <= 8) else 'bowden' if rot_dist else 'unknown',
            'nozzle_diameter': _safe_float(ext, 'nozzle_diameter', 0.4),
            'thermistor': _safe_str(ext, 'sensor_type'),
            'max_temp': _safe_int(ext, 'max_temp'),
        }

    # --- [tmc2209 extruder] or [tmc5160 extruder] ---
    for driver in ('tmc2209', 'tmc5160', 'tmc2130', 'tmc2660'):
        tmc_sec = cfg.get(f'{driver} extruder', {})
        if tmc_sec:
            hw.setdefault('extruder', {})['tmc_driver'] = driver
            hw['extruder']['run_current'] = _safe_float(tmc_sec, 'run_current')
            break

    # --- [autotune_tmc extruder] ---
    autotune = cfg.get('autotune_tmc extruder', {})
    if autotune:
        hw.setdefault('extruder', {})['motor'] = _safe_str(autotune, 'motor')
        hw['extruder']['tuning_goal'] = _safe_str(autotune, 'tuning_goal')

    # --- [fan] (part cooling) ---
    fan = cfg.get('fan', {})
    if fan:
        hw['part_fan'] = {
            'max_power': _safe_float(fan, 'max_power', 1.0),
            'hardware_pwm': _safe_str(fan, 'hardware_pwm', 'False').lower() == 'true',
            'cycle_time': _safe_float(fan, 'cycle_time'),
        }

    # --- [input_shaper] ---
    shaper = cfg.get('input_shaper', {})
    if shaper:
        hw['input_shaper'] = {}
        for axis in ('x', 'y'):
            stype = _safe_str(shaper, f'shaper_type_{axis}') or _safe_str(shaper, 'shaper_type')
            freq = _safe_float(shaper, f'shaper_freq_{axis}')
            damp = _safe_float(shaper, f'damping_ratio_{axis}')
            if stype or freq:
                entry = {}
                if stype:
                    entry['type'] = stype
                if freq:
                    entry['freq'] = freq
                    # Compute recommended max accel: shaper_freq × 100
                    entry['recommended_max_accel'] = int(freq * 100)
                if damp:
                    entry['damping'] = damp
                hw['input_shaper'][axis] = entry

    # --- XY TMC drivers ---
    for driver in ('tmc2209', 'tmc5160', 'tmc2130', 'tmc2660'):
        tmc_x = cfg.get(f'{driver} stepper_x', {})
        if tmc_x:
            stealthchop = _safe_str(tmc_x, 'stealthchop_threshold', '0')
            hw['xy_tmc'] = {
                'driver': driver,
                'run_current': _safe_float(tmc_x, 'run_current'),
                'stealthchop': stealthchop != '0',
            }
            break

    # --- Probe type ---
    for section in cfg:
        if 'probe_eddy' in section:
            hw['probe_type'] = 'eddy'
            break
        elif section == 'bltouch':
            hw['probe_type'] = 'bltouch'
            break
        elif section == 'probe':
            hw['probe_type'] = 'probe'
            break

    # --- MMU detection ---
    mmu_dir = os.path.join(config_dir, 'mmu')
    if os.path.isdir(mmu_dir):
        hw['mmu_present'] = True
    else:
        # Also check for MMU sections in config
        hw['mmu_present'] = any(s.startswith('mmu') for s in cfg)

    return hw


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
# SLICER SETTINGS EXTRACTOR — parse OrcaSlicer/PrusaSlicer G-code footers
# =============================================================================

# Keys of interest from the slicer footer — grouped by category.
# These appear in OrcaSlicer gcode as ``; key = value`` lines near EOF.
_SLICER_ACCEL_KEYS = [
    'default_acceleration', 'outer_wall_acceleration', 'inner_wall_acceleration',
    'bridge_acceleration', 'sparse_infill_acceleration',
    'internal_solid_infill_acceleration', 'top_surface_acceleration',
    'travel_acceleration', 'initial_layer_acceleration',
]
_SLICER_SPEED_KEYS = [
    'outer_wall_speed', 'inner_wall_speed', 'bridge_speed',
    'sparse_infill_speed', 'internal_solid_infill_speed',
    'top_surface_speed', 'travel_speed', 'gap_infill_speed',
    'initial_layer_speed', 'internal_bridge_speed', 'support_speed',
]
_SLICER_OTHER_KEYS = [
    'bridge_flow', 'wall_loops', 'wall_sequence',
    'overhang_1_4_speed', 'overhang_2_4_speed',
    'overhang_3_4_speed', 'overhang_4_4_speed',
    'small_perimeter_speed', 'filament_max_volumetric_speed',
]
_SLICER_ALL_KEYS = set(_SLICER_ACCEL_KEYS + _SLICER_SPEED_KEYS + _SLICER_OTHER_KEYS)

# Regex to parse ``; key = value`` lines in OrcaSlicer footer
_SLICER_LINE_RE = re.compile(r'^\s*;\s*(\w+)\s*=\s*(.+?)\s*$')


def extract_slicer_settings(gcode_path):
    """Extract slicer settings from the OrcaSlicer/PrusaSlicer gcode footer.

    We only need the last ~2000 lines where OrcaSlicer writes its config
    block.  Returns a dict of {key: value} for recognized settings, or
    None if the file can't be read or contains no settings.
    """
    if not gcode_path or not os.path.isfile(gcode_path):
        return None

    settings = {}
    try:
        # Read only the tail of the file — the config block is at the end.
        # We use a deque-based approach to avoid reading 100k+ line files.
        from collections import deque
        with open(gcode_path, 'r', errors='replace') as f:
            tail = deque(f, maxlen=2000)
        for line in tail:
            m = _SLICER_LINE_RE.match(line)
            if m:
                key, val = m.group(1), m.group(2)
                if key in _SLICER_ALL_KEYS:
                    settings[key] = _parse_slicer_value(val)
    except Exception as exc:
        print(f"Warning: Could not extract slicer settings from {gcode_path}: {exc}")
        return None

    return settings if settings else None


def _parse_slicer_value(raw):
    """Convert a raw slicer value string to int, float, or str."""
    raw = raw.strip().strip('"')
    # Percentage values like "80%" — keep as string for display
    if raw.endswith('%'):
        return raw
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _find_gcode_for_summary(summary, gcodes_dir=None):
    """Find the gcode file on disk that corresponds to a print summary.

    The summary JSON ``filename`` field contains the original gcode name
    (e.g. ``Voron_Design_Cube_v7(R2)_PETG_25m48s.gcode``).  We try an
    exact match first, then fall back to fuzzy (everything before the
    time estimate).

    Returns the absolute gcode path, or None if not found.
    """
    if gcodes_dir is None:
        gcodes_dir = GCODES_DIR
    filename = (summary or {}).get('filename', '')
    if not filename:
        return None

    # 1. Exact match
    exact = os.path.join(gcodes_dir, filename)
    if os.path.isfile(exact):
        return exact

    # 2. Fuzzy match — strip the time estimate suffix and look for any
    #    file that starts with the same prefix.
    #    e.g. "Voron_Design_Cube_v7(R2)_PETG_25m48s.gcode"
    #       → prefix = "Voron_Design_Cube_v7(R2)_PETG_"
    base = os.path.splitext(filename)[0]  # remove .gcode
    # OrcaSlicer time suffix pattern: 1h25m, 25m48s, 3h2m, etc.
    m = re.match(r'^(.+?_)\d+[hm]\d+[ms]?$', base)
    if m:
        prefix = m.group(1)
        try:
            candidates = [
                f for f in os.listdir(gcodes_dir)
                if f.startswith(prefix) and f.endswith('.gcode')
            ]
            if candidates:
                # Pick the most recently modified one
                candidates.sort(
                    key=lambda f: os.path.getmtime(os.path.join(gcodes_dir, f)),
                    reverse=True,
                )
                return os.path.join(gcodes_dir, candidates[0])
        except OSError:
            pass

    return None


def analyze_slicer_vs_banding(slicer_settings, banding_data, csv_accel_values):
    """Cross-reference slicer acceleration settings with observed banding.

    Given the extracted slicer settings, banding analysis from the CSV,
    and the raw list of accel values seen during printing, produce a
    diagnostic dict with:
    - distinct_accels: unique accel values observed in the CSV
    - accel_map: mapping of observed accel → probable slicer feature
    - max_accel_swing: largest single accel change observed
    - issues: list of specific slicer setting problems found
    - suggestions: list of {setting, current, suggested, reason} dicts

    Returns None if insufficient data.
    """
    if not slicer_settings or not csv_accel_values:
        return None

    result = {
        'distinct_accels': [],
        'accel_map': {},
        'max_accel_swing': 0,
        'issues': [],
        'suggestions': [],
        'settings_summary': {},
    }

    # --- Build settings summary for display ---
    for key in _SLICER_ACCEL_KEYS + _SLICER_SPEED_KEYS + _SLICER_OTHER_KEYS:
        if key in slicer_settings:
            result['settings_summary'][key] = slicer_settings[key]

    # --- Distinct acceleration values from CSV ---
    from collections import Counter
    accel_counter = Counter(csv_accel_values)
    distinct = sorted(accel_counter.keys())
    result['distinct_accels'] = distinct

    # --- Helpers: coerce slicer values to numbers ---
    def _to_num(v):
        """Coerce a value to float if possible, else return None."""
        if v is None:
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    def _to_num_or_pct(v, ref=None):
        """Coerce value to float; resolve '50%' style strings against *ref*."""
        if v is None:
            return None
        if isinstance(v, str) and v.strip().endswith('%'):
            try:
                pct = float(v.strip().rstrip('%')) / 100.0
                return pct * ref if ref else None
            except (ValueError, TypeError):
                return None
        return _to_num(v)

    # --- Map observed accels to slicer features ---
    # Build a reverse lookup: slicer accel value → feature name(s)
    # Resolve percentage values (e.g. '100%', '50%') against the reference accel.
    _ref_accel_for_map = _to_num(slicer_settings.get('default_acceleration')) or 10000
    feature_map = {}
    for key in _SLICER_ACCEL_KEYS:
        val = slicer_settings.get(key)
        if val is None:
            continue
        # Resolve percentage strings like '100%' or '50%'
        if isinstance(val, str) and val.strip().endswith('%'):
            try:
                resolved = float(val.strip().rstrip('%')) / 100.0 * _ref_accel_for_map
                ival = int(resolved)
            except (ValueError, TypeError):
                continue
        elif isinstance(val, (int, float)):
            ival = int(val)
        else:
            continue
        if ival not in feature_map:
            feature_map[ival] = []
        nice_name = key.replace('_acceleration', '').replace('_', ' ').title()
        feature_map[ival].append(nice_name)

    for accel_val in distinct:
        count = accel_counter[accel_val]
        if accel_val in feature_map:
            result['accel_map'][str(accel_val)] = {
                'features': feature_map[accel_val],
                'count': count,
                'pct': round(100 * count / len(csv_accel_values), 1),
            }
        else:
            # Unknown — might be Klipper default or DynZ override
            result['accel_map'][str(accel_val)] = {
                'features': ['Unknown / Klipper default'],
                'count': count,
                'pct': round(100 * count / len(csv_accel_values), 1),
            }

    # --- Largest accel swing from banding data ---
    accel_spikes = (banding_data or {}).get('events', {}).get('accel_spikes', [])
    if accel_spikes:
        result['max_accel_swing'] = max(abs(s['delta']) for s in accel_spikes)

    # --- Identify issues and generate specific suggestions ---
    outer_accel = _to_num(slicer_settings.get('outer_wall_acceleration'))
    inner_accel = _to_num(slicer_settings.get('inner_wall_acceleration'))
    default_accel = _to_num(slicer_settings.get('default_acceleration'))
    top_accel = _to_num(slicer_settings.get('top_surface_acceleration'))
    travel_accel = _to_num(slicer_settings.get('travel_acceleration'))
    bridge_flow_val = _to_num(slicer_settings.get('bridge_flow'))
    # bridge_accel may be a percentage like '50%' — resolve against a reference
    _ref_for_bridge = outer_accel or default_accel or 10000
    bridge_accel = _to_num_or_pct(slicer_settings.get('bridge_acceleration'), _ref_for_bridge)

    # Issue 1: Bridge accel much lower than wall accel → causes big swings
    #          at recessed features the slicer misidentifies as bridges
    ref_accel = outer_accel or default_accel or 10000
    if bridge_accel and ref_accel and bridge_accel < ref_accel * 0.6:
        swing = ref_accel - bridge_accel
        result['issues'].append({
            'type': 'bridge_accel_mismatch',
            'detail': (
                f'Bridge acceleration ({bridge_accel}) is {swing} lower than '
                f'outer wall ({ref_accel}). OrcaSlicer often misidentifies '
                f'recessed features (nut pockets, logos) as bridges, causing '
                f'large acceleration swings that show as banding lines.'
            ),
        })
        result['suggestions'].append({
            'setting': 'bridge_acceleration',
            'current': bridge_accel,
            'suggested': int(ref_accel * 0.8),
            'reason': 'Reduce accel swings at false-bridge features',
        })

    # Issue 2: Inner vs outer wall accel mismatch → transition lines
    if outer_accel and inner_accel and abs(outer_accel - inner_accel) > 3000:
        result['issues'].append({
            'type': 'wall_accel_mismatch',
            'detail': (
                f'Inner wall accel ({inner_accel}) differs from outer wall '
                f'({outer_accel}) by {abs(inner_accel - outer_accel)}. '
                f'Each wall transition causes an acceleration change that '
                f'can show as a faint line.'
            ),
        })
        target = outer_accel  # match outer for consistency
        result['suggestions'].append({
            'setting': 'inner_wall_acceleration',
            'current': inner_accel,
            'suggested': target,
            'reason': 'Match outer wall to eliminate wall transition accel swings',
        })

    # Issue 3: Bridge flow < 1.0 → under-extrusion on false bridges
    if bridge_flow_val is not None and isinstance(bridge_flow_val, (int, float)):
        if bridge_flow_val < 0.95:
            result['issues'].append({
                'type': 'bridge_flow_low',
                'detail': (
                    f'Bridge flow ratio ({bridge_flow_val}) causes '
                    f'{(1 - bridge_flow_val) * 100:.0f}% under-extrusion on '
                    f'any feature the slicer classifies as a bridge — including '
                    f'recessed areas that aren\'t true bridges.'
                ),
            })
            result['suggestions'].append({
                'setting': 'bridge_flow',
                'current': bridge_flow_val,
                'suggested': 1.0,
                'reason': 'Prevent under-extrusion on false-bridge features',
            })

    # Issue 4: Too many distinct accel values → frequent switching
    if len(distinct) >= 5 and result['max_accel_swing'] > 3000:
        result['issues'].append({
            'type': 'too_many_accels',
            'detail': (
                f'The slicer used {len(distinct)} distinct acceleration values '
                f'({min(distinct)}–{max(distinct)}). Each transition is a '
                f'potential banding line. Max swing was ±{result["max_accel_swing"]:.0f}.'
            ),
        })
        # Suggest consolidating accel values
        target_wall = outer_accel or default_accel
        if target_wall and inner_accel and inner_accel != target_wall:
            result['suggestions'].append({
                'setting': 'inner_wall_acceleration',
                'current': int(inner_accel),
                'suggested': int(target_wall),
                'reason': 'Match outer wall to reduce accel transitions',
            })
        if target_wall and travel_accel and travel_accel > target_wall * 2:
            result['suggestions'].append({
                'setting': 'travel_acceleration',
                'current': int(travel_accel),
                'suggested': int(target_wall * 1.5),
                'reason': 'Reduce travel accel gap to minimize transition artifacts',
            })
        if target_wall and top_accel and top_accel != target_wall:
            result['suggestions'].append({
                'setting': 'top_surface_acceleration',
                'current': int(top_accel),
                'suggested': int(target_wall),
                'reason': 'Match wall accel to avoid top-surface transition lines',
            })

    # Issue 5: Top surface accel very different from normal printing
    if top_accel and ref_accel and abs(top_accel - ref_accel) > 4000:
        result['issues'].append({
            'type': 'top_accel_mismatch',
            'detail': (
                f'Top surface accel ({top_accel}) differs from wall accel '
                f'({ref_accel}) by {abs(top_accel - ref_accel)}. This can '
                f'cause visible transitions at top surfaces.'
            ),
        })

    return result if (result['issues'] or result['settings_summary']) else None


# =============================================================================
# SLICER PROFILE ADVISOR — comprehensive per-setting recommendations
# =============================================================================

def generate_slicer_profile_advice(slicer_settings, hotend_info, print_summary=None, printer_hw=None):
    """Produce comprehensive per-setting advice for every parsed slicer value.

    *hotend_info* is a dict with keys from the adaptive flow config:
        - nozzle_type: 'HF' or 'SF'
        - max_safe_flow: float (mm\u00b3/s)
        - heater_wattage: int (40 or 60)

    *printer_hw* is an optional dict from ``collect_printer_hardware()`` with
    firmware limits, input shaper data, fan caps, etc.

    Returns a list of dicts:
        {setting, category, current, verdict, suggestion, reason, flow_mm3s}
    verdict: 'good', 'warn', 'bad', 'info'
    """
    if not slicer_settings or not hotend_info:
        return []
    if printer_hw is None:
        printer_hw = {}

    advice = []
    nozzle = hotend_info.get('nozzle_type', 'HF')
    wattage = hotend_info.get('heater_wattage', 40)

    # E3D Revo flow limits (source of truth) — fall back to config value
    safe_flow = hotend_info.get('safe_flow')
    peak_flow = hotend_info.get('peak_flow')
    if safe_flow is None or peak_flow is None:
        fallback = hotend_info.get('max_safe_flow', 25.0 if nozzle == 'HF' else 15.0)
        safe_flow = safe_flow or fallback
        peak_flow = peak_flow or fallback * 1.15
    max_flow = safe_flow
    material = hotend_info.get('material', 'PLA')
    nozzle_dia = hotend_info.get('nozzle_diameter', 0.4)
    variant = nozzle

    # Geometry values for flow calculation
    layer_h = slicer_settings.get('layer_height', 0.2)
    first_layer_h = slicer_settings.get('first_layer_height', layer_h)
    nozzle_d = slicer_settings.get('nozzle_diameter', 0.4)

    def _line_w(key, fallback=None):
        v = slicer_settings.get(key)
        if v is not None:
            return float(v)
        return fallback if fallback else nozzle_d + 0.02

    outer_w = _line_w('outer_wall_line_width', nozzle_d + 0.02)
    inner_w = _line_w('inner_wall_line_width', nozzle_d + 0.05)
    infill_w = _line_w('sparse_infill_line_width', nozzle_d + 0.05)
    top_w = _line_w('top_surface_line_width', nozzle_d + 0.05)
    first_w = _line_w('initial_layer_line_width', nozzle_d + 0.08)

    def _flow(speed, width, height):
        if speed and width and height:
            return round(speed * width * height, 1)
        return 0

    def _flow_verdict(flow_val):
        if flow_val <= 0:
            return 'info'
        if flow_val > peak_flow:
            return 'bad'
        if flow_val > safe_flow * 0.85:
            return 'warn'
        return 'good'

    def _add(setting, category, current, verdict, suggestion, reason, flow=None):
        entry = {
            'setting': setting, 'category': category, 'current': current,
            'verdict': verdict, 'suggestion': suggestion, 'reason': reason,
        }
        if flow is not None:
            entry['flow_mm3s'] = flow
        advice.append(entry)

    # =====================================================================
    # ACCELERATION SETTINGS
    # =====================================================================
    # OrcaSlicer can store accel values as percentages (e.g. '50%')
    # that need resolving against default_acceleration
    def _coerce_accel(val, ref=None):
        """Convert a slicer accel value to a numeric value.
        Handles int, float, and percentage strings like '50%'."""
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return val
        s = str(val).strip()
        if s.endswith('%') and ref is not None:
            try:
                return ref * float(s[:-1]) / 100.0
            except (ValueError, TypeError):
                return None
        try:
            return float(s)
        except (ValueError, TypeError):
            return None

    _raw_default = slicer_settings.get('default_acceleration')
    # default_acceleration is the reference for percentage values
    _ref_accel = _coerce_accel(_raw_default) or 10000

    default_accel = _coerce_accel(slicer_settings.get('default_acceleration'))
    outer_accel = _coerce_accel(slicer_settings.get('outer_wall_acceleration'), _ref_accel)
    inner_accel = _coerce_accel(slicer_settings.get('inner_wall_acceleration'), _ref_accel)
    bridge_accel = _coerce_accel(slicer_settings.get('bridge_acceleration'), _ref_accel)
    infill_accel = _coerce_accel(slicer_settings.get('sparse_infill_acceleration'), _ref_accel)
    solid_accel = _coerce_accel(slicer_settings.get('internal_solid_infill_acceleration'), _ref_accel)
    top_accel = _coerce_accel(slicer_settings.get('top_surface_acceleration'), _ref_accel)
    travel_accel = _coerce_accel(slicer_settings.get('travel_acceleration'), _ref_accel)
    first_accel = _coerce_accel(slicer_settings.get('initial_layer_acceleration'), _ref_accel)

    wall_accel = outer_accel or inner_accel or 5000
    MAX_ACCEL_GAP = 3000

    # Input shaper is the REAL constraint — firmware max is just a ceiling
    _fw_max_vel = (printer_hw or {}).get('firmware_max_velocity') or 500
    _kinematics = (printer_hw or {}).get('kinematics', 'unknown')
    _is_fast_printer = _kinematics in ('corexy', 'corexz') or _fw_max_vel >= 300
    _is_data = (printer_hw or {}).get('input_shaper', {})
    _shaper_limits = {}  # {axis: limit}
    _shaper_info = {}     # {axis: {limit, type, freq}}
    for _ax in ('x', 'y'):
        _ax_data = _is_data.get(_ax, {})
        _rec = _ax_data.get('recommended_max_accel')
        if _rec:
            _shaper_limits[_ax] = _rec
            _shaper_info[_ax] = {
                'limit': _rec,
                'type': _ax_data.get('type', '?'),
                'freq': _ax_data.get('freq', 0),
            }
    # Per-axis limits for axis-aware recommendations
    _shaper_x = _shaper_limits.get('x')  # None if no X shaper data
    _shaper_y = _shaper_limits.get('y')  # None if no Y shaper data
    _shaper_quality_max = min(_shaper_limits.values()) if _shaper_limits else None  # most restrictive
    _shaper_perf_max = max(_shaper_limits.values()) if _shaper_limits else None    # least restrictive
    _fw_accel = (printer_hw or {}).get('firmware_max_accel')

    # Practical accel limit: input shaper quality limit, NOT firmware ceiling
    # Use min (most restrictive) for wall features (arbitrary geometry)
    # Use max (least restrictive) for infill (can be axis-aligned)
    _practical_accel = _shaper_quality_max or _fw_accel or 5000
    _practical_accel_infill = _shaper_perf_max or _fw_accel or 5000

    # Optimal accel ranges based on hardware
    _optimal_wall_accel = int(_practical_accel * 0.85) if _shaper_quality_max else 5000
    _optimal_infill_accel = int(min(_practical_accel_infill,
                                     _fw_accel or 20000) * 0.7) if _shaper_limits else 8000
    _optimal_travel_accel = int(min((_fw_accel or 20000), 15000))

    if default_accel is not None:
        gap_to_wall = abs(default_accel - wall_accel) if wall_accel else 0
        if default_accel > 15000:
            _add('default_acceleration', 'Acceleration', int(default_accel),
                 'bad', f'{int(wall_accel)}',
                 f'Very high default accel. Klipper uses this as the ceiling. '
                 f'Gap of \u00b1{int(gap_to_wall)} from walls \u2014 '
                 f'set to {int(wall_accel)} so Klipper doesn\u2019t override your per-feature accels.')
        elif gap_to_wall >= MAX_ACCEL_GAP and wall_accel:
            _add('default_acceleration', 'Acceleration', int(default_accel),
                 'warn', f'{int(wall_accel)}',
                 f'Gap of \u00b1{int(gap_to_wall)} from walls ({int(wall_accel)}). '
                 f'Klipper uses this as the ceiling/fallback \u2014 if a feature has no '
                 f'specific accel, it uses this value, creating banding. '
                 f'Set to {int(wall_accel)} to match your wall accel.')
        elif default_accel > 10000:
            _add('default_acceleration', 'Acceleration', int(default_accel),
                 'good', None,
                 f'Default accel for Revo {variant} with {wattage}W heater. '
                 f'Within \u00b1{int(gap_to_wall)} of walls \u2014 acceptable.')
        elif _is_fast_printer and default_accel < _optimal_wall_accel * 0.5:
            shaper_note = (f'Your input shaper supports up to {_shaper_quality_max} on walls. '
                          if _shaper_quality_max else '')
            _add('default_acceleration', 'Acceleration', int(default_accel),
                 'warn', f'{_optimal_wall_accel}',
                 f'Very low for a {_kinematics} printer with {_fw_accel or "high"} max_accel. '
                 f'{shaper_note}'
                 f'Set to {_optimal_wall_accel} to match your hardware.')
        elif _shaper_quality_max and default_accel > _shaper_quality_max:
            _add('default_acceleration', 'Acceleration', int(default_accel),
                 'warn', f'{int(_shaper_quality_max)}',
                 f'Exceeds input shaper quality limit ({_shaper_quality_max}). '
                 f'Klipper uses this as the ceiling/fallback — set to '
                 f'{int(_shaper_quality_max)} to stay within shaper limit.')
        else:
            _add('default_acceleration', 'Acceleration', int(default_accel),
                 'info', None,
                 f'Conservative default. Fine for quality, but your Revo {variant} can handle more if you want speed.')

    if outer_accel is not None:
        if outer_accel > 10000:
            _add('outer_wall_acceleration', 'Acceleration', int(outer_accel),
                 'warn', '4000\u20138000',
                 'Outer walls define surface quality. Very high accel causes ringing and resonance artifacts.')
        elif _shaper_quality_max and outer_accel < _shaper_quality_max * 0.40 and _is_fast_printer:
            _add('outer_wall_acceleration', 'Acceleration', int(outer_accel),
                 'warn', f'{_optimal_wall_accel}',
                 f'Under-utilizing your {_kinematics} printer. '
                 f'Input shaper ({_is_data.get("y", {}).get("type", "?").upper()} @ '
                 f'{_is_data.get("y", {}).get("freq", "?")}Hz) '
                 f'supports up to {_shaper_quality_max} for quality prints. '
                 f'Set to {_optimal_wall_accel} for faster prints with clean walls.')
        elif outer_accel >= 3000:
            shaper_note = ' Your input shaper handles ringing at this accel.' if _shaper_quality_max else ''
            _add('outer_wall_acceleration', 'Acceleration', int(outer_accel),
                 'good', None,
                 f'Good range for quality.{shaper_note}')
        else:
            shaper_note = ' \u2014 input shaper will handle it' if _shaper_quality_max else ''
            if _optimal_wall_accel and outer_accel >= _optimal_wall_accel:
                # Already at or above optimal — don't suggest lowering with "push to" wording
                _add('outer_wall_acceleration', 'Acceleration', int(outer_accel),
                     'good', None,
                     f'At optimal range for a {_kinematics} printer{shaper_note}.')
            else:
                _add('outer_wall_acceleration', 'Acceleration', int(outer_accel),
                     'info', f'{_optimal_wall_accel}',
                     f'Conservative for a {_kinematics} printer. '
                     f'You can push to {_optimal_wall_accel}{shaper_note}.')

    if inner_accel is not None:
        gap = abs(inner_accel - outer_accel) if outer_accel else 0
        if outer_accel and gap >= MAX_ACCEL_GAP * 2:
            _add('inner_wall_acceleration', 'Acceleration', int(inner_accel),
                 'bad', str(int(outer_accel)),
                 f'Gap of \u00b1{int(gap)} from outer wall \u2014 '
                 f'set inner = outer to avoid transition lines between wall passes.')
        elif outer_accel and gap >= MAX_ACCEL_GAP:
            _add('inner_wall_acceleration', 'Acceleration', int(inner_accel),
                 'warn', str(int(outer_accel)),
                 f'Gap of \u00b1{int(gap)} from outer wall \u2014 '
                 f'set inner = outer to avoid transition lines between wall passes.')
        elif outer_accel and inner_accel == outer_accel:
            _add('inner_wall_acceleration', 'Acceleration', int(inner_accel),
                 'good', None,
                 'Matches outer wall \u2014 no accel transition between wall passes. Ideal.')
        else:
            _add('inner_wall_acceleration', 'Acceleration', int(inner_accel),
                 'good', None,
                 f'Close to outer wall (\u00b1{int(gap)} gap). Acceptable.')

    if bridge_accel is not None:
        gap = abs(bridge_accel - outer_accel) if outer_accel else (abs(bridge_accel - wall_accel) if wall_accel else 0)
        ref_accel = outer_accel or wall_accel
        if ref_accel and gap >= MAX_ACCEL_GAP * 2:
            _add('bridge_acceleration', 'Acceleration', int(bridge_accel),
                 'bad', str(int(ref_accel)),
                 f'Gap of \u00b1{int(gap)} from walls ({int(ref_accel)}). '
                 f'OrcaSlicer misidentifies recessed features as bridges \u2014 '
                 f'set bridge accel equal to wall accel.')
        elif ref_accel and gap >= MAX_ACCEL_GAP:
            _add('bridge_acceleration', 'Acceleration', int(bridge_accel),
                 'warn', str(int(ref_accel)),
                 f'Gap of \u00b1{int(gap)} from walls ({int(ref_accel)}). '
                 f'OrcaSlicer misidentifies features as bridges \u2014 '
                 f'set to {int(ref_accel)} to match wall accel.')
        elif ref_accel and gap >= MAX_ACCEL_GAP * 0.8:
            _add('bridge_acceleration', 'Acceleration', int(bridge_accel),
                 'info', str(int(ref_accel)),
                 f'Gap of \u00b1{int(gap)} from walls ({int(ref_accel)}) is borderline. '
                 f'Consider reducing to {int(ref_accel)} if you see transition artifacts at bridges.')
        else:
            _add('bridge_acceleration', 'Acceleration', int(bridge_accel),
                 'good', None,
                 f'Close to wall accel (\u00b1{int(gap)} gap). Minimal transition artifact risk.')

    if infill_accel is not None:
        gap = abs(infill_accel - wall_accel) if wall_accel else 0
        if wall_accel and gap >= MAX_ACCEL_GAP * 2:
            _add('sparse_infill_acceleration', 'Acceleration', int(infill_accel),
                 'bad', f'{int(wall_accel)}',
                 f'Gap of \u00b1{int(gap)} from walls ({int(wall_accel)}). '
                 f'This is the #1 cause of horizontal banding \u2014 every layer transition '
                 f'between wall and infill creates a visible line. '
                 f'Set to {int(wall_accel)} (same as walls).')
        elif wall_accel and gap >= MAX_ACCEL_GAP:
            _add('sparse_infill_acceleration', 'Acceleration', int(infill_accel),
                 'warn', f'{int(wall_accel)}',
                 f'Gap of \u00b1{int(gap)} from walls ({int(wall_accel)}). '
                 f'Infill-to-wall transitions can cause faint banding. '
                 f'Set to {int(wall_accel)} for best results.')
        else:
            _add('sparse_infill_acceleration', 'Acceleration', int(infill_accel),
                 'good', None,
                 f'Close to wall accel (\u00b1{int(gap)} gap). Minimal banding risk.')

    if solid_accel is not None:
        gap = abs(solid_accel - wall_accel) if wall_accel else 0
        if wall_accel and gap >= MAX_ACCEL_GAP * 2:
            _add('internal_solid_infill_acceleration', 'Acceleration', int(solid_accel),
                 'bad', str(int(wall_accel)),
                 f'Gap of \u00b1{int(gap)} from walls ({int(wall_accel)}). '
                 f'Solid infill transitions create visible lines where fill meets walls. '
                 f'Set to {int(wall_accel)}.')
        elif wall_accel and gap >= MAX_ACCEL_GAP:
            _add('internal_solid_infill_acceleration', 'Acceleration', int(solid_accel),
                 'warn', str(int(wall_accel)),
                 f'Gap of \u00b1{int(gap)} from walls ({int(wall_accel)}). '
                 f'Solid infill transitions can affect top/bottom surface quality. '
                 f'Set to {int(wall_accel)} for uniform accel.')
        else:
            _add('internal_solid_infill_acceleration', 'Acceleration', int(solid_accel),
                 'good', None,
                 f'Close to wall accel (\u00b1{int(gap)} gap). Good.')

    if top_accel is not None:
        gap = abs(top_accel - wall_accel) if wall_accel else 0
        if wall_accel and gap >= MAX_ACCEL_GAP * 2:
            _add('top_surface_acceleration', 'Acceleration', int(top_accel),
                 'bad', str(int(wall_accel)),
                 f'Gap of \u00b1{int(gap)} from walls ({int(wall_accel)}). '
                 f'Top surface meets walls at edges \u2014 matching accel prevents transition lines.')
        elif wall_accel and gap >= MAX_ACCEL_GAP:
            _add('top_surface_acceleration', 'Acceleration', int(top_accel),
                 'warn', str(int(wall_accel)),
                 f'Gap of \u00b1{int(gap)} from walls ({int(wall_accel)}). '
                 f'Top surface meets walls at edges \u2014 matching accel prevents transition lines.')
        elif top_accel < 3000:
            if _shaper_quality_max and 4000 > _shaper_quality_max:
                # Don't suggest values above the input shaper quality limit
                if top_accel < _shaper_quality_max:
                    _add('top_surface_acceleration', 'Acceleration', int(top_accel),
                         'info', f'{int(_shaper_quality_max)}',
                         f'Can increase up to {int(_shaper_quality_max)} (input shaper quality limit) '
                         f'for faster prints without quality loss on top surfaces.')
                else:
                    _add('top_surface_acceleration', 'Acceleration', int(top_accel),
                         'good', None,
                         f'At input shaper quality limit ({_shaper_quality_max}). Good for top surface quality.')
            else:
                _add('top_surface_acceleration', 'Acceleration', int(top_accel),
                     'info', '4000\u20136000',
                     'Very conservative. Can increase for faster prints without quality loss on top surfaces.')
        else:
            _add('top_surface_acceleration', 'Acceleration', int(top_accel),
                 'good', None,
                 f'Close to wall accel (\u00b1{int(gap)} gap). Good for top surface quality.')

    if travel_accel is not None:
        if travel_accel < 5000 and _is_fast_printer:
            _add('travel_acceleration', 'Acceleration', int(travel_accel),
                 'warn', f'{_optimal_travel_accel}',
                 f'Travel moves don\'t extrude \u2014 on a {_kinematics} printer with '
                 f'{_fw_accel or "high"} max_accel, set travel accel to {_optimal_travel_accel} '
                 f'for faster repositioning and less ooze.')
        elif travel_accel < 5000:
            _add('travel_acceleration', 'Acceleration', int(travel_accel),
                 'info', '10000\u201315000',
                 'Travel moves don\'t extrude \u2014 higher accel means faster repositioning and less ooze.')
        elif travel_accel > 20000:
            _add('travel_acceleration', 'Acceleration', int(travel_accel),
                 'info', None,
                 'Very high, but travel-only so no print quality impact. Fine if your frame handles it.')
        else:
            _add('travel_acceleration', 'Acceleration', int(travel_accel),
                 'good', None,
                 'Good travel accel. Fast repositioning without excessive frame stress.')

    if first_accel is not None:
        if first_accel > 5000:
            _add('initial_layer_acceleration', 'Acceleration', int(first_accel),
                 'warn', '1500\u20133000',
                 'First layer needs to stick. High accel shakes the nozzle and hurts adhesion.')
        elif first_accel < 500:
            _add('initial_layer_acceleration', 'Acceleration', int(first_accel),
                 'info', '1500\u20132000',
                 'Very slow first layer. Can safely increase for faster start.')
        else:
            _add('initial_layer_acceleration', 'Acceleration', int(first_accel),
                 'good', None,
                 'Good first layer accel. Gentle enough for adhesion.')

    # =====================================================================
    # HARDWARE VALIDATION — firmware limits & input shaper
    # =====================================================================
    fw_max_accel = (printer_hw or {}).get('firmware_max_accel')
    is_data = (printer_hw or {}).get('input_shaper', {})

    # Check if any slicer accel exceeds firmware max_accel
    if fw_max_accel:
        for accel_name, accel_val in [
            ('default_acceleration', default_accel),
            ('outer_wall_acceleration', outer_accel),
            ('inner_wall_acceleration', inner_accel),
            ('sparse_infill_acceleration', infill_accel),
            ('travel_acceleration', travel_accel),
        ]:
            if accel_val and accel_val > fw_max_accel:
                _add(accel_name, 'Firmware Limit', int(accel_val),
                     'bad', str(fw_max_accel),
                     f'Exceeds firmware max_accel ({fw_max_accel}). '
                     f'Klipper will silently clamp to {fw_max_accel} — '
                     f'this setting has no effect above that.')

    # Check if accel exceeds input shaper recommended limit (quality)
    # Axis-aware: on CoreXY, X and Y can have very different limits.
    # Wall features use arbitrary geometry → check against BOTH axes (min).
    # Infill features can be axis-aligned → check against EACH axis separately.
    if is_data and _shaper_info:
        _both_axes = len(_shaper_info) == 2

        # Classify features by motion pattern
        _wall_features = [
            ('outer_wall_acceleration', outer_accel),
            ('inner_wall_acceleration', inner_accel),
            ('bridge_acceleration', bridge_accel),
            ('top_surface_acceleration', top_accel),
        ]
        _infill_features = [
            ('internal_solid_infill_acceleration', solid_accel),
            ('sparse_infill_acceleration', infill_accel),
        ]

        # Wall features: move in arbitrary directions, limited by MOST restrictive axis
        for accel_name, accel_val in _wall_features:
            if accel_val and _shaper_quality_max and accel_val > _shaper_quality_max:
                feature = accel_name.replace('_acceleration', '').replace('_', ' ')
                if _both_axes and _shaper_perf_max and accel_val <= _shaper_perf_max:
                    # Exceeds one axis but not the other
                    slow_ax = min(_shaper_info, key=lambda a: _shaper_info[a]['limit'])
                    fast_ax = max(_shaper_info, key=lambda a: _shaper_info[a]['limit'])
                    slow_info = _shaper_info[slow_ax]
                    fast_info = _shaper_info[fast_ax]
                    _add(accel_name, 'Input Shaper', int(accel_val),
                         'warn', f'\u2264{int(slow_info["limit"])}',
                         f'Exceeds {slow_ax.upper()} axis shaper limit '
                         f'({slow_info["type"].upper()} @ {slow_info["freq"]}Hz = '
                         f'{int(slow_info["limit"])}) but within {fast_ax.upper()} axis '
                         f'({fast_info["type"].upper()} @ {fast_info["freq"]}Hz = '
                         f'{int(fast_info["limit"])}). '
                         f'Walls have mixed-axis moves — {feature} may show ringing '
                         f'on {slow_ax.upper()}-dominant segments.')
                else:
                    # Exceeds all axes
                    parts = []
                    for ax in sorted(_shaper_info):
                        si = _shaper_info[ax]
                        parts.append(f'{ax.upper()}: {si["type"].upper()} @ {si["freq"]}Hz = {int(si["limit"])}')
                    _add(accel_name, 'Input Shaper', int(accel_val),
                         'bad', f'\u2264{int(_shaper_quality_max)}',
                         f'Exceeds input shaper quality limit on ALL axes '
                         f'({", ".join(parts)}). '
                         f'Will cause visible ringing on {feature}.')

        # Infill features: can be axis-aligned, check per-axis
        for accel_name, accel_val in _infill_features:
            if accel_val and _shaper_quality_max and accel_val > _shaper_quality_max:
                feature = accel_name.replace('_acceleration', '').replace('_', ' ')
                if _both_axes and _shaper_perf_max and accel_val <= _shaper_perf_max:
                    # Within the faster axis — infill can be aligned to it
                    slow_ax = min(_shaper_info, key=lambda a: _shaper_info[a]['limit'])
                    fast_ax = max(_shaper_info, key=lambda a: _shaper_info[a]['limit'])
                    slow_info = _shaper_info[slow_ax]
                    fast_info = _shaper_info[fast_ax]
                    _add(accel_name, 'Input Shaper', int(accel_val),
                         'info', f'\u2264{int(fast_info["limit"])}',
                         f'Exceeds {slow_ax.upper()} axis limit '
                         f'({int(slow_info["limit"])}) but within {fast_ax.upper()} axis '
                         f'({int(fast_info["limit"])}). '
                         f'Infill patterns with {slow_ax.upper()}-dominant segments '
                         f'may show ringing. Rectilinear infill alternates axes, '
                         f'so some passes are fine.')
                elif _shaper_perf_max and accel_val > _shaper_perf_max:
                    # Exceeds all axes
                    parts = []
                    for ax in sorted(_shaper_info):
                        si = _shaper_info[ax]
                        parts.append(f'{ax.upper()}: {int(si["limit"])}')
                    _add(accel_name, 'Input Shaper', int(accel_val),
                         'warn', f'\u2264{int(_shaper_perf_max)}',
                         f'Exceeds input shaper quality limit on ALL axes '
                         f'({", ".join(parts)}). '
                         f'May cause visible ringing on {feature}.')
                else:
                    # Single-axis data or same limit
                    ax = list(_shaper_info.keys())[0]
                    si = _shaper_info[ax]
                    _add(accel_name, 'Input Shaper', int(accel_val),
                         'warn', f'\u2264{int(si["limit"])}',
                         f'Exceeds input shaper quality limit '
                         f'({si["type"].upper()} @ {si["freq"]}Hz on '
                         f'{ax.upper()} axis = {int(si["limit"])}). '
                         f'May cause visible ringing on {feature}.')

    # =====================================================================
    # FAN CAP WARNING — from hardware detection
    # =====================================================================
    fan_hw = (printer_hw or {}).get('part_fan', {})
    fan_max_power = fan_hw.get('max_power', 1.0)
    if fan_max_power < 1.0:
        pct = int(fan_max_power * 100)
        _add('part_cooling_fan', 'Hardware', f'{pct}% cap',
             'bad', '1.0 (100%)',
             f'Part cooling fan max_power is {fan_max_power} in firmware — '
             f'fan can never exceed {pct}%. This limits cooling capacity '
             f'and explains why Smart Cooling may not reach target speeds. '
             f'Set max_power: 1.0 in your [fan] config (adjust voltage if needed).')

    # =====================================================================
    # SPEED SETTINGS — with volumetric flow calculation
    # =====================================================================
    # (Hardware variables _shaper_quality_max, _practical_accel etc. defined above in accel section)

    def _optimal_speed(line_w, layer, quality_factor=0.85):
        """Compute the optimal speed for a feature based on hotend flow capacity,
        input shaper accel limits, and firmware velocity limit.

        Input shaper is the real constraint — you can't reach high speeds on
        short segments if accel is limited by the shaper."""
        if line_w <= 0 or layer <= 0:
            return None
        # Flow-limited speed
        flow_speed = safe_flow * quality_factor / (line_w * layer)
        # Firmware velocity ceiling
        vel_limit = _fw_max_vel * 0.9
        # Accel-limited practical speed: on a typical 20mm segment,
        # v_max = sqrt(2 * accel * distance).  Use the shaper quality
        # limit as the accel constraint.
        accel_speed = None
        if _practical_accel:
            # Typical segment length for the feature (shorter = more constrained)
            seg_len = 20  # mm — reasonable for wall segments
            accel_speed = (2 * _practical_accel * seg_len) ** 0.5
        candidates = [flow_speed, vel_limit]
        if accel_speed:
            candidates.append(accel_speed)
        return int(min(candidates))

    def _speed_advice(setting, category, speed, line_w, layer, feature_name,
                      min_ok=10, max_ok=300, quality_max=None, purpose=None):
        """Evaluate speed for a feature.
        purpose: None = speed-sensitive (suggest increases), or a string like
        'cooling', 'adhesion', 'precision' meaning speed is intentionally low
        for non-flow reasons — don't suggest increases."""
        if speed is None:
            return
        flow = _flow(speed, line_w, layer)
        fv = _flow_verdict(flow)
        if flow > peak_flow:
            max_safe_speed = int(safe_flow / (line_w * layer)) if line_w * layer > 0 else speed
            _add(setting, category, f'{int(speed)} mm/s', 'bad',
                 f'{max_safe_speed} mm/s',
                 f'{feature_name}: {flow} mm\u00b3/s exceeds Revo {variant} {nozzle_dia}mm '
                 f'{material} peak of {peak_flow} mm\u00b3/s (E3D data). '
                 f'Will cause under-extrusion.', flow)
        elif flow > safe_flow * 0.85:
            _add(setting, category, f'{int(speed)} mm/s', 'warn', None,
                 f'{feature_name}: {flow} mm\u00b3/s is near Revo {variant} {nozzle_dia}mm '
                 f'{material} safe limit of {safe_flow} mm\u00b3/s (E3D data). '
                 f'May work but leaves little headroom for the {wattage}W heater.', flow)
        elif quality_max and speed > quality_max:
            _add(setting, category, f'{int(speed)} mm/s', 'info',
                 f'{int(quality_max)} mm/s',
                 f'{feature_name}: speed is fine for flow ({flow} mm\u00b3/s) but '
                 f'higher speeds can reduce {feature_name.lower()} quality.', flow)
        elif speed < min_ok:
            _add(setting, category, f'{int(speed)} mm/s', 'info', None,
                 f'{feature_name}: very slow ({flow} mm\u00b3/s). Fine for quality, slow for time.', flow)
        elif purpose:
            # Speed is intentionally limited for non-flow reasons — don't suggest increases
            _add(setting, category, f'{int(speed)} mm/s', 'good', None,
                 f'{feature_name}: {flow} mm\u00b3/s ({int(flow / safe_flow * 100)}% of '
                 f'Revo {variant} capacity). Speed is {purpose}-limited \u2014 current value is appropriate.', flow)
        elif _is_fast_printer and flow < safe_flow * 0.65 and line_w > 0 and layer > 0:
            # Under-utilizing a fast printer — suggest speed increase
            # 65% threshold: on a corexy with a high-flow hotend, anything under
            # ~65% utilization is leaving significant time on the table
            optimal = _optimal_speed(line_w, layer, quality_factor=0.85)
            if optimal and speed < optimal * 0.70:
                # Significantly below optimal — suggest increase
                # quality_factor already reserves 15% headroom, so suggest
                # close to optimal.  Input shaper handles ringing.
                suggest_speed = int(optimal * 0.90)
                suggest_speed = max(suggest_speed, int(speed * 1.3))  # at least 30% increase
                suggest_speed = min(suggest_speed, _fw_max_vel)  # cap at firmware limit
                suggest_flow = _flow(suggest_speed, line_w, layer)
                _add(setting, category, f'{int(speed)} mm/s', 'warn',
                     f'{suggest_speed} mm/s',
                     f'{feature_name}: only {flow} mm\u00b3/s \u2014 '
                     f'{int(flow / safe_flow * 100)}% of your Revo {variant} capacity '
                     f'({safe_flow} mm\u00b3/s for {material}). '
                     f'Your {_kinematics} printer can handle {suggest_speed} mm/s '
                     f'({suggest_flow} mm\u00b3/s). Increase speed for faster prints '
                     f'without sacrificing quality.', flow)
            else:
                _add(setting, category, f'{int(speed)} mm/s', 'good', None,
                     f'{feature_name}: {flow} mm\u00b3/s \u2014 well within Revo {variant} '
                     f'{nozzle_dia}mm capacity ({safe_flow} mm\u00b3/s safe, E3D data).', flow)
        else:
            _add(setting, category, f'{int(speed)} mm/s', 'good', None,
                 f'{feature_name}: {flow} mm\u00b3/s \u2014 well within Revo {variant} '
                 f'{nozzle_dia}mm capacity ({safe_flow} mm\u00b3/s safe, E3D data).', flow)

    _speed_advice('outer_wall_speed', 'Speed',
                  slicer_settings.get('outer_wall_speed'), outer_w, layer_h,
                  'Outer wall', quality_max=250)
    _speed_advice('inner_wall_speed', 'Speed',
                  slicer_settings.get('inner_wall_speed'), inner_w, layer_h,
                  'Inner wall', quality_max=300)
    _speed_advice('bridge_speed', 'Speed',
                  slicer_settings.get('bridge_speed'), outer_w, layer_h,
                  'Bridge', max_ok=100, purpose='cooling')
    _speed_advice('sparse_infill_speed', 'Speed',
                  slicer_settings.get('sparse_infill_speed'), infill_w, layer_h,
                  'Sparse infill')
    _speed_advice('internal_solid_infill_speed', 'Speed',
                  slicer_settings.get('internal_solid_infill_speed'), infill_w, layer_h,
                  'Solid infill')
    _speed_advice('top_surface_speed', 'Speed',
                  slicer_settings.get('top_surface_speed'), top_w, layer_h,
                  'Top surface', quality_max=120, purpose='surface quality')
    _speed_advice('gap_infill_speed', 'Speed',
                  slicer_settings.get('gap_infill_speed'), outer_w, layer_h,
                  'Gap fill', purpose='precision')
    _speed_advice('initial_layer_speed', 'Speed',
                  slicer_settings.get('initial_layer_speed'), first_w, first_layer_h,
                  'First layer', purpose='adhesion')
    _speed_advice('internal_bridge_speed', 'Speed',
                  slicer_settings.get('internal_bridge_speed'), inner_w, layer_h,
                  'Internal bridge', purpose='cooling')
    _speed_advice('support_speed', 'Speed',
                  slicer_settings.get('support_speed'), infill_w, layer_h,
                  'Support')

    travel_speed = slicer_settings.get('travel_speed')
    if travel_speed is not None:
        if travel_speed > 500:
            _add('travel_speed', 'Speed', f'{int(travel_speed)} mm/s', 'info', None,
                 'Very fast travel. Fine if frame is rigid, but check for resonance on small parts.')
        elif travel_speed < 150:
            _add('travel_speed', 'Speed', f'{int(travel_speed)} mm/s', 'info',
                 '300\u2013500 mm/s',
                 'Slow travel wastes time and increases ooze at non-extruding moves.')
        else:
            _add('travel_speed', 'Speed', f'{int(travel_speed)} mm/s', 'good', None,
                 'Good travel speed. Fast repositioning without excessive frame stress.')

    # =====================================================================
    # QUALITY SETTINGS
    # =====================================================================
    bridge_flow = slicer_settings.get('bridge_flow')
    if bridge_flow is not None:
        if bridge_flow < 0.9:
            _add('bridge_flow', 'Quality', bridge_flow, 'warn', '1.0',
                 f'Under-extruding bridges by {(1 - bridge_flow) * 100:.0f}%. '
                 f'OrcaSlicer misidentifies recessed areas as bridges \u2014 set to 1.0.')
        elif bridge_flow > 1.1:
            _add('bridge_flow', 'Quality', bridge_flow, 'info', '1.0',
                 'Over-extruding on bridges. May cause drooping.')
        else:
            _add('bridge_flow', 'Quality', bridge_flow, 'good', None,
                 'Bridge flow normal. No under/over-extrusion.')

    wall_loops = slicer_settings.get('wall_loops')
    if wall_loops is not None:
        wl = int(wall_loops)
        if wl < 2:
            _add('wall_loops', 'Quality', wl, 'warn', '2\u20133',
                 'Single wall = weak part + infill pattern shows through. Use 2+ walls.')
        elif wl > 5:
            _add('wall_loops', 'Quality', wl, 'info', '2\u20134',
                 'Very thick walls. Probably unnecessary for most parts \u2014 eats print time.')
        else:
            _add('wall_loops', 'Quality', wl, 'good', None,
                 f'{wl} walls. Good structural strength without excessive time.')

    wall_seq = slicer_settings.get('wall_sequence')
    if wall_seq is not None:
        seq_str = str(wall_seq).lower()
        if 'outer' in seq_str and 'inner' in seq_str:
            if seq_str.index('outer') < seq_str.index('inner'):
                _add('wall_sequence', 'Quality', str(wall_seq), 'info', None,
                     'Outer wall first = better dimensional accuracy but inner wall can\'t '
                     'support overhangs. Best for calibration cubes and functional parts.')
            else:
                _add('wall_sequence', 'Quality', str(wall_seq), 'good', None,
                     'Inner wall first = better overhang support. Good default for most prints.')
        else:
            _add('wall_sequence', 'Quality', str(wall_seq), 'info', None,
                 f'Wall sequence: {wall_seq}')

    for i, angle in [(1, '25%'), (2, '50%'), (3, '75%'), (4, '100%')]:
        key = f'overhang_{i}_4_speed'
        val = slicer_settings.get(key)
        if val is not None:
            val_str = str(val)
            if '%' in val_str:
                pct = float(val_str.replace('%', ''))
                if pct > 80:
                    _add(key, 'Quality', val_str, 'info', f'{60 - (i * 10)}%',
                         f'{angle} overhang: speed too high \u2014 material sags before cooling. '
                         f'Slow down steep overhangs for better bridging.')
                else:
                    _add(key, 'Quality', val_str, 'good', None,
                         f'{angle} overhang: good slowdown for cooling time.')
            elif float(val) > 0:
                _add(key, 'Quality', f'{val} mm/s', 'info', None,
                     f'{angle} overhang at {val} mm/s.')

    small_peri = slicer_settings.get('small_perimeter_speed')
    if small_peri is not None:
        val_str = str(small_peri)
        if '%' in val_str:
            pct = float(val_str.replace('%', ''))
            if pct > 80:
                _add('small_perimeter_speed', 'Quality', val_str, 'info', '50\u201360%',
                     'Small perimeters need slow speed for dimensional accuracy (screw holes, pins).')
            else:
                _add('small_perimeter_speed', 'Quality', val_str, 'good', None,
                     'Good slowdown for small features.')
        elif float(small_peri) > 0:
            if float(small_peri) > 150:
                _add('small_perimeter_speed', 'Quality', f'{small_peri} mm/s', 'info',
                     '60\u2013100 mm/s',
                     'Small perimeters at this speed lose dimensional accuracy.')
            else:
                _add('small_perimeter_speed', 'Quality', f'{small_peri} mm/s', 'good', None,
                     'Good speed for small features.')

    fil_mvs = slicer_settings.get('filament_max_volumetric_speed')
    if fil_mvs is not None:
        fmvs = float(fil_mvs)
        if fmvs > peak_flow:
            _add('filament_max_volumetric_speed', 'Quality', f'{fmvs} mm\u00b3/s',
                 'bad', f'{safe_flow} mm\u00b3/s',
                 f'Slicer allows {fmvs} mm\u00b3/s but the Revo {variant} {nozzle_dia}mm '
                 f'can only do {peak_flow} peak for {material} (E3D data). '
                 f'Set to {safe_flow} for reliable prints.')
        elif fmvs > safe_flow:
            _add('filament_max_volumetric_speed', 'Quality', f'{fmvs} mm\u00b3/s',
                 'warn', f'{safe_flow} mm\u00b3/s',
                 f'Set to {fmvs} \u2014 above the Revo {variant} safe limit '
                 f'of {safe_flow} mm\u00b3/s for {material} (E3D data). '
                 f'This lets the slicer exceed what your hotend can handle.')
        elif fmvs < safe_flow * 0.5:
            _add('filament_max_volumetric_speed', 'Quality', f'{fmvs} mm\u00b3/s',
                 'warn', f'{safe_flow} mm\u00b3/s',
                 f'Set to {fmvs} \u2014 very conservative for the Revo {variant} '
                 f'(safe: {safe_flow} for {material}, E3D data). You\u2019re leaving speed on the table.')
        else:
            _add('filament_max_volumetric_speed', 'Quality', f'{fmvs} mm\u00b3/s',
                 'good', None,
                 f'Matches Revo {variant} safe limit ({safe_flow} mm\u00b3/s '
                 f'for {material}, E3D data). Good.')

    # =====================================================================
    # ACCEL UNIFORMITY SUMMARY
    # =====================================================================
    accel_vals = [_coerce_accel(v, _ref_accel) for k, v in slicer_settings.items()
                  if k in _SLICER_ACCEL_KEYS
                  and 'travel' not in k and 'initial' not in k]
    accel_vals = [v for v in accel_vals if v is not None and v > 0]
    if len(accel_vals) >= 3:
        spread = max(accel_vals) - min(accel_vals)
        max_accel_val = max(accel_vals)
        # With input shaper, the real question is whether accels exceed the
        # shaper quality limit, not whether they differ from each other.
        if _shaper_quality_max and max_accel_val <= _shaper_quality_max:
            _add('_accel_spread', 'Summary', f'\u00b1{int(spread)}',
                 'good', None,
                 f'All print accels within input shaper quality limit '
                 f'({_shaper_quality_max}). '
                 f'Spread of \u00b1{int(spread)} is fine \u2014 input shaper handles transitions.')
        elif _shaper_quality_max and max_accel_val > _shaper_quality_max:
            over_min = [v for v in accel_vals if v > _shaper_quality_max]
            # Axis-aware: distinguish between exceeding one vs both axes
            if _shaper_perf_max and _shaper_perf_max > _shaper_quality_max:
                over_both = [v for v in accel_vals if v > _shaper_perf_max]
                over_one = [v for v in over_min if v <= _shaper_perf_max]
                axis_parts = []
                for ax in sorted(_shaper_info):
                    si = _shaper_info[ax]
                    axis_parts.append(f'{ax.upper()}: {si["type"].upper()} @ '
                                      f'{si["freq"]}Hz = {int(si["limit"])}')
                axis_str = ', '.join(axis_parts)
                if over_both:
                    _add('_accel_spread', 'Summary',
                         f'{len(over_min)} over limit',
                         'warn', f'\u2264{int(_shaper_quality_max)}',
                         f'{len(over_both)} feature accel(s) exceed ALL shaper limits '
                         f'({axis_str}). '
                         f'{len(over_one)} more exceed the {min(_shaper_info, key=lambda a: _shaper_info[a]["limit"]).upper()} '
                         f'axis only. Reduce to \u2264{int(_shaper_quality_max)} for '
                         f'clean surfaces on all axes.')
                else:
                    slow_ax = min(_shaper_info, key=lambda a: _shaper_info[a]['limit'])
                    _add('_accel_spread', 'Summary',
                         f'{len(over_one)} over {slow_ax.upper()} limit',
                         'warn', f'\u2264{int(_shaper_quality_max)}',
                         f'{len(over_one)} feature accel(s) exceed {slow_ax.upper()} axis '
                         f'shaper limit ({int(_shaper_quality_max)}) but all are within '
                         f'{max(_shaper_info, key=lambda a: _shaper_info[a]["limit"]).upper()} '
                         f'axis ({int(_shaper_perf_max)}). '
                         f'Axis-aligned infill passes are fine; wall/diagonal moves '
                         f'may show ringing. ({axis_str})')
            else:
                _add('_accel_spread', 'Summary', f'{len(over_min)} over limit',
                     'warn', f'\u2264{int(_shaper_quality_max)}',
                     f'{len(over_min)} feature accel(s) exceed input shaper quality limit '
                     f'({int(_shaper_quality_max)}). '
                     f'May cause visible ringing on those features. '
                     f'Reduce to \u2264{int(_shaper_quality_max)} for clean surfaces.')
        elif spread <= MAX_ACCEL_GAP:
            _add('_accel_spread', 'Summary', f'\u00b1{int(spread)}',
                 'good', None,
                 f'All print accelerations within \u00b1{int(spread)} of each other. '
                 f'Minimal banding risk from accel transitions.')
        else:
            _add('_accel_spread', 'Summary', f'\u00b1{int(spread)}',
                 'info', f'Within \u00b1{MAX_ACCEL_GAP}',
                 f'Acceleration spread of \u00b1{int(spread)} across features. '
                 f'Large spreads can cause visible transitions between features. '
                 f'Consider narrowing the range if you see banding at feature boundaries.')

    all_flows = [a.get('flow_mm3s', 0) for a in advice if a.get('flow_mm3s')]
    if all_flows:
        peak_actual = max(all_flows)
        headroom = safe_flow - peak_actual
        if peak_actual > peak_flow:
            _add('_flow_headroom', 'Summary', f'{peak_actual} mm\u00b3/s peak',
                 'bad', f'Reduce speed or switch to Revo HF' if variant == 'SF' else 'Reduce speed',
                 f'Peak flow ({peak_actual} mm\u00b3/s) exceeds Revo {variant} {nozzle_dia}mm '
                 f'{material} peak of {peak_flow} mm\u00b3/s (E3D data). '
                 f'You will get under-extrusion.')
        elif headroom < 0:
            _add('_flow_headroom', 'Summary', f'{peak_actual} mm\u00b3/s peak',
                 'warn', None,
                 f'Peak flow ({peak_actual} mm\u00b3/s) exceeds the Revo {variant} '
                 f'safe limit of {safe_flow} mm\u00b3/s but within burst peak '
                 f'({peak_flow}). Short bursts OK, sustained sections may struggle.')
        elif headroom < 3:
            _add('_flow_headroom', 'Summary', f'{peak_actual} mm\u00b3/s peak',
                 'warn', None,
                 f'Only {headroom:.1f} mm\u00b3/s headroom below Revo {variant} safe limit '
                 f'({safe_flow} mm\u00b3/s, E3D data). The adaptive flow system needs room '
                 f'to boost \u2014 consider slowing infill by 10\u201315%.')
        else:
            _add('_flow_headroom', 'Summary', f'{peak_actual} mm\u00b3/s peak',
                 'good', None,
                 f'{headroom:.1f} mm\u00b3/s headroom below Revo {variant} safe limit '
                 f'({safe_flow} mm\u00b3/s for {material}, E3D data). '
                 f'Plenty of room for adaptive flow adjustments.')

    # =====================================================================
    # PRINTER UTILIZATION SUMMARY — how much of the hardware is being used
    # =====================================================================
    if _is_fast_printer and all_flows:
        peak_actual = max(all_flows)
        flow_utilization = peak_actual / safe_flow * 100 if safe_flow > 0 else 0

        # Compute what optimal speeds would achieve
        optimal_outer = _optimal_speed(outer_w, layer_h, 0.75) or 200
        optimal_inner = _optimal_speed(inner_w, layer_h, 0.85) or 250
        optimal_infill = _optimal_speed(infill_w, layer_h, 0.90) or 300

        if flow_utilization < 35:
            # Build the "optimized profile" summary
            profile_lines = []
            if slicer_settings.get('outer_wall_speed') and slicer_settings['outer_wall_speed'] < optimal_outer * 0.7:
                profile_lines.append(f'outer_wall_speed: {optimal_outer}')
            if slicer_settings.get('inner_wall_speed') and slicer_settings['inner_wall_speed'] < optimal_inner * 0.7:
                profile_lines.append(f'inner_wall_speed: {optimal_inner}')
            if slicer_settings.get('sparse_infill_speed') and slicer_settings['sparse_infill_speed'] < optimal_infill * 0.7:
                profile_lines.append(f'sparse_infill_speed: {optimal_infill}')
            if slicer_settings.get('internal_solid_infill_speed') and slicer_settings['internal_solid_infill_speed'] < optimal_infill * 0.7:
                profile_lines.append(f'internal_solid_infill_speed: {optimal_infill}')

            profile_str = ', '.join(profile_lines) if profile_lines else 'See individual speed suggestions above'
            _add('_printer_utilization', 'Performance', f'{flow_utilization:.0f}% flow utilization',
                 'bad', 'See optimized values below',
                 f'Your {_kinematics.upper()} printer with Revo {variant} ({safe_flow} mm\u00b3/s safe) '
                 f'is only using {flow_utilization:.0f}% of its flow capacity. '
                 f'Peak flow was {peak_actual} mm\u00b3/s but the hotend can safely sustain {safe_flow}. '
                 f'Optimized profile: {profile_str}. '
                 f'This could cut your print time significantly.')
        elif flow_utilization < 55:
            _add('_printer_utilization', 'Performance', f'{flow_utilization:.0f}% flow utilization',
                 'warn', None,
                 f'Your {_kinematics.upper()} printer is using {flow_utilization:.0f}% of Revo {variant} '
                 f'flow capacity ({peak_actual}/{safe_flow} mm\u00b3/s). '
                 f'There\u2019s room to increase speeds \u2014 see individual suggestions above.')
        elif flow_utilization < 85:
            _add('_printer_utilization', 'Performance', f'{flow_utilization:.0f}% flow utilization',
                 'good', None,
                 f'Good balance of speed and safety. Using {flow_utilization:.0f}% of Revo {variant} '
                 f'capacity ({peak_actual}/{safe_flow} mm\u00b3/s) with headroom for adaptive flow.')
        else:
            _add('_printer_utilization', 'Performance', f'{flow_utilization:.0f}% flow utilization',
                 'info', None,
                 f'Running near Revo {variant} capacity ({flow_utilization:.0f}%: '
                 f'{peak_actual}/{safe_flow} mm\u00b3/s). '
                 f'Adaptive flow has limited room to boost. Consider reducing speed slightly.')

    return advice


# =============================================================================
# E3D REVO HOTEND REFERENCE DATA
# =============================================================================
_E3D_REVO_FLOW = {
    0.4: {
        'HF': {
            'PLA':  {'safe': 24, 'peak': 28},
            'PETG': {'safe': 18, 'peak': 22},
            'ABS':  {'safe': 20, 'peak': 24},
            'ASA':  {'safe': 20, 'peak': 24},
            'TPU':  {'safe': 8,  'peak': 11},
        },
        'SF': {
            'PLA':  {'safe': 11, 'peak': 14},
            'PETG': {'safe': 9,  'peak': 12},
            'ABS':  {'safe': 10, 'peak': 13},
            'ASA':  {'safe': 10, 'peak': 13},
            'TPU':  {'safe': 5,  'peak': 7},
        },
    },
    0.6: {
        'HF': {
            'PLA':  {'safe': 35, 'peak': 40},
            'PETG': {'safe': 28, 'peak': 33},
            'ABS':  {'safe': 30, 'peak': 35},
            'ASA':  {'safe': 30, 'peak': 35},
            'TPU':  {'safe': 12, 'peak': 16},
        },
        'SF': {
            'PLA':  {'safe': 18, 'peak': 22},
            'PETG': {'safe': 14, 'peak': 18},
            'ABS':  {'safe': 16, 'peak': 20},
            'ASA':  {'safe': 16, 'peak': 20},
            'TPU':  {'safe': 8,  'peak': 10},
        },
    },
}

_REVO_HEATER_WATTAGE = {40: 'standard', 60: 'high-power'}


def _get_revo_variant():
    """Get the Revo variant (HF/SF) from adaptive flow config."""
    val = _get_config_value('use_high_flow_nozzle')
    if val is not None:
        return 'HF' if val else 'SF'
    return 'HF'


def _get_revo_flow_limit(nozzle_dia, variant, material):
    """Look up E3D Revo flow limits for given nozzle/variant/material."""
    known_sizes = sorted(_E3D_REVO_FLOW.keys())
    closest = min(known_sizes, key=lambda s: abs(s - nozzle_dia))
    nozzle_data = _E3D_REVO_FLOW.get(closest, {})
    variant_data = nozzle_data.get(variant, nozzle_data.get('HF', {}))
    mat_upper = (material or 'PLA').strip().upper()
    for mat_key in variant_data:
        if mat_upper == mat_key or mat_upper.startswith(mat_key):
            return variant_data[mat_key]
    return variant_data.get('PLA', {'safe': 15, 'peak': 20})



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

        detail_text = (f'Increase speeds by ~{speed_increase_pct}% — avg flow rises from '
                       f'{avg_flow:.1f} to ~{new_avg_flow} mm³/s '
                       f'(safe limit: {safe_flow} mm³/s).')
        if speed_lines:
            detail_text += '\n' + '  •  '.join([''] + speed_lines)

        suggestions.append({
            'what': 'Increase print speeds',
            'detail': detail_text,
            'impact': 'faster prints',
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

        # Severity depends on whether there are actual banding events
        if high_risk > 20:
            sev = 'warn'
        elif high_risk > 5:
            sev = 'info'
        else:
            sev = 'info'

        recs.append({
            'severity': sev, 'category': 'Slicer',
            'title': f'{len(slicer_issues)} slicer setting issue{"s" if len(slicer_issues) != 1 else ""} found',
            'detail': issue_details,
            'action': action_text,
        })
    elif data.get('slicer_settings') and high_risk <= 5:
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

    # --- Hardware-aware recommendations ---
    printer_hw = data.get('printer_hw') or {}

    # Fan cap warning
    fan_hw = printer_hw.get('part_fan', {})
    fan_max = fan_hw.get('max_power', 1.0)
    if fan_max < 1.0:
        pct = int(fan_max * 100)
        recs.append({
            'severity': 'bad', 'category': 'Hardware',
            'title': f'Part cooling fan capped at {pct}%',
            'detail': f'Your [fan] config has max_power: {fan_max}. The fan can never exceed {pct}%, '
                       f'which limits Smart Cooling and may cause overheating on PLA/PETG overhangs.',
            'action': f'Set max_power: 1.0 in your [fan] section (typically btt.cfg). '
                       f'If your fan is too strong at 100%, use the slicer or Smart Cooling to limit it.',
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

        if avg_utilization < 30:
            # Compute what optimized speeds could look like
            nozzle_dia_val = hotend_data.get('nozzle_diameter', 0.4)
            layer_h_val = 0.2  # standard
            line_w_val = nozzle_dia_val + 0.05
            optimal_infill = int(safe_flow_val * 0.85 / (line_w_val * layer_h_val)) if line_w_val * layer_h_val > 0 else 200
            optimal_wall = int(safe_flow_val * 0.65 / (line_w_val * layer_h_val)) if line_w_val * layer_h_val > 0 else 150

            # Estimate potential time savings
            speed_ratio = safe_flow_val * 0.70 / avg_flow_val if avg_flow_val > 0 else 2.0
            duration = s.get('duration_min', 0)
            estimated_new = duration / speed_ratio if speed_ratio > 0 else duration
            time_saved = duration - estimated_new

            # Compute recommended accel with shaper awareness
            _shaper_y_accel = is_data.get("y", {}).get("recommended_max_accel", 0)
            if _shaper_y_accel:
                _rec_accel = int(min(_shaper_y_accel, fw_max_accel or 20000) * 0.85)
                _accel_note = ' (based on your input shaper)'
            else:
                _rec_accel = int((fw_max_accel or 5000) * 0.85)
                _accel_note = ''

            recs.append({
                'severity': 'bad', 'category': 'Performance',
                'title': f'Printer at {avg_utilization:.0f}% capacity \u2014 printing far too slow',
                'detail': f'Your {kinematics.upper()} printer with Revo '
                           f'{hotend_data.get("nozzle_type", "HF")} averaged only '
                           f'{avg_flow_val:.1f} mm\u00b3/s (safe limit: {safe_flow_val}). '
                           f'Firmware supports {fw_max_accel or "high"} max accel and '
                           f'{printer_hw.get("firmware_max_velocity", 500)} mm/s max velocity, '
                           f'but the slicer profile is barely using any of it.'
                           + (f' Estimated time savings with optimized speeds: ~{time_saved:.0f} min '
                              f'({duration:.0f} \u2192 ~{estimated_new:.0f} min).' if time_saved > 2 else ''),
                'action': f'In your slicer, update speeds: outer wall \u2192 {optimal_wall} mm/s, '
                           f'inner wall \u2192 {int(optimal_wall * 1.5)} mm/s, '
                           f'infill \u2192 {optimal_infill} mm/s. '
                           f'Set all print accelerations to {_rec_accel}{_accel_note}. '
                           f'Travel accel \u2192 {min(fw_max_accel or 15000, 15000)}. '
                           f'See the Slicer Profile tab for per-setting details.',
            })
        # 35-50% utilization: slicer profile tab already has per-setting
        # suggestions, so no need for a separate generic warning here.

    # --- Boost optimization insights (from actual print data) ---
    bopt = data.get('boost_optimization')
    if bopt and bopt.get('verdict'):
        v = bopt['verdict']
        increase = bopt.get('speed_increase_pct', 0)
        suggestions = bopt.get('suggestions', [])
        can_increase = bopt.get('can_increase', [])

        if v == 'significant_headroom' and increase >= 25:
            detail_parts = [bopt['verdict_text']]
            for ci in can_increase[:3]:
                detail_parts.append(f"• {ci['aspect']}: {ci['headroom']}")
            action_parts = []
            for sg in suggestions[:3]:
                action_parts.append(f"{sg['what']}: {sg['detail']}")
            rec = {
                'severity': 'info', 'category': 'Optimization',
                'title': f'Room to go ~{increase}% faster (based on actual print data)',
                'detail': '\n'.join(detail_parts),
                'action': '\n'.join(action_parts) if action_parts else 'See the Slicer Profile tab for per-setting suggestions.',
            }
            # If flow_k increase is suggested, add config change
            flow_k_sug = [sg for sg in suggestions if sg.get('config_var') == 'flow_k']
            if flow_k_sug:
                chg = _suggest_change('flow_k', 'increase', 0.15, material=material, maximum=2.5)
                if chg:
                    rec['config_changes'] = [chg]
            recs.append(rec)

        elif v == 'moderate_headroom' and increase >= 10:
            detail_parts = [bopt['verdict_text']]
            for ci in can_increase[:2]:
                detail_parts.append(f"• {ci['aspect']}: {ci['headroom']}")
            recs.append({
                'severity': 'info', 'category': 'Optimization',
                'title': f'Moderate room to optimize (~{increase}% headroom)',
                'detail': '\n'.join(detail_parts),
                'action': 'Check the Slicer Profile tab for specific speed/accel suggestions based on your hardware limits.',
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

    # --- Vibration-based recommendations ---
    vib = data.get('vibration')
    if vib and vib.get('summary'):
        vs = vib['summary']
        vib_score = vs.get('quality_score')
        mag_avg = vs.get('mag_rms_avg', 0)

        # Overall vibration quality score
        if vib_score is not None:
            if vib_score < 40:
                recs.append({
                    'severity': 'bad', 'category': 'Vibration',
                    'title': f'Vibration quality score: {vib_score}/100',
                    'detail': f'Overall vibration quality is poor (avg magnitude RMS: {mag_avg} mm/s\u00b2). '
                               f'This likely causes visible surface artifacts and reduced print quality.',
                    'action': 'Check belt tension, tighten eccentric nuts, and verify all frame bolts. '
                              'See the Vibration tab for per-feature breakdown and specific accel reductions.',
                })
            elif vib_score < 65:
                recs.append({
                    'severity': 'warn', 'category': 'Vibration',
                    'title': f'Vibration quality score: {vib_score}/100',
                    'detail': f'Moderate vibration detected (avg magnitude RMS: {mag_avg} mm/s\u00b2). '
                               f'May cause minor surface artifacts on smooth surfaces.',
                    'action': 'Check the Vibration tab — features with high vibration can be improved '
                              'by reducing their acceleration. Specific values are suggested per-feature.',
                })
            elif vib_score >= 80:
                recs.append({
                    'severity': 'good', 'category': 'Vibration',
                    'title': f'Vibration quality score: {vib_score}/100',
                    'detail': f'Low vibration across all features (avg magnitude RMS: {mag_avg} mm/s\u00b2). '
                               f'The printer is mechanically well-tuned.',
                    'action': 'No changes needed. Vibration levels are healthy.',
                })

        # Per-feature accel reduction recommendations
        by_accel = vib.get('by_accel', {})
        reduce_features = []
        for accel_str, feat_data in by_accel.items():
            rec_data = feat_data.get('recommendation', {})
            if rec_data.get('action') == 'reduce':
                reduce_features.append({
                    'accel': accel_str,
                    'suggested': rec_data['suggested_accel'],
                    'reason': rec_data['reason'],
                    'gcode': rec_data['gcode'],
                })

        if reduce_features:
            gcode_lines = [f"{rf['gcode']}  ; was {rf['accel']}" for rf in reduce_features[:5]]
            recs.append({
                'severity': 'warn', 'category': 'Vibration',
                'title': f'{len(reduce_features)} feature(s) would benefit from lower acceleration',
                'detail': 'These features showed vibration significantly above the baseline. '
                          'Reducing their acceleration will improve surface quality:\n' +
                          '\n'.join(f"\u2022 Accel {rf['accel']}: {rf['reason']}" for rf in reduce_features[:5]),
                'action': 'Apply these in your slicer or via gcode:\n' + '\n'.join(gcode_lines),
            })

    # Vibration-banding correlations
    vib_banding = data.get('vibration_banding', [])
    if vib_banding:
        strong = [c for c in vib_banding if c['vibration_rms'] > 200]
        if strong:
            detail_lines = []
            for c in strong[:5]:
                detail_lines.append(
                    f"\u2022 Z={c['z_height']}mm: banding risk {c['banding_risk']}, "
                    f"vibration {c['vibration_rms']} RMS @ accel {c['vibration_accel']} \u2014 {c['probable_cause']}"
                )
            recs.append({
                'severity': 'warn', 'category': 'Vibration',
                'title': f'{len(strong)} banding event(s) confirmed by vibration data',
                'detail': 'ADXL data correlates with banding events at these Z-heights, '
                          'providing evidence for the mechanical cause:\n' + '\n'.join(detail_lines),
                'action': 'Focus on the specific accels and heights listed above. '
                          'Reducing accel for these features will address the root cause.',
            })
        elif vib_banding:
            recs.append({
                'severity': 'info', 'category': 'Vibration',
                'title': f'{len(vib_banding)} banding event(s) cross-referenced with vibration data',
                'detail': 'Banding events were found near ADXL sample points, but vibration was '
                          'moderate — the banding may be thermal or PA-related rather than mechanical.',
                'action': 'Check thermal lag and PA stability tabs for other causes.',
            })

    # --- All good ---
    if not recs or all(r['severity'] == 'good' for r in recs):
        recs.append({
            'severity': 'good', 'category': 'Overall',
            'title': 'Print looks well-tuned',
            'detail': 'No significant issues detected across heater, thermal lag, PA, banding, or vibration analysis.',
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
        'boost_optimization': None,  # not available for aggregate view
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
                # Look for vibration score for this session
            vib_score = None
            try:
                base_name = os.path.basename(s.get('summary_file', '')).replace('_summary.json', '')
                vib_files = glob.glob(os.path.join(os.path.expanduser(log_dir), base_name + '*_vibration.json'))
                if not vib_files:
                    vib_files = glob.glob(os.path.join(os.path.expanduser(log_dir), '*_vibration.json'))
                for vf in sorted(vib_files, reverse=True):
                    if base_name and base_name in os.path.basename(vf):
                        with open(vf, 'r') as fv:
                            vd = json.load(fv)
                            vib_score = (vd.get('summary') or {}).get('quality_score')
                            # Retroactively compute score if missing
                            if vib_score is None:
                                vs = vd.get('summary') or {}
                                ba = vd.get('by_accel') or {}
                                if vs:
                                    try:
                                        vib_score, _ = _compute_vibration_score(vs, ba)
                                    except Exception:
                                        pass
                        break
            except Exception:
                pass

            trend_data.append({
                'date': ts[:10] if len(ts) >= 10 else ts,
                'material': sm.get('material', ''),
                'avg_boost': sm.get('avg_boost', 0),
                'max_boost': sm.get('max_boost', 0),
                'avg_pwm': sm.get('avg_pwm', 0),
                'max_pwm': sm.get('max_pwm', 0),
                'high_risk': ba.get('high_risk_events', 0),
                'culprit': ba.get('likely_culprit', '-'),
                'vib_score': vib_score,
            })
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
        wattage = _af_val('sc_heater_wattage')
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

    # --- Comprehensive per-setting profile advice ---
    profile_advice = None
    if slicer and hotend_info:
        profile_advice = generate_slicer_profile_advice(
            slicer, hotend_info,
            print_summary=data.get('summary'),
            printer_hw=printer_hw,
        )
    data['slicer_profile_advice'] = profile_advice

    # --- Boost optimization analysis — "can I go faster?" ---
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

    # --- Vibration data from ADXL auto-sampler ---
    vibration_data = None
    try:
        # Look for vibration JSON matching this print
        if data.get('selected_file'):
            base = data['selected_file'].replace('_summary.json', '').replace('.csv', '')
            vib_candidates = glob.glob(os.path.join(os.path.expanduser(log_dir), '*_vibration.json'))
            for vc in sorted(vib_candidates, reverse=True):
                if base in os.path.basename(vc):
                    with open(vc, 'r') as f:
                        vibration_data = json.load(f)
                    break
            # Fall back to most recent vibration file
            if vibration_data is None and vib_candidates:
                with open(sorted(vib_candidates, reverse=True)[0], 'r') as f:
                    vibration_data = json.load(f)
    except Exception:
        pass

    # --- Retroactively compute vibration scores if missing ---
    if vibration_data:
        summary_v = vibration_data.get('summary') or {}
        by_accel_v = vibration_data.get('by_accel') or {}
        if summary_v.get('quality_score') is None and summary_v:
            try:
                score, breakdown = _compute_vibration_score(summary_v, by_accel_v)
                summary_v['quality_score'] = score
                summary_v['score_breakdown'] = breakdown
                vibration_data['summary'] = summary_v
            except Exception:
                pass
        # Retroactively compute per-accel recommendations if missing
        has_recs = any(v.get('recommendation') not in (None, '') for v in by_accel_v.values()) if by_accel_v else True
        if not has_recs and by_accel_v:
            try:
                samples_v = vibration_data.get('samples') or []
                recs = _compute_accel_recommendations(by_accel_v, samples_v)
                for accel_val in recs:
                    if accel_val in by_accel_v:
                        by_accel_v[accel_val]['recommendation'] = recs[accel_val]
                vibration_data['by_accel'] = by_accel_v
            except Exception:
                pass

    data['vibration'] = vibration_data

    # --- Cross-reference vibration with banding ---
    vib_banding_corr = []
    if vibration_data:
        banding_csv = data.get('banding_csv_analysis')
        if not banding_csv and csv_path:
            banding_csv = analyze_csv_for_banding(csv_path)
        if banding_csv:
            vib_banding_corr = _correlate_vibration_with_banding(vibration_data, banding_csv)
    data['vibration_banding'] = vib_banding_corr

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
.box h3{font-size:14px;margin-bottom:4px;color:#8b949e}
.box-desc{font-size:11px;color:#484f58;margin-bottom:10px;line-height:1.4}
.box-desc .good{color:#3fb950}.box-desc .warn{color:#d29922}.box-desc .bad{color:#f85149}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.tab-wrap{position:relative;display:inline-flex;align-items:center;gap:4px}
.tab-tip{display:inline-flex;align-items:center;justify-content:center;
width:13px;height:13px;border-radius:50%;background:#30363d;color:#484f58;
font-size:8px;cursor:help;flex-shrink:0}
.tab-tip:hover::after{content:attr(data-tip);position:absolute;top:calc(100% + 8px);
left:50%;transform:translateX(-50%);background:#1c2128;color:#c9d1d9;border:1px solid #30363d;
border-radius:6px;padding:8px 12px;font-size:11px;line-height:1.4;white-space:normal;
width:260px;z-index:10;text-transform:none;letter-spacing:0;font-weight:400;
box-shadow:0 4px 12px rgba(0,0,0,.4)}
.tab-tip:hover::before{content:'';position:absolute;top:calc(100% + 4px);
left:50%;transform:translateX(-50%);border:4px solid transparent;
border-bottom-color:#30363d;z-index:11}
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
d:'Samples flagged as banding risk from rapid temp/PA/accel changes. \u2022 0\u20135 = excellent \u2022 5\u201320 = minor, unlikely visible \u2022 20\u201350 = moderate, check print quality \u2022 50+ = high, likely visible banding'}];
var vb=D.vibration;if(vb&&vb.summary&&vb.summary.quality_score!=null){
var vs=vb.summary.quality_score,vc=vs>=80?'#3fb950':vs>=50?'#d29922':'#f85149';
items.push({l:'Vib Score',v:vs+'/100',s:'ADXL quality',w:vs<50,
d:'Vibration quality score from ADXL auto-sampling. \u2022 80-100 = excellent \u2022 50-79 = moderate, some features noisy \u2022 <50 = poor, check belts and reduce accel'})}}
c.innerHTML=items.map(function(x){return '<div class="cd"><div class="lb">'+
x.l+(x.d?'<span class="tip" data-tip="'+x.d+'">?</span>':'')+
'</div><div class="vl'+(x.w?' w':'')+'">'+x.v+
'</div><div class="sb">'+x.s+'</div></div>'}).join('')}
rc();

var allTabs=[
{id:'rx',l:'\u2699 Recommendations',tip:'Actionable suggestions to improve print quality. Start here.'},
{id:'sl',l:'\u2702 Slicer',tip:'Shows slicer settings extracted from your G-code file. Cross-references acceleration values with banding data to identify specific settings causing issues.'},
{id:'tl',l:'Timeline',tip:'Real-time temperature, flow rate and speed plotted over the entire print. See how your heater responds to flow demands.'},
{id:'zh',l:'Z-Height',tip:'Shows which layers had the most thermal stress. Tall bars = layers where banding is most likely.'},
{id:'ht',l:'Heater',tip:'Is your heater keeping up? Shows power usage at different flow rates. Bars near 100% mean the heater is maxed out.'},
{id:'pa',l:'PA',tip:'Pressure Advance value over time. A flat line means stable extrusion. Wobbling means the system is hunting.'},
{id:'dz',l:'DynZ',tip:'Dynamic Z-offset adjustments for first layers and overhangs. Shows where acceleration was reduced to protect quality.'},
{id:'ds',l:'Distribution',tip:'How your print spent its time across different speeds and flow rates. Helps identify if you are pushing too hard.'},
{id:'tr',l:'Trends',tip:'Compare prints over time. Are things getting better or worse? Shows boost, PWM and risk across multiple prints.'},
{id:'vb',l:'Vibration',tip:'ADXL vibration analysis from auto-sampling during prints. Shows per-feature vibration, dominant frequencies, and quality recommendations.'}];
var at='rx',tb=document.getElementById('tb'),ca=document.getElementById('ca');
function buildTabs(){
var tabs=allTabs;
if(isAgg)tabs=allTabs.filter(function(t){return t.id!=='tl'&&t.id!=='sl'});
if(isAgg&&(at==='tl'||at==='sl'))at='rx';
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
if(at==='rx')rRec();
else if(at==='sl')rSlicer();
else if(at==='tl')rTimeline(tl);
else if(at==='zh')rZH();
else if(at==='ht')rHt();
else if(at==='pa')rPA(tl);
else if(at==='dz')rDZ();
else if(at==='ds')rDist();
else if(at==='tr')rTr()
else if(at==='vb')rVib()}

function rSlicer(){
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

/* --- Material & file header --- */
h+='<div class="sl-hdr"><span class="sl-mat"><span class="sl-icon">\u25cf</span>'+mat+'</span>';
if(fname)h+='<span class="sl-file">'+fname+'</span>';
h+='</div>';

/* --- Hotend info (compact one-liner) --- */
if(hi){
var sfLabel=hi.safe_flow||hi.max_safe_flow||'?';
var pkLabel=hi.peak_flow||'?';
var ndLabel=hi.nozzle_diameter||'0.4';
h+='<div class="box" style="padding:10px 16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">'+
'<span style="background:linear-gradient(135deg,#1f6feb,#58a6ff);border-radius:8px;padding:6px 12px;font-weight:700;color:#fff;font-size:14px">Revo '+(hi.nozzle_type||'HF')+'</span>'+
'<span style="color:#e6edf3;font-size:13px">E3D Revo '+(hi.nozzle_type||'HF')+' '+ndLabel+'mm \u2022 '+(hi.heater_wattage||'?')+'W \u2022 '+mat+'</span>'+
'<span style="color:#8b949e;font-size:12px">Safe: <b style="color:#3fb950">'+sfLabel+'</b> \u2022 Peak: <b style="color:#d29922">'+pkLabel+'</b> mm\u00b3/s</span>'+
'</div>'}

/* --- Printer Hardware panel --- */
var phw=D.printer_hw;
if(phw&&Object.keys(phw).length>0){
var ext=phw.extruder||{};
var fan=phw.part_fan||{};
var is_hw=phw.input_shaper||{};
var isx=is_hw.x||{};
var isy=is_hw.y||{};
var fanPct=fan.max_power!==undefined?Math.round(fan.max_power*100):100;
var fanClr=fanPct<100?'#f85149':'#3fb950';
h+='<div class="box" style="padding:12px 16px">';
h+='<div style="font-weight:700;font-size:13px;color:#58a6ff;margin-bottom:8px">\ud83d\udd27 Detected Printer Hardware</div>';
/* Compute practical limits from input shaper (the real constraint) */
var shaperMinAccel=Math.min(isx.recommended_max_accel||99999,isy.recommended_max_accel||99999);
var shaperMaxAccel=Math.max(isx.recommended_max_accel||0,isy.recommended_max_accel||0);
var hasShaper=shaperMinAccel<99999;
var practicalAccel=hasShaper?shaperMinAccel:phw.firmware_max_accel;
h+='<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px;font-size:12px">';
if(phw.kinematics)h+='<div><span style="color:#8b949e">Kinematics:</span> <b>'+phw.kinematics.toUpperCase()+'</b></div>';
if(phw.build_volume)h+='<div><span style="color:#8b949e">Build:</span> <b>'+phw.build_volume.join('\u00d7')+'mm</b></div>';
if(hasShaper){
h+='<div><span style="color:#8b949e">Quality Max Accel:</span> <b style="color:#3fb950">'+shaperMinAccel+'</b> <span style="color:#484f58">(Y limit)</span></div>';
h+='<div><span style="color:#8b949e">Input Shaper:</span> <b>X: '+(isx.type||'?').toUpperCase()+'@'+(isx.freq||'?')+'Hz ('+isx.recommended_max_accel+') / Y: '+(isy.type||'?').toUpperCase()+'@'+(isy.freq||'?')+'Hz ('+isy.recommended_max_accel+')</b></div>';
}
if(phw.firmware_max_accel)h+='<div><span style="color:#8b949e">Firmware Ceiling:</span> <span style="color:#484f58">'+phw.firmware_max_accel+' accel / '+(phw.firmware_max_velocity||'?')+' mm/s</span></div>';
if(ext.drive_type)h+='<div><span style="color:#8b949e">Extruder:</span> <b>'+ext.drive_type+'</b> (rot_dist: '+ext.rotation_distance+')</div>';
if(ext.nozzle_diameter)h+='<div><span style="color:#8b949e">Nozzle:</span> <b>'+ext.nozzle_diameter+'mm</b></div>';
if(ext.motor)h+='<div><span style="color:#8b949e">Motor:</span> <b>'+ext.motor+'</b></div>';
h+='<div><span style="color:#8b949e">Part Fan:</span> <b style="color:'+fanClr+'">'+fanPct+'%</b> max_power</div>';
if(phw.z_steppers>=4)h+='<div><span style="color:#8b949e">Z:</span> <b>Quad Gantry</b> ('+phw.z_steppers+' steppers)</div>';
if(phw.probe_type)h+='<div><span style="color:#8b949e">Probe:</span> <b>'+phw.probe_type+'</b></div>';
if(phw.mmu_present)h+='<div><span style="color:#d29922">MMU Present</span></div>';
h+='</div></div>'}

/* --- Performance/Utilization summary items --- */
var perfItems=pa.filter(function(a){return a.category==='Performance'});
if(perfItems.length){
h+='<div class="box">';
perfItems.forEach(function(a){
var clrMap={'bad':'#f85149','warn':'#d29922','good':'#3fb950','info':'#58a6ff'};
var bgMap={'bad':'rgba(248,81,73,0.1)','warn':'rgba(210,153,34,0.1)','good':'rgba(63,185,80,0.1)','info':'rgba(88,166,255,0.1)'};
var iconMap={'bad':'\u26a1','warn':'\u26a0\ufe0f','good':'\u2705','info':'\u2139\ufe0f'};
h+='<div style="padding:12px 16px;border-left:3px solid '+clrMap[a.verdict]+';background:'+bgMap[a.verdict]+';border-radius:4px;margin-bottom:4px">'+
'<div style="font-weight:700;color:'+clrMap[a.verdict]+';font-size:14px">'+iconMap[a.verdict]+' '+a.current+'</div>'+
'<div style="color:#c9d1d9;font-size:13px;margin:4px 0">'+a.reason+'</div></div>'});
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
h+='<div class="box" style="border-left:3px solid '+vClr+';margin-top:16px">';
h+='<div style="font-weight:700;font-size:15px;color:'+vClr+';margin-bottom:8px">'+vIcon+' Optimization Analysis <span style="font-size:12px;font-weight:400;color:#8b949e">(based on actual print data)</span></div>';
h+='<div style="color:#c9d1d9;font-size:13px;margin-bottom:12px">'+bo.verdict_text+'</div>';

/* Headroom gauges */
h+='<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px;margin-bottom:12px">';
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
applyBtn='<div style="margin-top:6px"><button id="'+bId+'" class="cfg-btn" style="font-size:11px;padding:3px 12px" '+
'onclick="applyChange(\\''+bId+'\\',\\''+sg.config_var+'\\','+sg.suggested_value+',\\''+(sg.material||'')+'\\')"'+
'>Apply '+sg.config_var+' = '+sg.suggested_value+'</button></div>';}
h+='<div style="padding:8px 10px;background:rgba(63,185,80,0.06);border-radius:4px;margin-bottom:4px;font-size:12px">'+
'<div style="color:#3fb950;font-weight:600">'+sg.what+' <span style="font-size:10px;color:'+impactClr+';font-weight:400">'+sg.impact+'</span></div>'+
detHtml+applyBtn+'</div>'});
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

function rTr(){
var tr=D.trends;
if(!tr||tr.length<2){ca.innerHTML='<p>Need at least 2 prints for trends.</p>';return}
var rows='';tr.forEach(function(t){
rows+='<tr><td>'+t.date+'</td><td>'+t.material+
'</td><td>'+t.avg_boost.toFixed(1)+'\u00b0C</td><td class="'+
(t.max_pwm>0.95?'d':'')+'">'+(t.max_pwm*100).toFixed(0)+
'%</td><td class="'+(t.high_risk>10?'w':'')+'">'+t.high_risk+
'</td><td>'+(t.vib_score!=null?t.vib_score:'\u2014')+
'</td><td>'+t.culprit+'</td></tr>'});
ca.innerHTML='<div class="box"><h3>Print-over-Print Trends</h3>'+
'<p class="box-desc">Each point is a completed print. Falling boost and risk = the system is learning your setup. '+
'Rising values may indicate a clog, worn nozzle, or changed slicer settings.</p>'+mc('c9')+
'</div><div class="box"><h3>Details</h3>'+
'<p class="box-desc">Culprit = the most common cause of risk events for that print (e.g. temperature, PA, acceleration).</p>'+
'<table><tr><th>Date</th><th>Material</th>'+
'<th>Boost</th><th>Max PWM</th><th>Risk</th><th>Vib</th><th>Culprit</th></tr>'+rows+'</table></div>';
CH.c9=new Chart(document.getElementById('c9'),{type:'line',data:{
labels:tr.map(function(t){return t.date}),
datasets:[
{label:'Avg Boost (\u00b0C)',data:tr.map(function(t){return t.avg_boost}),
borderColor:'#d29922',borderWidth:2,pointRadius:4,fill:false,yAxisID:'y'},
{label:'Risk Events',data:tr.map(function(t){return t.high_risk}),
borderColor:'#f85149',borderWidth:2,pointRadius:4,fill:false,yAxisID:'y1'},
{label:'Avg PWM (%)',data:tr.map(function(t){return t.avg_pwm*100}),
borderColor:'#bc8cff',borderWidth:2,pointRadius:4,fill:false,yAxisID:'y'},
{label:'Vib Score',data:tr.map(function(t){return t.vib_score}),
borderColor:'#3fb950',borderWidth:2,pointRadius:4,fill:false,yAxisID:'y',
borderDash:[5,3],pointStyle:'rectRot',hidden:!tr.some(function(t){return t.vib_score!=null})}]},
options:{responsive:true,animation:false,
interaction:{intersect:false,mode:'index'},
scales:{x:{title:{display:true,text:'Print Date'}},
y:{title:{display:true,text:'\u00b0C / %'},position:'left'},
y1:{title:{display:true,text:'Events'},position:'right',
grid:{drawOnChartArea:false}}}}})}

function rRec(){
var recs=D.recommendations||[];
var hw=D.printer_hw||{};
var hwHtml='';
if(hw.kinematics||hw.extruder||hw.input_shaper||hw.part_fan){
hwHtml='<div class="box" style="margin-bottom:16px;border:1px solid #30363d;padding:14px;border-radius:8px">'+
'<h3 style="color:#58a6ff;font-size:15px;margin-bottom:10px">\ud83d\udee0 Detected Printer Hardware</h3><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px;font-size:13px">';
if(hw.kinematics)hwHtml+='<div><span style="color:#8b949e">Kinematics:</span> '+hw.kinematics+'</div>';
if(hw.build_volume)hwHtml+='<div><span style="color:#8b949e">Build:</span> '+hw.build_volume[0]+'\u00d7'+hw.build_volume[1]+'\u00d7'+hw.build_volume[2]+' mm</div>';
var is_=hw.input_shaper||{};
var rMinAcc=Math.min((is_.x||{}).recommended_max_accel||99999,(is_.y||{}).recommended_max_accel||99999);
var rHasShaper=rMinAcc<99999;
if(rHasShaper)hwHtml+='<div><span style="color:#8b949e">Quality Max Accel:</span> <b style="color:#3fb950">'+rMinAcc+'</b> <span style="color:#484f58">(Y limit)</span></div>';
if(hw.firmware_max_accel)hwHtml+='<div><span style="color:#8b949e">Firmware Ceiling:</span> <span style="color:#484f58">'+hw.firmware_max_accel+' accel / '+(hw.firmware_max_velocity||'?')+' mm/s</span></div>';
var ext=hw.extruder||{};
if(ext.drive_type)hwHtml+='<div><span style="color:#8b949e">Extruder:</span> '+ext.drive_type+(ext.motor?' ('+ext.motor+')':'')+'</div>';
if(ext.nozzle_diameter)hwHtml+='<div><span style="color:#8b949e">Nozzle:</span> '+ext.nozzle_diameter+' mm</div>';
if(ext.tmc_driver)hwHtml+='<div><span style="color:#8b949e">Extruder TMC:</span> '+ext.tmc_driver+(ext.run_current?' @ '+ext.run_current+'A':'')+'</div>';
var fan=hw.part_fan||{};
if(fan.max_power!=null){var fp=Math.round(fan.max_power*100);hwHtml+='<div><span style="color:#8b949e">Fan Cap:</span> <span style="color:'+(fan.max_power<1?"#f85149":"#3fb950")+'">'+fp+'%</span></div>'}
if(is_.x||is_.y){hwHtml+='<div><span style="color:#8b949e">Input Shaper:</span> ';
var parts=[];if(is_.x)parts.push('X: '+(is_.x.type||'?').toUpperCase()+' @ '+is_.x.freq+'Hz ('+is_.x.recommended_max_accel+')');if(is_.y)parts.push('Y: '+(is_.y.type||'?').toUpperCase()+' @ '+is_.y.freq+'Hz ('+is_.y.recommended_max_accel+')');hwHtml+=parts.join(', ')+'</div>'}
if(hw.z_steppers)hwHtml+='<div><span style="color:#8b949e">Z Steppers:</span> '+hw.z_steppers+(hw.z_steppers>=4?' (Quad Gantry)':'')+'</div>';
if(hw.probe_type)hwHtml+='<div><span style="color:#8b949e">Probe:</span> '+hw.probe_type+'</div>';
if(hw.mmu_present)hwHtml+='<div><span style="color:#8b949e">MMU:</span> \u2713 Detected</div>';
var tmc=hw.xy_tmc||{};
if(tmc.driver)hwHtml+='<div><span style="color:#8b949e">XY TMC:</span> '+tmc.driver+(tmc.run_current?' @ '+tmc.run_current+'A':'')+(tmc.stealthchop?' (StealthChop)':' (SpreadCycle)')+'</div>';
hwHtml+='</div></div>'}
if(!recs.length){ca.innerHTML=hwHtml+'<div class="box"><p>No data available for recommendations yet.</p></div>';return}
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
ca.innerHTML=hwHtml+h}

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

/* ── Vibration Analysis Panel ── */
function rVib(){
var vb=D.vibration;
if(!vb||!vb.summary){
ca.innerHTML='<div class="box"><div class="box-hd">📊 Vibration Analysis</div>'+
'<p class="box-desc">No vibration data available yet. The ADXL345 auto-sampler will automatically collect vibration samples during your next print.</p>'+
'<p style="color:#8b949e;margin-top:12px">Samples are taken every 5 minutes throughout the print. Each sample captures a short burst of accelerometer data at ~3200Hz and correlates it with what the printer is doing (speed, acceleration, layer height).</p>'+
'<p style="color:#8b949e;margin-top:8px">After the print completes, the vibration data appears here with per-feature analysis and recommendations.</p></div>';
return}
var sm=vb.summary;
var ns=vb.n_samples||0;
var qs=sm.quality_score;var qc=qs>=80?'#3fb950':qs>=50?'#d29922':'#f85149';
var h='<div class="box"><div class="box-hd">📊 Print Vibration Summary</div>';
h+='<p class="box-desc">'+ns+' ADXL samples collected during print'+(vb.filename?' of <b>'+vb.filename+'</b>':'')+'. Each sample = '+vb.sample_duration_s+'s burst at ~3200Hz.</p>';
h+='<div style="display:flex;gap:16px;margin:12px 0;flex-wrap:wrap">';
if(qs!=null){h+='<div class="score-box" style="min-width:140px;border:2px solid '+qc+'"><div class="score-label">Quality Score</div><div class="score-val" style="color:'+qc+';font-size:28px">'+qs+'<span style="font-size:14px;color:#8b949e">/100</span></div>';
var sb=sm.score_breakdown;if(sb){h+='<div style="font-size:10px;color:#8b949e;margin-top:4px">';
if(sb.mag_rms)h+='RMS:'+sb.mag_rms.score.toFixed(0)+' ';if(sb.axis_balance)h+='Bal:'+sb.axis_balance.score.toFixed(0)+' ';if(sb.peak_control)h+='Peak:'+sb.peak_control.score.toFixed(0)+' ';if(sb.feature_consistency)h+='Cons:'+sb.feature_consistency.score.toFixed(0);h+='</div>'}
h+='</div>'}
h+='<div class="score-box" style="min-width:120px"><div class="score-label">X RMS</div><div class="score-val" style="color:#58a6ff">'+sm.x_rms_avg+'</div><div style="font-size:11px;color:#8b949e">peak: '+sm.x_rms_max+'</div></div>';
h+='<div class="score-box" style="min-width:120px"><div class="score-label">Y RMS</div><div class="score-val" style="color:#3fb950">'+sm.y_rms_avg+'</div><div style="font-size:11px;color:#8b949e">peak: '+sm.y_rms_max+'</div></div>';
h+='<div class="score-box" style="min-width:120px"><div class="score-label">Mag RMS</div><div class="score-val" style="color:#f0883e">'+sm.mag_rms_avg+'</div><div style="font-size:11px;color:#8b949e">peak: '+sm.mag_rms_max+'</div></div>';
h+='<div class="score-box" style="min-width:120px"><div class="score-label">Mag Peak</div><div class="score-val" style="color:#da3633">'+sm.mag_peak_max+'</div></div>';
if(sm.dominant_freq_x_hz>0||sm.dominant_freq_y_hz>0){
h+='<div class="score-box" style="min-width:120px"><div class="score-label">Dom. Freq X</div><div class="score-val" style="color:#58a6ff;font-size:18px">'+sm.dominant_freq_x_hz+' Hz</div></div>';
h+='<div class="score-box" style="min-width:120px"><div class="score-label">Dom. Freq Y</div><div class="score-val" style="color:#3fb950;font-size:18px">'+sm.dominant_freq_y_hz+' Hz</div></div>';
}
h+='</div></div>';
/* Per-accel/feature breakdown */
var ba=vb.by_accel;
if(ba&&Object.keys(ba).length>0){
var am=(D.slicer_diagnosis||{}).accel_map||{};
h+='<div class="box"><div class="box-hd">🔧 Vibration by Feature / Acceleration</div>';
h+='<p class="box-desc">How vibration varies across different print features (identified by acceleration value). Higher RMS = more vibration = lower quality.</p>';
h+='<table class="tbl"><thead><tr><th>Accel (mm/s²)</th><th>Feature</th><th>Samples</th><th>X RMS</th><th>Y RMS</th><th>Mag RMS</th><th>Avg Speed</th><th>Z Range</th><th>Recommendation</th></tr></thead><tbody>';
var keys=Object.keys(ba).sort(function(a,b){return Number(a)-Number(b)});
for(var i=0;i<keys.length;i++){
var k=keys[i],d2=ba[k];
var feat=am[k]?am[k].features.join(', '):'—';
var xc=d2.x_rms_avg>500?'color:#da3633':d2.x_rms_avg>200?'color:#d29922':'';
var yc=d2.y_rms_avg>500?'color:#da3633':d2.y_rms_avg>200?'color:#d29922':'';
var rec=d2.recommendation||{};
var recHtml='';
if(rec.action==='reduce'){recHtml='<span style="color:#d29922">↓ '+rec.suggested_accel+'</span><br><span style="font-size:10px;color:#8b949e">-'+rec.reduction_pct+'%</span>'}
else if(rec.action==='keep'){recHtml='<span style="color:#3fb950">✓ OK</span>'}
else{recHtml='<span style="color:#8b949e">—</span>'}
h+='<tr><td>'+k+'</td><td>'+feat+'</td><td>'+d2.n_samples+'</td>';
h+='<td style="'+xc+'">'+d2.x_rms_avg+'</td><td style="'+yc+'">'+d2.y_rms_avg+'</td>';
h+='<td>'+d2.mag_rms_avg+'</td><td>'+d2.speed_avg+' mm/s</td>';
h+='<td>'+d2.z_range[0]+' - '+d2.z_range[1]+' mm</td><td>'+recHtml+'</td></tr>';
}
h+='</tbody></table>';
/* Show gcode commands for features that need reducing */
var reduceKeys=keys.filter(function(k){return (ba[k].recommendation||{}).action==='reduce'});
if(reduceKeys.length>0){
h+='<div style="margin-top:12px;padding:10px;background:#0d1117;border-radius:6px;border:1px solid #30363d">';
h+='<div style="font-size:11px;color:#8b949e;margin-bottom:6px">Suggested G-code (apply per-feature in slicer or macros):</div>';
for(var ri=0;ri<reduceKeys.length;ri++){var rk=reduceKeys[ri],rr=ba[rk].recommendation;
h+='<div style="font-family:monospace;font-size:12px;color:#d29922;margin:2px 0">'+rr.gcode+'  <span style="color:#484f58">; was '+rk+', '+rr.reason+'</span></div>'}
h+='</div>'}
h+='</div>';
}
/* Vibration over time chart */
var samps=vb.samples||[];
if(samps.length>=2){
h+='<div class="box"><div class="box-hd">📈 Vibration Over Print Progress</div>';
h+='<p class="box-desc">How vibration changed throughout the print. Spikes may indicate speed changes, feature transitions, or mechanical issues.</p>';
h+='<div style="height:350px">'+mc('vib_time_chart')+'</div></div>';
}
/* Vibration ↔ Banding Correlation */
var vbc=D.vibration_banding||[];
if(vbc.length>0){
h+='<div class="box"><div class="box-hd">🔗 Vibration × Banding Correlation</div>';
h+='<p class="box-desc">Banding events cross-referenced with ADXL vibration data at matching Z-heights. <b>Strong correlations = mechanical cause confirmed.</b></p>';
h+='<table class="tbl"><thead><tr><th>Z Height</th><th>Banding Risk</th><th>Vib RMS</th><th>Accel</th><th>Speed</th><th>Probable Cause</th></tr></thead><tbody>';
for(var ci=0;ci<Math.min(vbc.length,15);ci++){var cv=vbc[ci];
var rc2=cv.vibration_rms>500?'color:#da3633':cv.vibration_rms>200?'color:#d29922':'';
h+='<tr><td>'+cv.z_height+' mm</td><td class="'+(cv.banding_risk>=8?'d':cv.banding_risk>=5?'w':'')+'">'+cv.banding_risk+'</td>';
h+='<td style="'+rc2+'">'+cv.vibration_rms+'</td><td>'+cv.vibration_accel+'</td>';
h+='<td>'+cv.vibration_speed+' mm/s</td><td>'+cv.probable_cause+'</td></tr>'}
h+='</tbody></table></div>'}
/* Recommendations */
h+='<div class="box"><div class="box-hd">💡 Vibration Insights</div>';
var recs=[];
if(sm.x_rms_avg>500||sm.y_rms_avg>500)recs.push('<b>High overall vibration</b> — X or Y RMS above 500 mm/s². Consider reducing print speed or tightening belts.');
if(sm.x_rms_avg>0&&sm.y_rms_avg>0&&Math.abs(sm.x_rms_avg-sm.y_rms_avg)/Math.max(sm.x_rms_avg,sm.y_rms_avg)>0.4)recs.push('<b>Axis imbalance</b> — X and Y vibration differ by >40%. Check belt tension balance, or one axis may have a mechanical issue.');
if(sm.dominant_freq_x_hz>0&&sm.dominant_freq_y_hz>0){
var is_x=D.slicer_settings&&D.slicer_settings.input_shaper_freq_x?parseFloat(D.slicer_settings.input_shaper_freq_x):0;
var is_y=D.slicer_settings&&D.slicer_settings.input_shaper_freq_y?parseFloat(D.slicer_settings.input_shaper_freq_y):0;
if(is_x>0&&Math.abs(sm.dominant_freq_x_hz-is_x)/is_x>0.2)recs.push('<b>X shaper frequency mismatch</b> — dominant vibration at '+sm.dominant_freq_x_hz+'Hz but input shaper set to '+is_x+'Hz. Consider re-running resonance test.');
if(is_y>0&&Math.abs(sm.dominant_freq_y_hz-is_y)/is_y>0.2)recs.push('<b>Y shaper frequency mismatch</b> — dominant vibration at '+sm.dominant_freq_y_hz+'Hz but input shaper set to '+is_y+'Hz. Consider re-running resonance test.');
}
if(ba){
var maxVibAccel='',maxVibVal=0;
for(var k in ba){if(ba[k].mag_rms_avg>maxVibVal){maxVibVal=ba[k].mag_rms_avg;maxVibAccel=k}}
var minVibAccel='',minVibVal=999999;
for(var k in ba){if(ba[k].mag_rms_avg<minVibVal){minVibVal=ba[k].mag_rms_avg;minVibAccel=k}}
if(maxVibVal>minVibVal*2&&maxVibAccel!==minVibAccel){
var mfeat=am[maxVibAccel]?am[maxVibAccel].features.join('/'):'accel='+maxVibAccel;
recs.push('<b>'+mfeat+' is the noisiest feature</b> — '+maxVibVal+' RMS vs '+minVibVal+' for quietest. Reducing speed/accel for this feature would have the biggest quality impact.');
}
}
if(recs.length===0)recs.push('Vibration levels look healthy. No obvious issues detected.');
for(var i=0;i<recs.length;i++)h+='<p style="margin:6px 0;padding:8px;background:#161b22;border-radius:6px;border-left:3px solid '+(i<3?'#d29922':'#3fb950')+'">'+recs[i]+'</p>';
h+='</div>';
ca.innerHTML=h;
/* Render time chart */
if(samps.length>=2&&typeof Chart!=='undefined'){
var ctx=document.getElementById('vib_time_chart');
if(ctx){
var labels=[],xd=[],yd=[],md=[];
for(var i=0;i<samps.length;i++){
var pr=samps[i].printer||{};
labels.push((pr.progress_pct||0).toFixed(0)+'%');
xd.push(samps[i].vibration.x.rms);
yd.push(samps[i].vibration.y.rms);
md.push(samps[i].vibration.magnitude.rms);
}
CH['vib_time']=new Chart(ctx,{type:'line',data:{labels:labels,datasets:[
{label:'X RMS',data:xd,borderColor:'#58a6ff',borderWidth:2,pointRadius:4,tension:0.3},
{label:'Y RMS',data:yd,borderColor:'#3fb950',borderWidth:2,pointRadius:4,tension:0.3},
{label:'Mag RMS',data:md,borderColor:'#f0883e',borderWidth:2,pointRadius:4,tension:0.3,borderDash:[4,2]}
]},options:{responsive:true,maintainAspectRatio:false,
scales:{x:{title:{display:true,text:'Print Progress'}},y:{title:{display:true,text:'Vibration RMS (mm/s²)'},beginAtZero:true}},
plugins:{legend:{position:'top',labels:{boxWidth:12,font:{size:11}}}}}})
}}
}

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


def _moonraker_gcode(script, timeout=8):
    """Send a gcode script to Klipper via Moonraker. Returns True on success."""
    encoded = urllib.parse.quote(script)
    try:
        r = subprocess.run(
            ['curl', '-s', '-m', str(timeout),
             'http://127.0.0.1:7125/printer/gcode/script?script=' + encoded],
            capture_output=True, timeout=timeout + 2
        )
        return r.returncode == 0
    except Exception:
        return False

def _moonraker_query(endpoint, timeout=5):
    """Query a Moonraker API endpoint. Returns parsed JSON dict or None."""
    try:
        r = subprocess.run(
            ['curl', '-s', '-m', str(timeout),
             'http://127.0.0.1:7125' + endpoint],
            capture_output=True, timeout=timeout + 2
        )
        if r.returncode == 0 and r.stdout:
            return json.loads(r.stdout.decode())
    except Exception:
        pass
    return None


# --------------- Background ADXL Print Sampler --------------------------------
# Automatically samples ADXL345 vibration at strategic points during prints.
# Each sample: 2-second burst → parse CSV → record vibration metrics + printer state.
# Results saved as *_vibration.json alongside the existing print logs.

_ADXL_SAMPLE_CSV = '/tmp/adxl345-autosample.csv'
_ADXL_SAMPLE_INTERVAL = 300   # seconds between samples (5 minutes — reduced from 2min to ease MCU load)
_ADXL_SAMPLE_DURATION = 0.5   # seconds of ADXL recording per sample (reduced from 2.0s to prevent timer-too-close)
_ADXL_SPEED_THRESHOLD = 80    # mm/s — defer sampling when toolhead is moving faster than this
_ADXL_MAX_BACKOFF = 1800      # max backoff interval in seconds (30 min)
_adxl_sampler_active = False   # True while a print is being sampled
_adxl_sampler_enabled = True   # Set to False via --no-adxl to disable entirely


def _parse_adxl_csv(csv_path):
    """Parse an ADXL345 CSV file into vibration metrics.
    
    Returns dict with per-axis stats: mean, peak, RMS, dominant frequency estimate.
    CSV format: #time,accel_x,accel_y,accel_z
    """
    samples = []
    try:
        with open(csv_path, 'r') as f:
            for line in f:
                if line.startswith('#') or not line.strip():
                    continue
                parts = line.strip().split(',')
                if len(parts) >= 4:
                    try:
                        t = float(parts[0])
                        x = float(parts[1])
                        y = float(parts[2])
                        z = float(parts[3])
                        samples.append((t, x, y, z))
                    except (ValueError, IndexError):
                        pass
    except Exception:
        return None

    n = len(samples)
    if n < 10:
        return None

    xs = [s[1] for s in samples]
    ys = [s[2] for s in samples]
    zs = [s[3] for s in samples]

    def _axis_stats(vals):
        n = len(vals)
        mean = sum(vals) / n
        # Remove DC offset for vibration analysis
        ac = [v - mean for v in vals]
        rms = (sum(v * v for v in ac) / n) ** 0.5
        peak = max(abs(v) for v in ac)
        return {
            'mean': round(mean, 1),
            'rms': round(rms, 1),
            'peak': round(peak, 1),
        }

    # Compute magnitude for each sample
    mags = [(s[1]**2 + s[2]**2 + s[3]**2)**0.5 for s in samples]
    mag_mean = sum(mags) / n
    mag_ac = [m - mag_mean for m in mags]
    mag_rms = (sum(v * v for v in mag_ac) / n) ** 0.5
    mag_peak = max(abs(v) for v in mag_ac)

    # Estimate sample rate
    dt = samples[-1][0] - samples[0][0]
    sample_rate = (n - 1) / dt if dt > 0 else 3200

    # Simple dominant frequency estimate using zero-crossing rate
    # (lightweight alternative to FFT for resource-constrained Pi)
    def _zero_cross_freq(ac_vals, fs):
        crossings = 0
        for i in range(1, len(ac_vals)):
            if ac_vals[i - 1] * ac_vals[i] < 0:
                crossings += 1
        # Zero-crossing rate ≈ 2 * frequency
        return round(crossings / (2 * len(ac_vals) / fs), 1) if len(ac_vals) > 1 else 0

    x_ac = [v - sum(xs) / n for v in xs]
    y_ac = [v - sum(ys) / n for v in ys]

    return {
        'n_samples': n,
        'duration_s': round(dt, 2),
        'sample_rate_hz': round(sample_rate, 0),
        'x': _axis_stats(xs),
        'y': _axis_stats(ys),
        'z': _axis_stats(zs),
        'magnitude': {
            'mean': round(mag_mean, 1),
            'rms': round(mag_rms, 1),
            'peak': round(mag_peak, 1),
        },
        'dominant_freq_x_hz': _zero_cross_freq(x_ac, sample_rate),
        'dominant_freq_y_hz': _zero_cross_freq(y_ac, sample_rate),
    }


def _is_toolhead_busy():
    """Check if the toolhead is actively moving at high speed.
    
    Returns True if the printer is in a high-speed move — in that case we
    should defer ADXL sampling to avoid overwhelming the MCU.
    """
    data = _moonraker_query(
        '/printer/objects/query?gcode_move&toolhead', timeout=3
    )
    if not data:
        return True  # assume busy if we can't query
    status = data.get('result', {}).get('status', {})
    gm = status.get('gcode_move', {})
    speed = gm.get('speed', 0)
    # speed is in mm/s in gcode_move
    return speed > _ADXL_SPEED_THRESHOLD


def _take_adxl_sample():
    """Take a single ADXL burst sample. Returns vibration metrics dict or None.
    
    Sequence: start MEASURE → wait → stop MEASURE → wait for CSV → parse.
    The burst duration is kept very short (0.5s default) to minimise MCU
    interrupt load during active printing and avoid timer-too-close errors.
    """
    # Clean up any old CSV
    if os.path.exists(_ADXL_SAMPLE_CSV):
        try:
            os.remove(_ADXL_SAMPLE_CSV)
        except OSError:
            pass

    # Start recording
    if not _moonraker_gcode('ACCELEROMETER_MEASURE', timeout=10):
        return None

    # Let it record for the sample duration
    time.sleep(_ADXL_SAMPLE_DURATION)

    # Stop recording — Klipper writes CSV via daemon process
    _moonraker_gcode('ACCELEROMETER_MEASURE NAME=autosample', timeout=15)

    # Wait for CSV to appear and have data (up to 2 seconds)
    for _ in range(20):
        time.sleep(0.1)
        if os.path.exists(_ADXL_SAMPLE_CSV):
            try:
                if os.path.getsize(_ADXL_SAMPLE_CSV) > 100:
                    break
            except OSError:
                pass

    if not os.path.exists(_ADXL_SAMPLE_CSV):
        return None

    return _parse_adxl_csv(_ADXL_SAMPLE_CSV)


def _get_printer_state():
    """Query current printer state from Moonraker for annotation."""
    data = _moonraker_query(
        '/printer/objects/query?print_stats&toolhead&gcode_move&display_status'
    )
    if not data:
        return {}

    status = data.get('result', {}).get('status', {})
    ps = status.get('print_stats', {})
    th = status.get('toolhead', {})
    gm = status.get('gcode_move', {})
    ds = status.get('display_status', {})

    # Current speed = gcode_move speed (mm/s), position for Z
    speed = gm.get('speed', 0)  # in mm/s
    pos = gm.get('gcode_position', [0, 0, 0, 0])
    accel = th.get('max_accel', 0)
    sq_corner = th.get('square_corner_velocity', 0)

    return {
        'progress_pct': round((ds.get('progress', 0)) * 100, 1),
        'print_duration_s': round(ps.get('print_duration', 0), 1),
        'filename': ps.get('filename', ''),
        'speed_mm_s': round(speed, 1) if speed else 0,
        'z_height': round(pos[2], 2) if len(pos) > 2 else 0,
        'accel': int(accel),
        'sq_corner_vel': round(sq_corner, 1),
        'layer': ps.get('info', {}).get('current_layer'),
        'total_layers': ps.get('info', {}).get('total_layer'),
    }


def _adxl_print_sampler_loop(log_dir):
    """Background thread: monitor print state and take ADXL samples during prints.
    
    Strategy:
    - Poll print_stats every 10 seconds
    - On print start: wait 60s for first layer to stabilise, then sample every _ADXL_SAMPLE_INTERVAL
    - Each sample: short ADXL burst (0.5s) + printer state snapshot
    - Defers sampling when toolhead is in a fast move to avoid MCU overload
    - Uses exponential backoff on consecutive failures (max 30 min)
    - On print end: save all samples to *_vibration.json
    """
    import logging
    logger = logging.getLogger('ADXLSampler')
    global _adxl_sampler_active

    last_state = 'standby'
    samples = []
    print_filename = ''
    sample_timer = 0
    initial_delay_done = False
    consecutive_failures = 0     # for exponential backoff
    current_interval = _ADXL_SAMPLE_INTERVAL  # may grow on failures

    logger.info("ADXL print sampler started — monitoring for prints "
                f"(interval={_ADXL_SAMPLE_INTERVAL}s, burst={_ADXL_SAMPLE_DURATION}s)")

    while True:
        try:
            time.sleep(10)

            # Check print state
            data = _moonraker_query('/printer/objects/query?print_stats')
            if not data:
                continue
            ps = data.get('result', {}).get('status', {}).get('print_stats', {})
            state = ps.get('state', 'standby')

            # ---------- Print just started ----------
            if state == 'printing' and last_state != 'printing':
                print_filename = ps.get('filename', '')
                samples = []
                sample_timer = 0
                initial_delay_done = False
                consecutive_failures = 0
                current_interval = _ADXL_SAMPLE_INTERVAL
                _adxl_sampler_active = True
                logger.info(f"Print started: {print_filename} — ADXL sampling enabled")

            # ---------- Currently printing ----------
            if state == 'printing' and _adxl_sampler_active:
                sample_timer += 10  # we sleep 10s per loop

                # Wait 60s after print starts before first sample (skip first-layer rattling)
                if not initial_delay_done:
                    if sample_timer >= 60:
                        initial_delay_done = True
                        sample_timer = current_interval  # trigger immediate sample
                    else:
                        last_state = state
                        continue

                # Time for a sample?
                if sample_timer >= current_interval:
                    # Defer if toolhead is doing a fast move — try again next loop
                    if _is_toolhead_busy():
                        logger.debug("  Deferring ADXL sample — toolhead moving fast")
                        last_state = state
                        continue

                    sample_timer = 0

                    printer_state = _get_printer_state()
                    logger.info(
                        f"Taking ADXL sample #{len(samples)+1} at "
                        f"{printer_state.get('progress_pct', 0):.0f}% "
                        f"(z={printer_state.get('z_height', 0)}, "
                        f"accel={printer_state.get('accel', 0)})"
                    )

                    vibration = _take_adxl_sample()
                    if vibration:
                        sample_entry = {
                            'sample_num': len(samples) + 1,
                            'timestamp': time.time(),
                            'printer': printer_state,
                            'vibration': vibration,
                        }
                        samples.append(sample_entry)
                        logger.info(
                            f"  Sample OK: X_rms={vibration['x']['rms']}, "
                            f"Y_rms={vibration['y']['rms']}, "
                            f"mag_rms={vibration['magnitude']['rms']}"
                        )
                        # Reset backoff on success
                        if consecutive_failures > 0:
                            consecutive_failures = 0
                            current_interval = _ADXL_SAMPLE_INTERVAL
                            logger.info(f"  Backoff reset — interval back to {current_interval}s")
                    else:
                        consecutive_failures += 1
                        # Exponential backoff: double the interval each failure, cap at _ADXL_MAX_BACKOFF
                        current_interval = min(
                            _ADXL_SAMPLE_INTERVAL * (2 ** consecutive_failures),
                            _ADXL_MAX_BACKOFF
                        )
                        logger.warning(
                            f"  ADXL sample failed (#{consecutive_failures}) — "
                            f"next attempt in {current_interval}s"
                        )

            # ---------- Print just ended ----------
            if state != 'printing' and last_state == 'printing' and samples:
                _adxl_sampler_active = False
                logger.info(
                    f"Print ended — {len(samples)} ADXL samples collected"
                )

                # Save vibration data
                try:
                    vib_data = _build_vibration_summary(samples, print_filename)
                    # Find matching log file to place vibration file alongside it
                    vib_path = _save_vibration_data(vib_data, log_dir, print_filename)
                    if vib_path:
                        logger.info(f"Vibration data saved: {vib_path}")
                except Exception as exc:
                    logger.error(f"Failed to save vibration data: {exc}")

                samples = []
                print_filename = ''

            last_state = state

        except Exception as exc:
            logger.error(f"ADXL sampler error: {exc}")
            time.sleep(30)  # back off on errors


def _build_vibration_summary(samples, filename):
    """Aggregate per-sample vibration data into a print-level summary."""
    if not samples:
        return {}

    # Group samples by accel value (proxy for feature type)
    by_accel = {}
    all_x_rms = []
    all_y_rms = []
    all_mag_rms = []
    all_mag_peak = []

    for s in samples:
        vib = s['vibration']
        pr = s['printer']
        accel = pr.get('accel', 0)

        all_x_rms.append(vib['x']['rms'])
        all_y_rms.append(vib['y']['rms'])
        all_mag_rms.append(vib['magnitude']['rms'])
        all_mag_peak.append(vib['magnitude']['peak'])

        key = str(accel)
        if key not in by_accel:
            by_accel[key] = {'x_rms': [], 'y_rms': [], 'mag_rms': [], 'speeds': [], 'z_heights': []}
        by_accel[key]['x_rms'].append(vib['x']['rms'])
        by_accel[key]['y_rms'].append(vib['y']['rms'])
        by_accel[key]['mag_rms'].append(vib['magnitude']['rms'])
        by_accel[key]['speeds'].append(pr.get('speed_mm_s', 0))
        by_accel[key]['z_heights'].append(pr.get('z_height', 0))

    # Build per-accel summary
    accel_summary = {}
    for accel_val, data in by_accel.items():
        n = len(data['x_rms'])
        accel_summary[accel_val] = {
            'n_samples': n,
            'x_rms_avg': round(sum(data['x_rms']) / n, 1),
            'y_rms_avg': round(sum(data['y_rms']) / n, 1),
            'mag_rms_avg': round(sum(data['mag_rms']) / n, 1),
            'speed_avg': round(sum(data['speeds']) / n, 1),
            'z_range': [round(min(data['z_heights']), 1), round(max(data['z_heights']), 1)],
        }

    # Dominant frequency analysis across all samples
    freq_x = [s['vibration'].get('dominant_freq_x_hz', 0) for s in samples if s['vibration'].get('dominant_freq_x_hz')]
    freq_y = [s['vibration'].get('dominant_freq_y_hz', 0) for s in samples if s['vibration'].get('dominant_freq_y_hz')]

    n_all = len(all_x_rms)
    summary = {
        'x_rms_avg': round(sum(all_x_rms) / n_all, 1) if n_all else 0,
        'x_rms_max': round(max(all_x_rms), 1) if all_x_rms else 0,
        'y_rms_avg': round(sum(all_y_rms) / n_all, 1) if n_all else 0,
        'y_rms_max': round(max(all_y_rms), 1) if all_y_rms else 0,
        'mag_rms_avg': round(sum(all_mag_rms) / n_all, 1) if n_all else 0,
        'mag_rms_max': round(max(all_mag_rms), 1) if all_mag_rms else 0,
        'mag_peak_max': round(max(all_mag_peak), 1) if all_mag_peak else 0,
        'dominant_freq_x_hz': round(sum(freq_x) / len(freq_x), 1) if freq_x else 0,
        'dominant_freq_y_hz': round(sum(freq_y) / len(freq_y), 1) if freq_y else 0,
    }

    # Compute vibration quality score (0-100)
    score, score_breakdown = _compute_vibration_score(summary, accel_summary)
    summary['quality_score'] = score
    summary['score_breakdown'] = score_breakdown

    # Generate per-feature accel recommendations
    accel_advice = _compute_accel_recommendations(accel_summary, samples)
    for accel_val in accel_advice:
        if accel_val in accel_summary:
            accel_summary[accel_val]['recommendation'] = accel_advice[accel_val]

    return {
        'filename': filename,
        'n_samples': len(samples),
        'sample_interval_s': _ADXL_SAMPLE_INTERVAL,
        'sample_duration_s': _ADXL_SAMPLE_DURATION,
        'samples': samples,
        'summary': summary,
        'by_accel': accel_summary,
    }


def _compute_vibration_score(summary, by_accel):
    """Compute a 0-100 vibration quality score from summary metrics.

    Scoring breakdown (each sub-score is 0-100, weighted then averaged):
      - Magnitude RMS (40%): lower average vibration = higher score
      - Axis balance  (15%): X and Y RMS being similar = higher score
      - Peak control  (20%): low peak-to-average ratio = higher score
      - Feature consistency (25%): less variation between features = higher score

    Returns (score, breakdown_dict).
    """
    breakdown = {}

    # --- Magnitude RMS score (40%) ---
    # Map mag_rms_avg onto 0-100: 0 mm/s² → 100, 1000+ → 0
    mag_rms = summary.get('mag_rms_avg', 0)
    mag_score = max(0, min(100, 100 - (mag_rms / 10.0)))
    breakdown['mag_rms'] = {'score': round(mag_score, 1), 'weight': 40,
                            'detail': f'Avg magnitude RMS: {mag_rms} mm/s²'}

    # --- Axis balance score (15%) ---
    x_rms = summary.get('x_rms_avg', 0)
    y_rms = summary.get('y_rms_avg', 0)
    if max(x_rms, y_rms) > 0:
        imbalance = abs(x_rms - y_rms) / max(x_rms, y_rms)
        balance_score = max(0, 100 - imbalance * 200)  # 50% diff → 0
    else:
        balance_score = 100
    breakdown['axis_balance'] = {'score': round(balance_score, 1), 'weight': 15,
                                  'detail': f'X={x_rms}, Y={y_rms} (imbalance {abs(x_rms-y_rms):.0f})'}

    # --- Peak control score (20%) ---
    # Crest factor: peak/RMS.  ≤2 is excellent, ≥5 is bad
    mag_peak = summary.get('mag_peak_max', 0)
    if mag_rms > 0:
        crest = mag_peak / mag_rms
        peak_score = max(0, min(100, 100 - (crest - 1.5) * 25))
    else:
        peak_score = 100
    breakdown['peak_control'] = {'score': round(peak_score, 1), 'weight': 20,
                                  'detail': f'Peak {mag_peak} vs avg {mag_rms} (crest {mag_peak/mag_rms:.1f}x)' if mag_rms > 0 else 'No data'}

    # --- Feature consistency score (25%) ---
    # How much does vibration vary between different accel/feature values
    if by_accel and len(by_accel) >= 2:
        mag_values = [v.get('mag_rms_avg', 0) for v in by_accel.values()]
        mag_mean = sum(mag_values) / len(mag_values)
        if mag_mean > 0:
            cv = (max(mag_values) - min(mag_values)) / mag_mean  # coefficient of variation
            consistency_score = max(0, min(100, 100 - cv * 100))
        else:
            consistency_score = 100
        breakdown['feature_consistency'] = {
            'score': round(consistency_score, 1), 'weight': 25,
            'detail': f'{len(by_accel)} features, range {min(mag_values):.0f}-{max(mag_values):.0f}'}
    else:
        consistency_score = 80  # neutral when too few features
        breakdown['feature_consistency'] = {'score': 80, 'weight': 25, 'detail': 'Insufficient feature data'}

    # Weighted average
    total = (mag_score * 40 + balance_score * 15 + peak_score * 20 + consistency_score * 25) / 100
    return round(max(0, min(100, total)), 0), breakdown


def _compute_accel_recommendations(by_accel, samples):
    """Generate per-feature acceleration recommendations based on vibration data.

    For each accel group, if vibration is high, suggest a reduced accel value.
    The reduction is proportional to how far above the "healthy" threshold
    the vibration is.

    Returns: {accel_str: {action, suggested_accel, reason, gcode}} or empty dict.
    """
    if not by_accel:
        return {}

    # Establish healthy baseline: the feature with the LOWEST vibration
    mag_values = {k: v.get('mag_rms_avg', 0) for k, v in by_accel.items()}
    if not mag_values:
        return {}
    baseline = min(mag_values.values())
    if baseline <= 0:
        baseline = 100  # sane default

    # Threshold: features above 1.5× baseline get a recommendation
    VIBRATION_THRESHOLD = 1.5
    recommendations = {}

    for accel_str, data in by_accel.items():
        mag = data.get('mag_rms_avg', 0)
        if mag <= 0 or baseline <= 0:
            continue
        ratio = mag / baseline
        if ratio < VIBRATION_THRESHOLD:
            recommendations[accel_str] = {
                'action': 'keep',
                'reason': f'Vibration OK ({mag:.0f} RMS, {ratio:.1f}× baseline)',
            }
            continue

        # Reduce accel proportionally: 1.5× → 25% reduction, 3× → 50% reduction
        reduction_pct = min(0.50, (ratio - 1.0) * 0.25)
        current_accel = int(accel_str)
        suggested = int(current_accel * (1.0 - reduction_pct))
        # Round to nearest 500
        suggested = max(500, round(suggested / 500) * 500)

        recommendations[accel_str] = {
            'action': 'reduce',
            'current_accel': current_accel,
            'suggested_accel': suggested,
            'reduction_pct': round(reduction_pct * 100),
            'reason': f'High vibration ({mag:.0f} RMS, {ratio:.1f}× baseline) — reduce by {round(reduction_pct*100)}%',
            'gcode': f'SET_VELOCITY_LIMIT ACCEL={suggested}',
        }

    return recommendations


def _correlate_vibration_with_banding(vibration_data, banding_analysis):
    """Cross-reference ADXL vibration samples with banding events by Z-height.

    Looks for banding risk events that occurred at the same Z-height as
    high-vibration ADXL samples, providing evidence for causal links.

    Returns list of correlation entries or empty list.
    """
    if not vibration_data or not banding_analysis:
        return []

    samples = vibration_data.get('samples', [])
    events = banding_analysis.get('events', {})
    high_risk = events.get('high_risk_moments', [])
    accel_spikes = events.get('accel_spikes', [])

    if not samples or not (high_risk or accel_spikes):
        return []

    # Build vibration lookup by Z-height (±1mm tolerance)
    vib_by_z = []
    for s in samples:
        pr = s.get('printer', {})
        vib = s.get('vibration', {})
        z = pr.get('z_height', 0)
        mag = vib.get('magnitude', {}).get('rms', 0)
        vib_by_z.append({'z': z, 'mag_rms': mag, 'accel': pr.get('accel', 0),
                         'speed': pr.get('speed_mm_s', 0), 'sample': s})

    correlations = []
    Z_TOLERANCE = 1.5  # mm — match within this range

    for event in high_risk:
        ez = event.get('z', 0)
        risk = event.get('risk', 0)
        flags = event.get('flags', '')

        # Find vibration sample closest to this Z
        best = None
        best_dist = Z_TOLERANCE + 1
        for v in vib_by_z:
            dist = abs(v['z'] - ez)
            if dist < best_dist:
                best_dist = dist
                best = v

        if best and best_dist <= Z_TOLERANCE:
            correlations.append({
                'z_height': round(ez, 2),
                'banding_risk': risk,
                'banding_flags': flags,
                'vibration_rms': best['mag_rms'],
                'vibration_accel': best['accel'],
                'vibration_speed': best['speed'],
                'z_distance': round(best_dist, 2),
                'probable_cause': _classify_correlation(risk, best['mag_rms'], flags),
            })

    # De-duplicate by Z (keep strongest)
    seen_z = {}
    for c in correlations:
        z_key = round(c['z_height'])
        if z_key not in seen_z or c['banding_risk'] > seen_z[z_key]['banding_risk']:
            seen_z[z_key] = c

    return sorted(seen_z.values(), key=lambda x: x['banding_risk'], reverse=True)[:20]


def _classify_correlation(risk, mag_rms, flags):
    """Classify the probable cause of a banding+vibration correlation."""
    causes = []
    if mag_rms > 500:
        causes.append('excessive vibration')
    elif mag_rms > 200:
        causes.append('elevated vibration')
    if 'accel' in flags.lower():
        causes.append('acceleration change')
    if 'pa' in flags.lower():
        causes.append('PA transition')
    if 'temp' in flags.lower():
        causes.append('temperature shift')
    if not causes:
        if mag_rms > 100:
            causes.append('moderate vibration during accel event')
        else:
            causes.append('banding event (low vibration — likely non-mechanical)')
    return ' + '.join(causes)


def _save_vibration_data(vib_data, log_dir, print_filename):
    """Save vibration data JSON alongside existing print logs."""
    log_dir = os.path.expanduser(log_dir)
    if not os.path.isdir(log_dir):
        return None

    # Find the most recent summary JSON that matches this print filename
    safe_name = ''.join(c for c in print_filename if c.isalnum() or c in '._-')[:50]
    candidates = sorted(glob.glob(os.path.join(log_dir, '*_summary.json')), reverse=True)

    target_dir = log_dir
    base_name = None

    for cand in candidates:
        if safe_name and safe_name in os.path.basename(cand):
            base_name = os.path.basename(cand).replace('_summary.json', '')
            break

    if not base_name and candidates:
        # Fall back to most recent
        base_name = os.path.basename(candidates[0]).replace('_summary.json', '')

    if not base_name:
        # No existing logs — create standalone file
        from datetime import datetime
        base_name = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + safe_name

    vib_path = os.path.join(target_dir, base_name + '_vibration.json')
    with open(vib_path, 'w') as f:
        json.dump(vib_data, f, indent=2, default=str)

    return vib_path


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


def serve_dashboard(port, log_dir, material=None, enable_adxl=True):
    """Start the dashboard web server."""
    DashboardHandler.log_dir = log_dir
    DashboardHandler.material = material

    # Start background ADXL print sampler thread (unless disabled)
    if enable_adxl and _adxl_sampler_enabled:
        import threading as _threading
        sampler_thread = _threading.Thread(
            target=_adxl_print_sampler_loop,
            args=(log_dir,),
            daemon=True,
            name='ADXLPrintSampler'
        )
        sampler_thread.start()
    else:
        print("  ADXL auto-sampler: disabled")

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
    parser.add_argument(
        '--no-adxl', action='store_true',
        help='Disable ADXL auto-sampling during prints (prevents timer-too-close errors on slower boards)')
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
        serve_dashboard(args.port, log_dir, material=args.material,
                        enable_adxl=not args.no_adxl)
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
