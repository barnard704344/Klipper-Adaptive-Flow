"""
Adaptive Flow — Slicer settings parser and per-print slicer diagnostics.

Extracts OrcaSlicer/PrusaSlicer settings from G-code footers, cross-references
them with observed print data, and generates per-setting slicer advice.
Also contains E3D Revo hotend flow-limit reference data.
"""

import os
import re
import math
import statistics
import logging
from collections import deque

from af_config import GCODES_DIR, _get_config_value, load_csv_rows



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

def generate_slicer_profile_advice(slicer_settings, hotend_info, print_summary=None, printer_hw=None, boost_speed_increase_pct=None):
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
             f'fan can never exceed {pct}%. This limits cooling capacity. '
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
                # Compute theoretical max for reference
                max_theoretical = int(optimal * 0.90)
                max_theoretical = min(max_theoretical, _fw_max_vel)
                # Use the data-backed speed_increase_pct from boost
                # optimization when available — this ensures the per-speed
                # suggestions match the Optimization Analysis section.
                if boost_speed_increase_pct is not None and boost_speed_increase_pct >= 5:
                    suggest_speed = int(speed * (1 + boost_speed_increase_pct / 100))
                else:
                    # No actual print data — cap at 50% as a safe default
                    suggest_speed = int(speed * 1.5)
                suggest_speed = max(suggest_speed, int(speed * 1.10))  # at least 10% increase
                suggest_speed = min(suggest_speed, max_theoretical)    # don't exceed theoretical
                suggest_speed = min(suggest_speed, _fw_max_vel)        # cap at firmware limit
                suggest_flow = _flow(suggest_speed, line_w, layer)
                pct_inc = int((suggest_speed / speed - 1) * 100)
                _add(setting, category, f'{int(speed)} mm/s', 'warn',
                     f'{suggest_speed} mm/s',
                     f'{feature_name}: only {flow} mm\u00b3/s \u2014 '
                     f'{int(flow / safe_flow * 100)}% of your Revo {variant} capacity '
                     f'({safe_flow} mm\u00b3/s for {material}). '
                     f'Suggest {suggest_speed} mm/s '
                     f'({suggest_flow} mm\u00b3/s, +{pct_inc}%) \u2014 '
                     f'hardware capacity allows up to {max_theoretical} mm/s.', flow)
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

    # =====================================================================
    # WALL SPEED BIFURCATION DETECTION — intra-layer flow swing analysis
    # =====================================================================
    outer_speed = slicer_settings.get('outer_wall_speed')
    inner_speed = slicer_settings.get('inner_wall_speed')
    infill_speed = slicer_settings.get('sparse_infill_speed')

    # 1) Outer wall speed == infill speed is a red flag for quality
    if outer_speed and infill_speed and outer_speed >= infill_speed * 0.95:
        recommended_outer = min(int(infill_speed * 0.65), 150)
        outer_flow = _flow(outer_speed, outer_w, layer_h)
        recommended_flow = _flow(recommended_outer, outer_w, layer_h)
        _add('_outer_wall_vs_infill', 'Quality', f'{int(outer_speed)} mm/s',
             'bad', f'{recommended_outer} mm/s',
             f'Outer wall speed ({int(outer_speed)} mm/s) equals infill speed '
             f'({int(infill_speed)} mm/s). Outer walls are visible surfaces \u2014 '
             f'running them at infill speed ({outer_flow} mm\u00b3/s) sacrifices '
             f'surface quality for zero time savings on most prints. '
             f'Reduce outer_wall_speed to {recommended_outer} mm/s '
             f'({recommended_flow} mm\u00b3/s) for clean surfaces.',
             outer_flow)

    # 2) Wall speed bifurcation: small_perimeter_speed creates 2 distinct
    #    flow rates on outer walls within the SAME layer.  Geometry that mixes
    #    long and short perimeters (logos, text, variable-radius curves) gets
    #    both speeds in one layer → the melt zone can't track the transition
    #    → visible banding at those Z heights.
    if outer_speed and small_peri is not None and outer_speed > 100:
        sp_str = str(small_peri)
        if '%' in sp_str:
            sp_pct = float(sp_str.replace('%', ''))
            sp_abs = outer_speed * sp_pct / 100.0
        else:
            sp_abs = float(small_peri)
            sp_pct = sp_abs / outer_speed * 100 if outer_speed > 0 else 100

        if sp_pct < 100 and outer_speed > 0:
            full_flow = _flow(outer_speed, outer_w, layer_h)
            small_flow = _flow(sp_abs, outer_w, layer_h)
            flow_ratio = full_flow / small_flow if small_flow > 0 else 1
            flow_swing = full_flow - small_flow

            if flow_ratio >= 1.5 and flow_swing > 3:
                # Severe bifurcation — this WILL cause banding on mixed-geometry layers
                _add('_wall_speed_bifurcation', 'Quality',
                     f'{int(outer_speed)} / {int(sp_abs)} mm/s',
                     'bad', f'outer_wall_speed: {int(sp_abs * 1.2)} or small_perimeter_speed: 100%',
                     f'BANDING ROOT CAUSE: outer walls run at two speeds \u2014 '
                     f'{int(outer_speed)} mm/s ({full_flow} mm\u00b3/s) for long perimeters '
                     f'and {int(sp_abs)} mm/s ({small_flow} mm\u00b3/s) for short ones '
                     f'(small_perimeter_speed={sp_str}). '
                     f'That\u2019s a {flow_ratio:.1f}\u00d7 flow swing ({flow_swing:.1f} mm\u00b3/s) '
                     f'on the same visible surface. '
                     f'Layers with mixed geometry (logos, text, curves) get BOTH speeds '
                     f'\u2192 the melt zone can\u2019t track {flow_swing:.1f} mm\u00b3/s transitions '
                     f'\u2192 visible bands at those Z heights. '
                     f'Fix: either reduce outer_wall_speed to ~{int(sp_abs * 1.2)} mm/s '
                     f'(eliminates the swing) or set small_perimeter_speed to 100% '
                     f'(uniform speed everywhere).',
                     full_flow)
            elif flow_ratio >= 1.3:
                _add('_wall_speed_bifurcation', 'Quality',
                     f'{int(outer_speed)} / {int(sp_abs)} mm/s',
                     'warn', f'outer_wall_speed: {int(sp_abs * 1.3)} or small_perimeter_speed: 80%',
                     f'Outer walls have a {flow_ratio:.1f}\u00d7 speed split: '
                     f'{int(outer_speed)} mm/s for long perimeters vs '
                     f'{int(sp_abs)} mm/s for short ones. '
                     f'Flow swings of {flow_swing:.1f} mm\u00b3/s on layers with mixed '
                     f'geometry may cause subtle banding. '
                     f'Narrow the gap by reducing outer_wall_speed or increasing '
                     f'small_perimeter_speed.',
                     full_flow)

    # 3) Gap-fill chaos: gap_infill_speed much slower than wall speeds
    #    creates additional flow transients at geometry transitions
    gap_speed = slicer_settings.get('gap_infill_speed')
    if gap_speed and outer_speed and gap_speed < outer_speed * 0.3:
        gap_flow = _flow(gap_speed, outer_w, layer_h)
        wall_flow = _flow(outer_speed, outer_w, layer_h)
        _add('_gap_fill_transients', 'Quality',
             f'{int(gap_speed)} mm/s',
             'warn', f'{int(outer_speed * 0.4)}\u2013{int(outer_speed * 0.5)} mm/s',
             f'Gap fill at {int(gap_speed)} mm/s ({gap_flow} mm\u00b3/s) is '
             f'{outer_speed / gap_speed:.1f}\u00d7 slower than outer walls '
             f'({int(outer_speed)} mm/s, {wall_flow} mm\u00b3/s). '
             f'Each gap-fill segment creates a sharp flow transient the '
             f'extruder must recover from. On layers with many small gaps '
             f'(complex geometry), this adds pressure instability. '
             f'Increase to {int(outer_speed * 0.4)}\u2013{int(outer_speed * 0.5)} mm/s '
             f'to reduce the flow swing.',
             gap_flow)

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
            # If boost optimization says there's room to go faster, the static
            # "slow down" advice contradicts actual print data — downgrade.
            if boost_speed_increase_pct is not None and boost_speed_increase_pct >= 5:
                _add('_flow_headroom', 'Summary', f'{peak_actual} mm\u00b3/s peak',
                     'info', None,
                     f'Peak flow near Revo {variant} safe limit '
                     f'({safe_flow} mm\u00b3/s, E3D data) but actual print data '
                     f'shows thermal and flow headroom remain.')
            else:
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



