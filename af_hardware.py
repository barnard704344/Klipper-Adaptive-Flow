"""
Adaptive Flow — Hardware-aware Klipper config parser.

Reads printer.cfg and its [include] files to detect printer capabilities
(kinematics, build volume, extruder type, TMC drivers, input shaper, etc.)
used by the recommendation engine.
"""

import os
import math
import re
import logging

from af_config import CONFIG_DIR

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
