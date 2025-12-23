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
    predicted extrusion rate and an estimated load based on current SG.
    """

    def __init__(self, config):
        self.printer = config.get_printer()
        self.driver_name = config.get("driver_name", None)  # Optional - not required for velocity-based mode
        self.printer.register_event_handler("klippy:connect", self.handle_connect)

        # lookahead buffer and bookkeeping
        self._lookahead_lock = threading.Lock()
        self._lookahead = deque()  # entries are (e_delta_mm, duration_s, timestamp)

        # Travel tracking for diagnostics
        self._travel_lock = threading.Lock()
        self._pending_travel = 0.0  # Accumulated travel distance without extrusion (mm)
        self._last_move_was_travel = False  # Track if last move was non-extruding
        self._travel_start_time = None  # When travel sequence started

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
        
        # Corner detection for PA learning
        self._last_move_vector = None  # (dx, dy) of previous move
        self._corner_events = deque(maxlen=50)  # (timestamp, angle_degrees, had_extrusion)
        self._corner_lock = threading.Lock()
        
        # Print session logging
        self._log_lock = threading.Lock()
        self._log_file = None
        self._log_writer = None
        self._log_start_time = None
        self._log_sample_count = 0
        self._log_stats = {}  # Running stats for summary

    def handle_connect(self):
        # TMC driver is optional - only needed for load sensing (deprecated)
        self.tmc = None
        if self.driver_name:
            try:
                self.tmc = self.printer.lookup_object(self.driver_name)
            except Exception:
                logging.getLogger('ExtruderMonitor').info(
                    f"TMC driver '{self.driver_name}' not found - load sensing disabled (velocity mode only)")

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

        # Travel tracking for diagnostics
        with self._travel_lock:
            if has_extrusion:
                # Extrusion detected - reset travel accumulator
                self._pending_travel = 0.0
                self._last_move_was_travel = False
                self._travel_start_time = None
            elif dist > 0.1:  # Travel move (no extrusion, significant distance)
                # Accumulate travel distance
                if not self._last_move_was_travel:
                    # Starting a new travel sequence
                    self._travel_start_time = time.time()
                self._pending_travel += dist
                self._last_move_was_travel = True

        # Corner detection for PA learning
        # Only track corners during extrusion moves with significant XY movement
        if has_extrusion and dist > 0.5:
            current_vector = (dx, dy)
            if self._last_move_vector is not None:
                # Calculate angle between vectors
                angle = self._calculate_corner_angle(self._last_move_vector, current_vector)
                if angle is not None and angle > 45:  # Sharp corner threshold
                    with self._corner_lock:
                        self._corner_events.append((time.time(), angle, True))
            self._last_move_vector = current_vector
        elif not has_extrusion:
            # Reset vector tracking on travel moves
            self._last_move_vector = None

        # update stored state
        if cur_e is not None:
            self._gcode_last_e = cur_e
        if cur_f is not None:
            self._gcode_last_f = cur_f
        for axis in ('X','Y','Z'):
            if axis in params:
                self._gcode_pos[axis] = params[axis]

    def _calculate_corner_angle(self, v1, v2):
        """Calculate angle between two 2D vectors in degrees. Returns None if invalid."""
        import math
        try:
            # Normalize vectors
            mag1 = math.sqrt(v1[0]**2 + v1[1]**2)
            mag2 = math.sqrt(v2[0]**2 + v2[1]**2)
            if mag1 < 0.01 or mag2 < 0.01:
                return None
            
            # Dot product gives cos(angle)
            dot = (v1[0]*v2[0] + v1[1]*v2[1]) / (mag1 * mag2)
            # Clamp to [-1, 1] to avoid math domain errors
            dot = max(-1.0, min(1.0, dot))
            angle_rad = math.acos(dot)
            angle_deg = math.degrees(angle_rad)
            
            # Return the deviation from straight (180° = straight, 90° = right angle)
            return 180.0 - angle_deg
        except Exception:
            return None
    
    def get_recent_corners(self, max_age=5.0):
        """Return list of recent corner events within max_age seconds."""
        now = time.time()
        with self._corner_lock:
            return [(ts, angle, ext) for ts, angle, ext in self._corner_events 
                    if (now - ts) <= max_age]
    
    def get_corner_count(self, max_age=5.0):
        """Return count of corners detected in the last max_age seconds."""
        return len(self.get_recent_corners(max_age))

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
                
                # Write header
                self._log_writer.writerow([
                    'elapsed_s', 'temp_actual', 'temp_target', 'boost',
                    'flow', 'speed', 'pwm', 'pa', 'z_height', 'predicted_flow'
                ])
                
                self._log_start_time = time.time()
                self._log_sample_count = 0
                self._log_stats = {
                    'material': material,
                    'filename': filename,
                    'start_time': datetime.now().isoformat(),
                    'boost_sum': 0.0,
                    'boost_max': 0.0,
                    'pwm_sum': 0.0,
                    'pwm_max': 0.0,
                    'flow_sum': 0.0,
                    'flow_max': 0.0,
                    'speed_max': 0.0,
                    'thermal_lag_sum': 0.0,
                }
                
                gcmd.respond_info(f"AT_LOG: Started logging to {log_path}")
                logger.info(f"Started print logging: {log_path}")
                
            except Exception as e:
                gcmd.respond_info(f"AT_LOG: Failed to start logging: {e}")
                logger.error(f"Failed to start logging: {e}")

    def cmd_AT_LOG_DATA(self, gcmd):
        """Log a single data point during printing."""
        with self._log_lock:
            if not self._log_writer:
                return  # Logging not active
            
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
                    f"{predicted:.2f}"
                ])
                
                # Update running stats
                self._log_sample_count += 1
                self._log_stats['boost_sum'] += boost
                self._log_stats['boost_max'] = max(self._log_stats['boost_max'], boost)
                self._log_stats['pwm_sum'] += pwm
                self._log_stats['pwm_max'] = max(self._log_stats['pwm_max'], pwm)
                self._log_stats['flow_sum'] += flow
                self._log_stats['flow_max'] = max(self._log_stats['flow_max'], flow)
                self._log_stats['speed_max'] = max(self._log_stats['speed_max'], speed)
                self._log_stats['thermal_lag_sum'] += (temp_target - temp_actual)
                
                # Flush periodically
                if self._log_sample_count % 60 == 0:
                    self._log_file.flush()
                    
            except Exception as e:
                logging.getLogger('ExtruderMonitor').debug(f"Log data error: {e}")

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
                    summary = {
                        'material': self._log_stats['material'],
                        'filename': self._log_stats['filename'],
                        'start_time': self._log_stats['start_time'],
                        'end_time': datetime.now().isoformat(),
                        'duration_min': round(duration_s / 60, 1),
                        'samples': samples,
                        'avg_boost': round(self._log_stats['boost_sum'] / samples, 2),
                        'max_boost': round(self._log_stats['boost_max'], 1),
                        'avg_pwm': round(self._log_stats['pwm_sum'] / samples, 3),
                        'max_pwm': round(self._log_stats['pwm_max'], 3),
                        'avg_flow': round(self._log_stats['flow_sum'] / samples, 2),
                        'max_flow': round(self._log_stats['flow_max'], 2),
                        'max_speed': round(self._log_stats['speed_max'], 1),
                        'avg_thermal_lag': round(self._log_stats['thermal_lag_sum'] / samples, 2),
                    }
                    
                    # Write summary JSON
                    log_path = self._log_file.name
                    summary_path = log_path.replace('.csv', '_summary.json')
                    with open(summary_path, 'w') as f:
                        json.dump(summary, f, indent=2)
                    
                    gcmd.respond_info(f"AT_LOG: Session ended - {samples} samples over {summary['duration_min']}min")
                    gcmd.respond_info(f"AT_LOG: Avg boost: {summary['avg_boost']}C, Max: {summary['max_boost']}C")
                    gcmd.respond_info(f"AT_LOG: Avg PWM: {summary['avg_pwm']:.1%}, Max: {summary['max_pwm']:.1%}")
                    gcmd.respond_info(f"AT_LOG: Summary saved to {summary_path}")
                    logger.info(f"Print log summary: {summary}")
                
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

    def get_status(self, eventtime):
        # Called by Klippy status updates; include predicted rate if available
        status = {}

        pred_rate = self._predicted_extrusion_rate()
        status['predicted_extrusion_rate'] = pred_rate
        
        # Travel tracking for diagnostics
        with self._travel_lock:
            status['pending_travel'] = self._pending_travel
            status['in_travel'] = self._last_move_was_travel
            if self._travel_start_time is not None:
                status['travel_duration'] = time.time() - self._travel_start_time
            else:
                status['travel_duration'] = 0.0
        
        # Corner detection for PA learning
        status['corner_count_5s'] = self.get_corner_count(5.0)
        recent_corners = self.get_recent_corners(5.0)
        if recent_corners:
            status['last_corner_angle'] = recent_corners[-1][1]
            status['last_corner_age'] = eventtime - recent_corners[-1][0] if hasattr(eventtime, '__float__') else time.time() - recent_corners[-1][0]
        else:
            status['last_corner_angle'] = 0
            status['last_corner_age'] = -1
        
        return status


def load_config(config):
    return ExtruderMonitor(config)
