import logging
import threading
import time
from collections import deque


class ExtruderMonitor:
    """Monitor extruder load and accept simple lookahead segments.

    This module provides a small lookahead buffer that can be fed with
    upcoming extrusion segments (E delta in mm and duration in seconds).
    It exposes G-code commands to add/clear segments and to query the
    predicted extrusion rate and an estimated load based on current SG.
    """

    def __init__(self, config):
        self.printer = config.get_printer()
        config.get("stepper", "extruder")
        self.driver_name = config.get("driver_name", "tmc2209 extruder")
        self.printer.register_event_handler("klippy:connect", self.handle_connect)

        # lookahead buffer and bookkeeping
        self._lookahead_lock = threading.Lock()
        self._lookahead = deque()  # entries are (e_delta_mm, duration_s, timestamp)

        # Travel tracking for ooze prevention
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

    def handle_connect(self):
        try:
            self.tmc = self.printer.lookup_object(self.driver_name)
        except Exception as e:
            raise self.printer.config_error(f"ExtruderMonitor: Driver '{self.driver_name}' not found.")

        gcode = self.printer.lookup_object('gcode')
        gcode.register_command("GET_EXTRUDER_LOAD", self.cmd_GET_EXTRUDER_LOAD,
                               desc="Get current extruder load")
        gcode.register_command("SET_LOOKAHEAD", self.cmd_SET_LOOKAHEAD,
                               desc="Add upcoming extrusion segment: SET_LOOKAHEAD E=<mm> D=<s> or SET_LOOKAHEAD CLEAR")
        gcode.register_command("GET_PREDICTED_LOAD", self.cmd_GET_PREDICTED_LOAD,
                               desc="Get predicted extrusion rate and estimated load using lookahead")
        gcode.register_command("CALIBRATE_BASELINE", self.cmd_CALIBRATE_BASELINE,
                               desc="Start baseline calibration - clears sample buffer")
        gcode.register_command("SAMPLE_LOAD", self.cmd_SAMPLE_LOAD,
                               desc="Add a load sample to calibration buffer")
        gcode.register_command("FINISH_CALIBRATION", self.cmd_FINISH_CALIBRATION,
                               desc="Calculate and save baseline from collected samples")
        
        # Calibration state
        self._calibration_samples = []
        self._calibration_active = False
        self._last_calibrated_baseline = -1

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

        # Track travel vs extrusion for ooze prevention
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

        # Travel tracking for ooze prevention
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
        # Expire stale entries older than 2 seconds
        now = time.time()
        max_age = 2.0
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

    def _read_current_load(self):
        """Read StallGuard result from TMC driver."""
        try:
            if hasattr(self, 'tmc'):
                mcu_tmc = self.tmc.mcu_tmc
                # Method 1: Try get_register with SG_RESULT (TMC2209/2226)
                try:
                    reg_val = mcu_tmc.get_register('SG_RESULT')
                    # SG_RESULT register: bits 0-9 contain the value
                    val = reg_val & 0x3FF
                    return int(val)
                except Exception:
                    pass
                # Method 2: Use fields helper if available
                try:
                    if hasattr(mcu_tmc, 'fields'):
                        val = mcu_tmc.fields.get_field('sg_result')
                        return int(val)
                except Exception:
                    pass
        except Exception:
            pass
        return -1

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
            cur_load = self._read_current_load()

            # Estimate future load by scaling current load by rate ratio using
            # a recent-rate baseline (fallback to pred_rate if no history).
            baseline = None
            if len(self._recent_rates) > 0:
                baseline = sum(self._recent_rates) / len(self._recent_rates)
            else:
                baseline = pred_rate if pred_rate > 0 else 1.0

            est_load = -1
            if cur_load >= 0 and baseline > 0:
                try:
                    est_load = int(cur_load * (pred_rate / float(baseline)))
                except Exception:
                    est_load = cur_load

            gcmd.respond_info(f'Predicted rate: {pred_rate:.3f} mm/s, Estimated load: {est_load}')
        except Exception as exc:
            gcmd.respond_info(f'Error computing predicted load: {str(exc)}')

    def cmd_GET_EXTRUDER_LOAD(self, gcmd):
        try:
            val = self._read_current_load()
            gcmd.respond_info(f"Extruder Load (SG_RESULT): {val}")
        except Exception as e:
            gcmd.respond_info(f"Error reading TMC: {str(e)}")

    def cmd_CALIBRATE_BASELINE(self, gcmd):
        """Start baseline calibration - clears sample buffer."""
        self._calibration_samples = []
        self._calibration_active = True
        gcmd.respond_info('Baseline calibration started. Samples will be collected.')
        gcmd.respond_info('Run SAMPLE_LOAD during extrusion, then FINISH_CALIBRATION when done.')

    def cmd_SAMPLE_LOAD(self, gcmd):
        """Take a load sample and add to calibration buffer."""
        if not self._calibration_active:
            gcmd.respond_info('No calibration in progress. Run CALIBRATE_BASELINE first.')
            return
        try:
            val = self._read_current_load()
            if val >= 0:
                self._calibration_samples.append(val)
                gcmd.respond_info(f'Sample {len(self._calibration_samples)}: SG_RESULT={val}')
            else:
                gcmd.respond_info('Failed to read load (got -1)')
        except Exception as e:
            gcmd.respond_info(f'Error sampling: {str(e)}')

    def cmd_FINISH_CALIBRATION(self, gcmd):
        """Calculate baseline from collected samples and report result."""
        if not self._calibration_active:
            gcmd.respond_info('No calibration in progress.')
            return
        
        if len(self._calibration_samples) < 3:
            gcmd.respond_info(f'Need at least 3 samples (have {len(self._calibration_samples)}). Keep sampling.')
            return
        
        # Calculate statistics
        samples = self._calibration_samples
        avg = sum(samples) / len(samples)
        min_val = min(samples)
        max_val = max(samples)
        
        # Remove outliers (values outside 2 std dev) and recalculate
        if len(samples) >= 5:
            std_dev = (sum((x - avg) ** 2 for x in samples) / len(samples)) ** 0.5
            filtered = [x for x in samples if abs(x - avg) <= 2 * std_dev]
            if len(filtered) >= 3:
                avg = sum(filtered) / len(filtered)
                gcmd.respond_info(f'Filtered {len(samples) - len(filtered)} outlier(s)')
        
        baseline = int(round(avg))
        
        self._calibration_active = False
        self._last_calibrated_baseline = baseline
        
        gcmd.respond_info('=' * 50)
        gcmd.respond_info(f'CALIBRATION COMPLETE')
        gcmd.respond_info(f'Samples: {len(samples)} | Min: {min_val} | Max: {max_val}')
        gcmd.respond_info(f'>>> BASELINE: {baseline} <<<')
        gcmd.respond_info('')
        gcmd.respond_info('To apply, run:')
        gcmd.respond_info(f'  SAVE_VARIABLE VARIABLE=sensor_baseline VALUE={baseline}')
        gcmd.respond_info('Or update auto_flow.cfg:')
        gcmd.respond_info(f'  variable_sensor_baseline: {baseline}')
        gcmd.respond_info('=' * 50)

    def get_status(self, eventtime):
        # Called by Klippy status updates; include predicted rate if available
        status = {'load': -1}
        try:
            status['load'] = self._read_current_load()
        except:
            status['load'] = -1

        pred_rate = self._predicted_extrusion_rate()
        status['predicted_extrusion_rate'] = pred_rate
        
        # Include calibration info
        status['calibration_active'] = getattr(self, '_calibration_active', False)
        status['calibration_samples'] = len(getattr(self, '_calibration_samples', []))
        status['last_calibrated_baseline'] = getattr(self, '_last_calibrated_baseline', -1)
        
        # Travel tracking for ooze prevention
        with self._travel_lock:
            status['pending_travel'] = self._pending_travel
            status['in_travel'] = self._last_move_was_travel
            if self._travel_start_time is not None:
                status['travel_duration'] = time.time() - self._travel_start_time
            else:
                status['travel_duration'] = 0.0
        
        return status


def load_config(config):
    return ExtruderMonitor(config)
