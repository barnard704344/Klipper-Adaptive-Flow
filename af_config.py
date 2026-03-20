"""
Adaptive Flow — Config file helpers, shared constants, and CSV loader.

This module is imported by af_hardware, af_slicer, af_analysis, and
analyze_print.  It must not import from any other af_* module.
"""

import os
import json
import csv
import time
from pathlib import Path

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
_MATERIAL_VARS = frozenset(['flow_k', 'pa_boost_k'])

# Allowlist of variables the dashboard API may write, with (min, max) bounds.
# Variables not in this dict are rejected by _apply_config_change.
_ALLOWED_VARIABLES = {
    # Material profile vars
    'flow_k':                   (0.0, 5.0),
    'pa_boost_k':               (0.0, 0.01),
    # Hardware
    'use_high_flow_nozzle':     (0, 1),       # bool treated as int
    'heater_wattage':           (20, 120),
    # HF compensation
    'hf_pa_scale':              (0.5, 3.0),
    'hf_smooth_time':           (0.01, 0.2),
    'sf_smooth_time':           (0.01, 0.2),
    'hf_temp_offset':           (0.0, 20.0),
    # Temperature boost
    'flow_smoothing':           (0.0, 1.0),
    'speed_boost_threshold':    (0.0, 500.0),
    'speed_boost_k':            (0.0, 1.0),
    'ramp_rate_rise':           (0.1, 20.0),
    'ramp_rate_fall':           (0.1, 20.0),
    'max_boost_limit':          (0.0, 60.0),
    # PA
    'pa_enable':                (0, 1),
    'pa_deadband':              (0.0, 0.05),
    'pa_min_value':             (0.0, 0.2),
    'pa_max_reduction':         (0.0, 0.1),
    # DynZ
    'dynz_enable':              (0, 1),
    'dynz_bin_height':          (0.1, 20.0),
    'dynz_speed_thresh':        (1.0, 500.0),
    'dynz_flow_max':            (0.1, 50.0),
    'dynz_pwm_thresh':          (0.1, 1.0),
    'dynz_score_inc':           (0.1, 10.0),
    'dynz_score_decay':         (0.0, 1.0),
    'dynz_activate_score':      (0.5, 50.0),
    'dynz_deactivate_score':    (0.0, 50.0),
    'dynz_relief_method':       None,          # string, no numeric bounds
    'dynz_temp_reduction':      (0.0, 30.0),
    'dynz_accel_relief':        (100, 50000),
    # Safety
    'thermal_runaway_threshold':    (3.0, 30.0),
    'thermal_undertemp_threshold':  (3.0, 30.0),
    'max_safe_flow_hf':         (5.0, 60.0),
    'max_safe_flow_std':        (3.0, 40.0),
    # First layer
    'first_layer_skip':         (0, 1),
    'first_layer_height':       (0.05, 2.0),
    # Multi-object
    'multi_object_temp_wait':   (0, 1),
    'temp_wait_tolerance':      (1.0, 20.0),
    # Filament
    'filament_cross_section':   (1.0, 10.0),
}


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
    # ---- Validate against allowlist ----------------------------------------
    if variable not in _ALLOWED_VARIABLES:
        return False, f'Variable "{variable}" is not an allowed config parameter.'

    bounds = _ALLOWED_VARIABLES[variable]
    if bounds is not None:
        try:
            numeric = float(new_value)
        except (TypeError, ValueError):
            return False, f'Variable "{variable}" requires a numeric value.'
        lo, hi = bounds
        if numeric < lo or numeric > hi:
            return False, f'Value {numeric} for "{variable}" is out of range ({lo}–{hi}).'

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
    Returns ``None`` if the current value can't be read or the suggestion
    has already been applied.
    """
    current = _get_config_value(variable, material)
    if current is None:
        return None

    # Prevent stacking: if this variable was recently applied through the
    # dashboard and the current config matches the applied-to value, compute
    # the suggestion from the PRE-change base so refreshing the page after
    # clicking Apply won't keep incrementing the target.
    base = current
    ts, old_val, new_val = _last_change_for(variable, material)
    if ts is not None and old_val is not None and new_val is not None:
        try:
            new_f = float(new_val)
            old_f = float(old_val)
            if abs(current - new_f) < 0.001:
                base = old_f
        except (ValueError, TypeError):
            pass

    if direction == 'reduce':
        suggested = round(base - amount, 4)
    else:
        suggested = round(base + amount, 4)
    if minimum is not None and suggested < minimum:
        suggested = minimum
    if maximum is not None and suggested > maximum:
        suggested = maximum

    # If current config already meets or exceeds the suggestion, skip
    if suggested == current:
        return None
    if direction == 'increase' and current >= suggested:
        return None
    if direction == 'reduce' and current <= suggested:
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

