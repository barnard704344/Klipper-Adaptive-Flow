import logging
import threading
import time
import json
import os
import csv
from collections import deque
from datetime import datetime

# Logging configuration
LOG_DIR = "~/printer_data/logs/adaptive_flow"
MAX_LOG_FILES = 20  # Keep last 20 print logs


class ExtruderMonitor:
    """Monitor extruder load and accept simple lookahead segments.

    This module provides a small lookahead buffer that can be fed with
    upcoming extrusion segments (E delta in mm and duration in seconds).
    It exposes G-code commands to add/clear segments and to query the
    predicted extrusion rate.
    """

    def __init__(self, config):
        self.printer = config.get_printer()
        self.printer.register_event_handler("klippy:connect", self.handle_connect)

        # lookahead buffer and bookkeeping
        self._lookahead_lock = threading.Lock()
        self._lookahead = deque()  # entries are (e_delta_mm, duration_s, timestamp)

        # keep a tiny history of recent extrusion rates (mm/s) to allow basic
        # normalization when estimating future load
        self._recent_rates = deque(maxlen=20)
        # state for live parsing of incoming G-code
        self._gcode_pos = {'X': None, 'Y': None, 'Z': None}
        self._gcode_last_e = None
        self._gcode_last_f = None
        self._relative_extrusion = False  # M83 sets True, M82 sets False
        
        # Pre-compile regex for performance
        import re
        self._param_re = re.compile(r'([A-Za-z])([-+]?[0-9]*\.?[0-9]+)')
        
        # Print session logging
        self._log_lock = threading.Lock()
        self._log_file = None
        self._log_writer = None
        self._log_start_time = None
        self._log_sample_count = 0
        self._log_stats = {}  # Running stats for summary
        
        # State tracking for deltas (banding detection)
        self._last_pa = None
        self._last_accel = None
        self._last_temp_target = None
        self._last_dynz_state = None
        self._last_z = 0.0

    def handle_connect(self):
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command("SET_LOOKAHEAD", self.cmd_SET_LOOKAHEAD,
                               desc="Add upcoming extrusion segment: SET_LOOKAHEAD E=<mm> D=<s> or SET_LOOKAHEAD CLEAR")
        gcode.register_command("GET_PREDICTED_LOAD", self.cmd_GET_PREDICTED_LOAD,
                               desc="Get predicted extrusion rate and estimated load using lookahead")
        gcode.register_command("AT_LOG_START", self.cmd_AT_LOG_START,
                               desc="Start logging print session data")
        gcode.register_command("AT_LOG_DATA", self.cmd_AT_LOG_DATA,
                               desc="Log a data point during printing")
        gcode.register_command("AT_LOG_END", self.cmd_AT_LOG_END,
                               desc="End logging and write summary")
        gcode.register_command("AT_LOG_STATUS", self.cmd_AT_LOG_STATUS,
                               desc="Check logging status and diagnostics")

        # Attempt to attach a live G-code listener using multiple methods
        hook_installed = False
        
        # Method 1: Use gcode_interceptor module (preferred - most reliable)
        try:
            interceptor = self.printer.lookup_object('gcode_interceptor')
            interceptor.register_gcode_callback(self._on_gcode_line)
            hook_installed = True
            logging.getLogger('ExtruderMonitor').info(
                'Live G-code lookahead hook installed via gcode_interceptor.')
        except Exception:
            pass

        # Method 2: Fallback - try legacy event APIs
        if not hook_installed:
            try:
                if hasattr(gcode, 'register_event_handler'):
                    try:
                        gcode.register_event_handler('gcode:received', self._on_gcode_event)
                        hook_installed = True
                    except Exception:
                        try:
                            gcode.register_event_handler('received', self._on_gcode_event)
                            hook_installed = True
                        except Exception:
                            pass

                if not hook_installed and hasattr(self.printer, 'register_event_handler'):
                    try:
                        self.printer.register_event_handler('gcode:received', self._on_gcode_event)
                        hook_installed = True
                    except Exception:
                        pass
            except Exception:
                pass

            if hook_installed:
                logging.getLogger('ExtruderMonitor').info(
                    'Live G-code lookahead hook installed via legacy event API.')

        if not hook_installed:
            logging.getLogger('ExtruderMonitor').warning(
                'Live G-code lookahead hook not installed. '
                'Add [gcode_interceptor] to printer.cfg for automatic lookahead.')

    def _on_gcode_line(self, line):
        """Simple callback for gcode_interceptor - receives raw G-code line."""
        if not line:
            return
        up = line.upper().strip()
        # Handle G0/G1 moves and M82/M83 extrusion mode commands
        if up.startswith('G0') or up.startswith('G1') or up.startswith('M82') or up.startswith('M83'):
            self._parse_gcode_move(line)

    # Public lookahead API (can be called from other modules)
    def add_lookahead_segment(self, e_delta_mm, duration_s):
        if duration_s <= 0:
            return
        with self._lookahead_lock:
            self._lookahead.append((float(e_delta_mm), float(duration_s), time.time()))

    def clear_lookahead(self):
        with self._lookahead_lock:
            self._lookahead.clear()

    def _on_gcode_event(self, *args, **kwargs):
        """Try to extract raw G-code line from event and parse G0/G1 moves.

        The event API varies; we defensively look through args/kwargs for
        something resembling the raw command string.
        """
        raw = None
        # Look in positional args
        for a in args:
            if isinstance(a, str):
                raw = a
                break
            # some klippy objects may provide .command or .gcode
            if hasattr(a, 'command'):
                try:
                    raw = str(a.command)
                    break
                except Exception:
                    pass
            if hasattr(a, 'gcode'):
                try:
                    raw = str(a.gcode)
                    break
                except Exception:
                    pass

        # Look in kwargs
        if raw is None:
            for k, v in kwargs.items():
                if isinstance(v, str):
                    raw = v
                    break
                if hasattr(v, 'command'):
                    try:
                        raw = str(v.command)
                        break
                    except Exception:
                        pass

        if not raw:
            return

        line = raw.strip()
        if not line:
            return
        up = line.upper()
        if not (up.startswith('G0') or up.startswith('G1')):
            return

        self._parse_gcode_move(line)

    def _parse_gcode_move(self, line):
        """Parse a G0/G1 move and add to lookahead if it contains extrusion."""
        # Check for extrusion mode commands
        up = line.upper().strip()
        if up.startswith('M83'):
            self._relative_extrusion = True
            return
        elif up.startswith('M82'):
            self._relative_extrusion = False
            return
        
        params = {}
        for m in self._param_re.finditer(line):
            params[m.group(1).upper()] = float(m.group(2))

        cur_e = params.get('E', None)
        cur_f = params.get('F', None)

        # compute Euclidean distance if coordinates available (works with X/Y only)
        dist = 0.0
        coords = {}
        for axis in ('X', 'Y', 'Z'):
            coords[axis] = params.get(axis, self._gcode_pos.get(axis))

        # Calculate distance with available axes (don't require all 3)
        try:
            dx = 0.0
            dy = 0.0
            dz = 0.0
            if coords['X'] is not None and self._gcode_pos['X'] is not None:
                dx = coords['X'] - self._gcode_pos['X']
            if coords['Y'] is not None and self._gcode_pos['Y'] is not None:
                dy = coords['Y'] - self._gcode_pos['Y']
            if coords['Z'] is not None and self._gcode_pos['Z'] is not None:
                dz = coords['Z'] - self._gcode_pos['Z']
            dist = (dx*dx + dy*dy + dz*dz) ** 0.5
        except Exception:
            dist = 0.0

        # Track travel vs extrusion for diagnostics
        has_extrusion = False

        # if extrusion present, compute delta
        if cur_e is not None:
            if self._relative_extrusion:
                # In relative mode, E value IS the delta
                delta_e = cur_e
            elif self._gcode_last_e is None:
                delta_e = cur_e
            else:
                delta_e = cur_e - self._gcode_last_e

            # Only count positive extrusion (not retractions)
            if delta_e > 0:
                has_extrusion = True

            # estimate duration
            duration = None
            feed = cur_f if cur_f is not None else self._gcode_last_f
            if feed and dist > 0.0:
                try:
                    duration = dist * 60.0 / float(feed)
                except Exception:
                    duration = None
            if duration is None:
                duration = max(0.001, abs(delta_e) / 1.0)

            if delta_e > 0:
                try:
                    self.add_lookahead_segment(delta_e, duration)
                    # record recent rate
                    rate = abs(delta_e) / max(1e-6, float(duration))
                    try:
                        self._recent_rates.append(rate)
                    except Exception:
                        pass
                except Exception:
                    pass

        # update stored state
        if cur_e is not None:
            self._gcode_last_e = cur_e
        if cur_f is not None:
            self._gcode_last_f = cur_f
        for axis in ('X','Y','Z'):
            if axis in params:
                self._gcode_pos[axis] = params[axis]

    def _predicted_extrusion_rate(self):
        """Return predicted extrusion rate in mm/s computed from current lookahead."""
        # Expire stale entries older than 5 seconds
        now = time.time()
        max_age = 5.0
        with self._lookahead_lock:
            # Remove expired entries from the front of the deque
            while self._lookahead and (now - self._lookahead[0][2]) > max_age:
                self._lookahead.popleft()
            
            total_e = 0.0
            total_t = 0.0
            for e, d, ts in self._lookahead:
                # Only consider entries within the time window
                if (now - ts) <= max_age:
                    total_e += abs(e)
                    total_t += max(1e-6, float(d))
        if total_t <= 0:
            return 0.0
        return total_e / total_t

    def cmd_SET_LOOKAHEAD(self, gcmd):
        # Basic and defensive parameter parsing. Klipper gcmd typically provides
        # get_float, but we attempt to be permissive if not present.
        try:
            # try Klipper-style parsing
            e = None
            d = None
            if hasattr(gcmd, 'get_float'):
                try:
                    e = gcmd.get_float('E')
                except Exception:
                    e = None
                try:
                    d = gcmd.get_float('D')
                except Exception:
                    d = None

            # fallback: parse remaining command string
            if (e is None or d is None) and hasattr(gcmd, 'command'):
                raw = str(gcmd.command).upper()
                if 'CLEAR' in raw:
                    self.clear_lookahead()
                    gcmd.respond_info('Lookahead cleared')
                    return
                # naive parse of tokens like E=1.23 D=0.5
                for token in raw.replace(',', ' ').split():
                    if token.startswith('E='):
                        try:
                            e = float(token.split('=', 1)[1])
                        except Exception:
                            pass
                    if token.startswith('D='):
                        try:
                            d = float(token.split('=', 1)[1])
                        except Exception:
                            pass

            if e is None and d is None:
                gcmd.respond_info('Usage: SET_LOOKAHEAD E=<mm> D=<s> | SET_LOOKAHEAD CLEAR')
                return

            if e is None or d is None:
                gcmd.respond_info('Both E and D are required (unless CLEAR)')
                return

            self.add_lookahead_segment(e, d)
            gcmd.respond_info(f'Added lookahead segment E={e} D={d}')
        except Exception as exc:
            gcmd.respond_info(f'Error in SET_LOOKAHEAD: {str(exc)}')

    def cmd_GET_PREDICTED_LOAD(self, gcmd):
        try:
            pred_rate = self._predicted_extrusion_rate()
            gcmd.respond_info(f'Predicted extrusion rate: {pred_rate:.3f} mm/s')
        except Exception as exc:
            gcmd.respond_info(f'Error computing predicted rate: {str(exc)}')

    def _ensure_log_dir(self):
        """Create log directory if it doesn't exist."""
        log_dir = os.path.expanduser(LOG_DIR)
        os.makedirs(log_dir, exist_ok=True)
        return log_dir

    def _cleanup_old_logs(self, log_dir):
        """Keep only the most recent MAX_LOG_FILES log files."""
        try:
            files = []
            for f in os.listdir(log_dir):
                if f.endswith('.csv'):
                    path = os.path.join(log_dir, f)
                    files.append((os.path.getmtime(path), path))
            
            files.sort(reverse=True)
            for _, path in files[MAX_LOG_FILES:]:
                try:
                    os.remove(path)
                    # Also remove corresponding JSON summary
                    json_path = path.replace('.csv', '_summary.json')
                    if os.path.exists(json_path):
                        os.remove(json_path)
                except:
                    pass
        except Exception as e:
            logging.getLogger('ExtruderMonitor').debug(f"Log cleanup error: {e}")

    def cmd_AT_LOG_START(self, gcmd):
        """Start a new logging session for this print."""
        logger = logging.getLogger('ExtruderMonitor')
        
        material = gcmd.get('MATERIAL', 'UNKNOWN')
        filename = gcmd.get('FILE', 'unknown')
        
        with self._log_lock:
            # Close any existing log
            if self._log_file:
                try:
                    self._log_file.close()
                except:
                    pass
            
            try:
                log_dir = self._ensure_log_dir()
                self._cleanup_old_logs(log_dir)
                
                # Create filename with timestamp
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                safe_filename = ''.join(c for c in filename if c.isalnum() or c in '._-')[:50]
                log_path = os.path.join(log_dir, f"{timestamp}_{safe_filename}.csv")
                
                self._log_file = open(log_path, 'w', newline='')
                self._log_writer = csv.writer(self._log_file)
                
                # Write header with enhanced banding detection columns
                self._log_writer.writerow([
                    'elapsed_s', 'temp_actual', 'temp_target', 'boost',
                    'flow', 'speed', 'pwm', 'pa', 'z_height', 'predicted_flow',
                    'dynz_active', 'accel', 'fan_pct',
                    # Banding detection columns
                    'pa_delta', 'accel_delta', 'temp_target_delta', 'temp_overshoot',
                    'dynz_transition', 'layer_transition', 'banding_risk', 'event_flags'
                ])
                # Flush header immediately to disk
                self._log_file.flush()
                os.fsync(self._log_file.fileno())
                
                # Reset state tracking
                self._last_pa = None
                self._last_accel = None  
                self._last_temp_target = None
                self._last_dynz_state = None
                self._last_z = 0.0
                
                self._log_start_time = time.time()
                self._log_sample_count = 0
                
                # Parse optional feature flags
                at_enabled = gcmd.get_int('AT_ENABLED', 1)
                dynz_enabled = gcmd.get_int('DYNZ_ENABLED', 1)
                sc_enabled = gcmd.get_int('SC_ENABLED', 1)
                pa_enabled = gcmd.get_int('PA_ENABLED', 1)
                
                self._log_stats = {
                    'material': material,
                    'filename': filename,
                    'start_time': datetime.now().isoformat(),
                    # Feature flags
                    'features': {
                        'auto_temp': bool(at_enabled),
                        'dynamic_pa': bool(pa_enabled),
                        'smart_cooling': bool(sc_enabled),
                        'dynamic_z': bool(dynz_enabled),
                    },
                    # Temp boost stats
                    'boost_sum': 0.0,
                    'boost_max': 0.0,
                    'boost_count': 0,  # samples with boost > 0
                    # Heater stats
                    'pwm_sum': 0.0,
                    'pwm_max': 0.0,
                    'pwm_at_1_count': 0,  # samples with pwm at 1.0
                    # Temperature stats
                    'temp_min': 999.0,
                    'temp_max': 0.0,
                    'temp_sum': 0.0,
                    'temp_target_max': 0.0,
                    # Flow stats
                    'flow_sum': 0.0,
                    'flow_max': 0.0,
                    'speed_max': 0.0,
                    # PA stats
                    'pa_min': 999.0,
                    'pa_max': 0.0,
                    'pa_sum': 0.0,
                    # Thermal lag
                    'thermal_lag_sum': 0.0,
                    'thermal_lag_max': 0.0,
                    # DynZ stats
                    'dynz_active_samples': 0,
                    'accel_min': 999999,
                    # Fan/Smart Cooling stats
                    'fan_sum': 0.0,
                    'fan_min': 100,
                    'fan_max': 0,
                    'fan_adjustments': 0,  # times fan changed from previous sample
                    'last_fan': -1,  # for tracking adjustments
                }
                
                gcmd.respond_info(f"AT_LOG: Started logging to {log_path}")
                gcmd.respond_info(f"AT_LOG: Waiting for data... (AT_LOG_DATA will be called each loop cycle)")
                logger.info(f"Started print logging: {log_path}")
                
            except Exception as e:
                gcmd.respond_info(f"AT_LOG: Failed to start logging: {e}")
                logger.error(f"Failed to start logging: {e}")
                # Make sure we don't leave partial state
                self._log_file = None
                self._log_writer = None

    def cmd_AT_LOG_DATA(self, gcmd):
        """Log a single data point during printing."""
        with self._log_lock:
            if not self._log_writer:
                # Logging not started - warn user occasionally
                if not hasattr(self, '_log_warning_shown'):
                    self._log_warning_shown = True
                    gcmd.respond_info("AT_LOG: Logging not active. Call AT_LOG_START first.")
                return
            
            # First data point - confirm logging is working
            if self._log_sample_count == 0:
                gcmd.respond_info("AT_LOG: First data point received - logging active")
            
            try:
                elapsed = time.time() - self._log_start_time
                temp_actual = gcmd.get_float('TEMP', 0.0)
                temp_target = gcmd.get_float('TARGET', 0.0)
                boost = gcmd.get_float('BOOST', 0.0)
                flow = gcmd.get_float('FLOW', 0.0)
                speed = gcmd.get_float('SPEED', 0.0)
                pwm = gcmd.get_float('PWM', 0.0)
                pa = gcmd.get_float('PA', 0.0)
                z_height = gcmd.get_float('Z', 0.0)
                predicted = gcmd.get_float('PREDICTED', 0.0)
                dynz_active = gcmd.get_int('DYNZ', 0)
                accel = gcmd.get_int('ACCEL', 0)
                fan_pct = gcmd.get_int('FAN', 0)
                
                # Calculate deltas for banding detection
                pa_delta = (pa - self._last_pa) if self._last_pa is not None else 0.0
                accel_delta = (accel - self._last_accel) if self._last_accel is not None else 0
                temp_target_delta = (temp_target - self._last_temp_target) if self._last_temp_target is not None else 0.0
                temp_overshoot = temp_actual - temp_target
                
                # Detect state transitions
                dynz_transition = 0
                if self._last_dynz_state is not None and dynz_active != self._last_dynz_state:
                    dynz_transition = 1 if dynz_active else -1  # 1=activated, -1=deactivated
                
                layer_transition = 1 if (z_height > self._last_z + 0.05) else 0
                
                # Banding risk score (0-10 scale)
                # High risk when multiple factors change simultaneously
                risk = 0
                event_flags = []
                
                if abs(accel_delta) > 500:
                    risk += 3
                    event_flags.append(f"ACCEL_CHG:{accel_delta:+d}")
                if abs(pa_delta) > 0.005:
                    risk += 2
                    event_flags.append(f"PA_CHG:{pa_delta:+.4f}")
                if abs(temp_target_delta) > 3.0:
                    risk += 2
                    event_flags.append(f"TEMP_CHG:{temp_target_delta:+.1f}")
                if abs(temp_overshoot) > 5.0:
                    risk += 1
                    event_flags.append(f"OVERSHOOT:{temp_overshoot:+.1f}")
                if dynz_transition != 0:
                    risk += 2
                    event_flags.append(f"DYNZ:{'ON' if dynz_transition > 0 else 'OFF'}")
                if pwm >= 0.95 and temp_target > temp_actual + 3:
                    risk += 1
                    event_flags.append("HEATER_STRUGGLE")
                
                event_str = ";".join(event_flags) if event_flags else ""
                
                self._log_writer.writerow([
                    f"{elapsed:.1f}",
                    f"{temp_actual:.1f}",
                    f"{temp_target:.1f}",
                    f"{boost:.1f}",
                    f"{flow:.2f}",
                    f"{speed:.1f}",
                    f"{pwm:.3f}",
                    f"{pa:.4f}",
                    f"{z_height:.2f}",
                    f"{predicted:.2f}",
                    f"{dynz_active}",
                    f"{accel}",
                    f"{fan_pct}",
                    # Enhanced banding detection columns
                    f"{pa_delta:.5f}",
                    f"{accel_delta}",
                    f"{temp_target_delta:.2f}",
                    f"{temp_overshoot:.2f}",
                    f"{dynz_transition}",
                    f"{layer_transition}",
                    f"{risk}",
                    event_str
                ])
                
                # Update state tracking
                self._last_pa = pa
                self._last_accel = accel
                self._last_temp_target = temp_target
                self._last_dynz_state = dynz_active
                self._last_z = z_height
                
                # Update running stats
                self._log_sample_count += 1
                
                # Track banding risk events
                if 'banding_risk_sum' not in self._log_stats:
                    self._log_stats['banding_risk_sum'] = 0
                    self._log_stats['banding_risk_max'] = 0
                    self._log_stats['high_risk_events'] = 0
                    self._log_stats['accel_changes'] = 0
                    self._log_stats['pa_changes'] = 0
                    self._log_stats['dynz_transitions'] = 0
                    self._log_stats['temp_overshoots'] = 0
                
                self._log_stats['banding_risk_sum'] += risk
                self._log_stats['banding_risk_max'] = max(self._log_stats['banding_risk_max'], risk)
                if risk >= 5:
                    self._log_stats['high_risk_events'] += 1
                if abs(accel_delta) > 500:
                    self._log_stats['accel_changes'] += 1
                if abs(pa_delta) > 0.005:
                    self._log_stats['pa_changes'] += 1
                if dynz_transition != 0:
                    self._log_stats['dynz_transitions'] += 1
                if abs(temp_overshoot) > 5.0:
                    self._log_stats['temp_overshoots'] += 1
                
                # Boost stats
                self._log_stats['boost_sum'] += boost
                self._log_stats['boost_max'] = max(self._log_stats['boost_max'], boost)
                if boost > 0:
                    self._log_stats['boost_count'] += 1
                
                # PWM stats
                self._log_stats['pwm_sum'] += pwm
                self._log_stats['pwm_max'] = max(self._log_stats['pwm_max'], pwm)
                if pwm >= 0.999:
                    self._log_stats['pwm_at_1_count'] += 1
                
                # Temperature stats
                if temp_actual > 50:  # Only track when extruder is hot
                    self._log_stats['temp_min'] = min(self._log_stats['temp_min'], temp_actual)
                    self._log_stats['temp_max'] = max(self._log_stats['temp_max'], temp_actual)
                    self._log_stats['temp_sum'] += temp_actual
                self._log_stats['temp_target_max'] = max(self._log_stats['temp_target_max'], temp_target)
                
                # Flow stats
                self._log_stats['flow_sum'] += flow
                self._log_stats['flow_max'] = max(self._log_stats['flow_max'], flow)
                self._log_stats['speed_max'] = max(self._log_stats['speed_max'], speed)
                
                # PA stats
                if pa > 0:
                    self._log_stats['pa_min'] = min(self._log_stats['pa_min'], pa)
                    self._log_stats['pa_max'] = max(self._log_stats['pa_max'], pa)
                    self._log_stats['pa_sum'] += pa
                
                # Thermal lag stats
                thermal_lag = temp_target - temp_actual
                self._log_stats['thermal_lag_sum'] += thermal_lag
                self._log_stats['thermal_lag_max'] = max(self._log_stats['thermal_lag_max'], thermal_lag)
                
                # DynZ stats
                if dynz_active:
                    self._log_stats['dynz_active_samples'] += 1
                if accel > 0:
                    self._log_stats['accel_min'] = min(self._log_stats['accel_min'], accel)
                
                # Fan/Smart Cooling stats
                self._log_stats['fan_sum'] += fan_pct
                self._log_stats['fan_min'] = min(self._log_stats['fan_min'], fan_pct)
                self._log_stats['fan_max'] = max(self._log_stats['fan_max'], fan_pct)
                if self._log_stats['last_fan'] >= 0 and abs(fan_pct - self._log_stats['last_fan']) >= 3:
                    self._log_stats['fan_adjustments'] += 1
                self._log_stats['last_fan'] = fan_pct
                
                # Flush frequently to prevent data loss (every 10 samples = ~10 seconds)
                # CRITICAL: Also flush first sample to ensure data appears immediately
                if self._log_sample_count == 1 or self._log_sample_count % 10 == 0:
                    self._log_file.flush()
                    os.fsync(self._log_file.fileno())
                    
            except Exception as e:
                logger = logging.getLogger('ExtruderMonitor')
                logger.error(f"Log data error: {e}")
                gcmd.respond_info(f"AT_LOG: Error writing data: {e}")

    def cmd_AT_LOG_END(self, gcmd):
        """End logging session and write summary."""
        logger = logging.getLogger('ExtruderMonitor')
        
        with self._log_lock:
            if not self._log_file:
                gcmd.respond_info("AT_LOG: No active logging session")
                return
            
            try:
                # Calculate final stats
                duration_s = time.time() - self._log_start_time
                samples = self._log_sample_count
                
                if samples > 0:
                    # Build comprehensive summary organized by feature
                    features = self._log_stats.get('features', {})
                    
                    # Calculate derived stats
                    dynz_active_pct = round(100.0 * self._log_stats['dynz_active_samples'] / samples, 1)
                    accel_min = self._log_stats['accel_min'] if self._log_stats['accel_min'] < 999999 else 0
                    boost_active_pct = round(100.0 * self._log_stats['boost_count'] / samples, 1)
                    pwm_maxed_pct = round(100.0 * self._log_stats['pwm_at_1_count'] / samples, 1)
                    temp_min = self._log_stats['temp_min'] if self._log_stats['temp_min'] < 999 else 0
                    temp_range = round(self._log_stats['temp_max'] - temp_min, 1) if temp_min > 0 else 0
                    pa_min = self._log_stats['pa_min'] if self._log_stats['pa_min'] < 999 else 0
                    pa_range = round(self._log_stats['pa_max'] - pa_min, 4) if pa_min > 0 else 0
                    
                    summary = {
                        # Session info
                        'material': self._log_stats['material'],
                        'filename': self._log_stats['filename'],
                        'start_time': self._log_stats['start_time'],
                        'end_time': datetime.now().isoformat(),
                        'duration_min': round(duration_s / 60, 1),
                        'samples': samples,
                        
                        # Features enabled
                        'features': features,
                        
                        # BANDING RISK ANALYSIS (NEW)
                        'banding_analysis': {
                            'avg_risk': round(self._log_stats.get('banding_risk_sum', 0) / samples, 2),
                            'max_risk': self._log_stats.get('banding_risk_max', 0),
                            'high_risk_events': self._log_stats.get('high_risk_events', 0),
                            'accel_changes': self._log_stats.get('accel_changes', 0),
                            'pa_changes': self._log_stats.get('pa_changes', 0),
                            'dynz_transitions': self._log_stats.get('dynz_transitions', 0),
                            'temp_overshoots': self._log_stats.get('temp_overshoots', 0),
                            # Diagnosis hints
                            'likely_culprit': self._diagnose_banding_culprit(),
                        },
                        
                        # Auto-Temperature stats
                        'auto_temp': {
                            'avg_boost': round(self._log_stats['boost_sum'] / samples, 2),
                            'max_boost': round(self._log_stats['boost_max'], 1),
                            'boost_active_pct': boost_active_pct,
                            'temp_min': round(temp_min, 1),
                            'temp_max': round(self._log_stats['temp_max'], 1),
                            'temp_range': temp_range,
                            'temp_target_max': round(self._log_stats['temp_target_max'], 1),
                            'avg_thermal_lag': round(self._log_stats['thermal_lag_sum'] / samples, 2),
                            'max_thermal_lag': round(self._log_stats['thermal_lag_max'], 1),
                        },
                        
                        # Heater stats
                        'heater': {
                            'avg_pwm': round(self._log_stats['pwm_sum'] / samples, 3),
                            'max_pwm': round(self._log_stats['pwm_max'], 3),
                            'pwm_maxed_pct': pwm_maxed_pct,
                        },
                        
                        # Flow stats
                        'flow': {
                            'avg_flow': round(self._log_stats['flow_sum'] / samples, 2),
                            'max_flow': round(self._log_stats['flow_max'], 2),
                            'max_speed': round(self._log_stats['speed_max'], 1),
                        },
                        
                        # Dynamic PA stats
                        'dynamic_pa': {
                            'pa_min': round(pa_min, 4),
                            'pa_max': round(self._log_stats['pa_max'], 4),
                            'pa_range': pa_range,
                            'pa_avg': round(self._log_stats['pa_sum'] / samples, 4) if samples > 0 else 0,
                        },
                        
                        # Dynamic Z-Window stats
                        'dynamic_z': {
                            'active_pct': dynz_active_pct,
                            'accel_min': accel_min,
                        },
                        
                        # Smart Cooling stats
                        'smart_cooling': {
                            'fan_avg': round(self._log_stats['fan_sum'] / samples, 1),
                            'fan_min': self._log_stats['fan_min'],
                            'fan_max': self._log_stats['fan_max'],
                            'fan_adjustments': self._log_stats['fan_adjustments'],
                        },
                        
                        # Legacy flat fields for backward compatibility
                        'avg_boost': round(self._log_stats['boost_sum'] / samples, 2),
                        'max_boost': round(self._log_stats['boost_max'], 1),
                        'avg_pwm': round(self._log_stats['pwm_sum'] / samples, 3),
                        'max_pwm': round(self._log_stats['pwm_max'], 3),
                        'avg_flow': round(self._log_stats['flow_sum'] / samples, 2),
                        'max_flow': round(self._log_stats['flow_max'], 2),
                        'max_speed': round(self._log_stats['speed_max'], 1),
                        'avg_thermal_lag': round(self._log_stats['thermal_lag_sum'] / samples, 2),
                        'dynz_active_pct': dynz_active_pct,
                        'accel_min': accel_min,
                        'fan_avg': round(self._log_stats['fan_sum'] / samples, 1),
                        'fan_min': self._log_stats['fan_min'],
                        'fan_max': self._log_stats['fan_max'],
                    }
                    
                    # Write summary JSON
                    log_path = self._log_file.name
                    summary_path = log_path.replace('.csv', '_summary.json')
                    with open(summary_path, 'w') as f:
                        json.dump(summary, f, indent=2)
                    
                    gcmd.respond_info(f"AT_LOG: Session ended - {samples} samples over {summary['duration_min']}min")
                                        # BANDING RISK SUMMARY (NEW - shown first for visibility)
                    ba = summary['banding_analysis']
                    if ba['high_risk_events'] > 0 or ba['accel_changes'] > 10:
                        gcmd.respond_info(f"⚠️  BANDING RISK DETECTED: {ba['high_risk_events']} high-risk events")
                        gcmd.respond_info(f"  Likely culprit: {ba['likely_culprit']}")
                        gcmd.respond_info(f"  Events: Accel changes:{ba['accel_changes']}, PA changes:{ba['pa_changes']}, DynZ transitions:{ba['dynz_transitions']}, Temp overshoots:{ba['temp_overshoots']}")
                    else:
                        gcmd.respond_info(f"✓ Banding risk low: avg {ba['avg_risk']:.1f}/10, {ba['high_risk_events']} high-risk events")
                                        # Feature summary
                    at = summary['auto_temp']
                    gcmd.respond_info(f"AT_LOG: Temp: {at['temp_min']}-{at['temp_max']}C (range {at['temp_range']}C), Boost avg:{at['avg_boost']}C max:{at['max_boost']}C")
                    
                    h = summary['heater']
                    gcmd.respond_info(f"AT_LOG: Heater: PWM avg:{h['avg_pwm']:.1%} max:{h['max_pwm']:.1%}, at 100%: {h['pwm_maxed_pct']}% of print")
                    
                    f = summary['flow']
                    gcmd.respond_info(f"AT_LOG: Flow: avg:{f['avg_flow']:.1f} max:{f['max_flow']:.1f} mm³/s, Speed max:{f['max_speed']:.0f}mm/s")
                    
                    pa = summary['dynamic_pa']
                    if pa['pa_max'] > 0:
                        gcmd.respond_info(f"AT_LOG: PA: {pa['pa_min']:.4f}-{pa['pa_max']:.4f} (range {pa['pa_range']:.4f})")
                    
                    dz = summary['dynamic_z']
                    if dz['active_pct'] > 0:
                        gcmd.respond_info(f"AT_LOG: DynZ: active {dz['active_pct']}% of print, min accel {dz['accel_min']}")
                    
                    sc = summary['smart_cooling']
                    gcmd.respond_info(f"AT_LOG: Cooling: {sc['fan_min']}-{sc['fan_max']}% (avg {sc['fan_avg']:.0f}%), {sc['fan_adjustments']} adjustments")
                    
                    gcmd.respond_info(f"AT_LOG: Summary saved to {summary_path}")
                    logger.info(f"Print log summary: {summary}")
                
                # Final flush before closing
                self._log_file.flush()
                os.fsync(self._log_file.fileno())
                self._log_file.close()
                
            except Exception as e:
                gcmd.respond_info(f"AT_LOG: Error ending session: {e}")
                logger.error(f"Log end error: {e}")
            
            finally:
                self._log_file = None
                self._log_writer = None
                self._log_start_time = None
                self._log_sample_count = 0
                self._log_stats = {}
    
    def cmd_AT_LOG_STATUS(self, gcmd):
        """Check logging status and provide diagnostics."""
        with self._log_lock:
            if self._log_writer and self._log_file:
                duration = time.time() - self._log_start_time if self._log_start_time else 0
                gcmd.respond_info(f"AT_LOG: ✓ Active - {self._log_sample_count} samples over {duration:.1f}s")
                gcmd.respond_info(f"AT_LOG: File: {self._log_file.name}")
                if self._log_sample_count == 0:
                    gcmd.respond_info("AT_LOG: ⚠️  No data points received yet!")
                    gcmd.respond_info("AT_LOG: Check: 1) Is AUTO_TEMP_LOOP running? 2) Is base_print_temp >= 170? 3) Is at_enabled=True?")
            else:
                gcmd.respond_info("AT_LOG: ✗ Not active - call AT_LOG_START first")
            
            # Check if adaptive flow is enabled
            vars = self.printer.lookup_object('save_variables')
            saved_vars = vars.allVariables if hasattr(vars, 'allVariables') else {}
            at_enabled = saved_vars.get('at_enabled', False)
            base_temp = saved_vars.get('base_print_temp', 0)
            
            gcmd.respond_info(f"AT_LOG: at_enabled={at_enabled}, base_print_temp={base_temp}")
            if not at_enabled:
                gcmd.respond_info("AT_LOG: ⚠️  Adaptive Flow is DISABLED - call AT_START first!")
            if base_temp < 170:
                gcmd.respond_info(f"AT_LOG: ⚠️  base_print_temp too low ({base_temp}°C) - heat nozzle and call AT_START!")
    
    def _diagnose_banding_culprit(self):
        """Analyze stats to identify most likely banding cause."""
        accel_chg = self._log_stats.get('accel_changes', 0)
        pa_chg = self._log_stats.get('pa_changes', 0)
        dynz_trans = self._log_stats.get('dynz_transitions', 0)
        temp_over = self._log_stats.get('temp_overshoots', 0)
        samples = self._log_sample_count
        
        if samples == 0:
            return "insufficient_data"
        
        # Calculate rates per hour of printing
        duration_hrs = (self._log_sample_count / 60) / 60  # samples are ~1/second
        if duration_hrs < 0.01:
            return "print_too_short"
        
        accel_per_hr = accel_chg / duration_hrs
        pa_per_hr = pa_chg / duration_hrs
        dynz_per_hr = dynz_trans / duration_hrs
        temp_per_hr = temp_over / duration_hrs
        
        # Determine DynZ relief method to correctly attribute accel changes
        # When DynZ uses 'temp_reduction', it does NOT change acceleration,
        # so accel changes are from the slicer, not DynZ.
        dynz_uses_accel = False
        try:
            cfg = self.printer.lookup_object('gcode_macro _AUTO_TEMP_CORE')
            relief_method = getattr(cfg, 'variables', {}).get(
                'dynz_relief_method', 'temp_reduction')
            dynz_uses_accel = (str(relief_method).strip("'\"") == 'accel_limit')
        except Exception:
            pass
        
        # Scoring - what's changing most frequently?
        scores = {
            'pa_oscillation': pa_per_hr * 5,
            'temp_instability': temp_per_hr * 3,
            'likely_mechanical': 0  # Default if nothing else scores high
        }
        
        if dynz_uses_accel:
            # DynZ is switching acceleration — attribute accel changes to DynZ
            scores['dynz_accel_switching'] = dynz_per_hr * 10 + accel_per_hr
            if dynz_per_hr > 10:
                scores['dynz_accel_switching'] += 50
        else:
            # DynZ uses temp reduction — it never touches acceleration
            # DynZ transitions may still cause temp swings, score that separately
            scores['dynz_temp_swings'] = dynz_per_hr * 5
            # All accel changes come from slicer G-code
            if accel_per_hr > 50:
                scores['slicer_accel_control'] = accel_per_hr * 2
        
        # Special case: frequent accel changes without DynZ = slicer issue
        # (also relevant in accel_limit mode if DynZ isn't firing)
        if dynz_uses_accel and accel_per_hr > 50 and dynz_per_hr < 5:
            scores['slicer_accel_control'] = accel_per_hr * 2
        
        # Find highest score
        culprit = max(scores, key=scores.get)
        max_score = scores[culprit]
        
        if max_score < 5:
            return "no_obvious_culprit"
        
        return culprit

    def get_status(self, eventtime):
        # Called by Klippy status updates; include predicted rate if available
        status = {}

        pred_rate = self._predicted_extrusion_rate()
        status['predicted_extrusion_rate'] = pred_rate
        
        return status


def load_config(config):
    return ExtruderMonitor(config)
