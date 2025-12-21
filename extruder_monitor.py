import logging
import threading
import time
import json
import os
from collections import deque

# Community defaults configuration
COMMUNITY_DEFAULTS_URL = "https://raw.githubusercontent.com/barnard704344/Klipper-Adaptive-Flow/main/community_defaults.json"
CACHE_FILE = "~/printer_data/config/adaptive_flow_community_cache.json"
CACHE_MAX_AGE_HOURS = 24


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
        
        # Community defaults - fetch/cache on startup
        self._community_defaults = None
        self._load_community_defaults()
        
        # Corner detection for PA learning
        self._last_move_vector = None  # (dx, dy) of previous move
        self._corner_events = deque(maxlen=50)  # (timestamp, angle_degrees, had_extrusion)
        self._corner_lock = threading.Lock()

    def _load_community_defaults(self):
        """Load community defaults from cache or fetch from GitHub."""
        cache_path = os.path.expanduser(CACHE_FILE)
        logger = logging.getLogger('ExtruderMonitor')
        
        # Try to load from cache first
        try:
            if os.path.exists(cache_path):
                cache_age_hours = (time.time() - os.path.getmtime(cache_path)) / 3600
                if cache_age_hours < CACHE_MAX_AGE_HOURS:
                    with open(cache_path, 'r') as f:
                        self._community_defaults = json.load(f)
                        logger.info(f"Loaded community defaults from cache (age: {cache_age_hours:.1f}h)")
                        return
        except Exception as e:
            logger.debug(f"Cache read failed: {e}")
        
        # Fetch from GitHub (in background thread to not block startup)
        def fetch_defaults():
            try:
                import urllib.request
                import ssl
                
                # Create SSL context that works on most systems
                ctx = ssl.create_default_context()
                
                req = urllib.request.Request(
                    COMMUNITY_DEFAULTS_URL,
                    headers={'User-Agent': 'Klipper-Adaptive-Flow/1.0'}
                )
                
                with urllib.request.urlopen(req, timeout=10, context=ctx) as response:
                    data = json.loads(response.read().decode('utf-8'))
                    
                    # Save to cache
                    try:
                        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                        with open(cache_path, 'w') as f:
                            json.dump(data, f, indent=2)
                    except Exception as e:
                        logger.debug(f"Cache write failed: {e}")
                    
                    self._community_defaults = data
                    logger.info(f"Fetched community defaults v{data.get('_version', 'unknown')}")
                    
            except Exception as e:
                logger.info(f"Community defaults fetch skipped (offline or unavailable): {e}")
                # Try to load stale cache as fallback
                try:
                    if os.path.exists(cache_path):
                        with open(cache_path, 'r') as f:
                            self._community_defaults = json.load(f)
                            logger.info("Using stale cache as fallback")
                except:
                    pass
        
        # Run fetch in background
        thread = threading.Thread(target=fetch_defaults, daemon=True)
        thread.start()
    
    def get_community_material(self, material, use_high_flow=True):
        """Get community defaults for a material. Returns dict or None."""
        if not self._community_defaults:
            return None
        
        materials = self._community_defaults.get('materials', {})
        
        # Try exact match first
        mat_data = materials.get(material.upper())
        if not mat_data:
            # Try partial match
            for mat_name, data in materials.items():
                if mat_name in material.upper():
                    mat_data = data
                    break
        
        if not mat_data:
            return None
        
        # Return the appropriate nozzle variant
        nozzle_key = 'high_flow' if use_high_flow else 'standard'
        result = mat_data.get(nozzle_key, {}).copy()
        result['default_pa'] = mat_data.get('default_pa', 0.04)
        result['description'] = mat_data.get('description', '')
        return result

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
        gcode.register_command("GET_COMMUNITY_DEFAULTS", self.cmd_GET_COMMUNITY_DEFAULTS,
                               desc="Get community defaults for a material: GET_COMMUNITY_DEFAULTS MATERIAL=PLA HF=1")

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

    def cmd_GET_COMMUNITY_DEFAULTS(self, gcmd):
        """Get community defaults for a material."""
        material = gcmd.get('MATERIAL', 'PLA').upper()
        use_hf = gcmd.get_int('HF', 1) == 1
        
        data = self.get_community_material(material, use_hf)
        
        if data:
            nozzle_type = "High Flow" if use_hf else "Standard"
            gcmd.respond_info(f"Community defaults for {material} ({nozzle_type}):")
            gcmd.respond_info(f"  speed_k: {data.get('speed_k', 'N/A')}")
            gcmd.respond_info(f"  flow_gate: {data.get('flow_gate', 'N/A')} mm³/s")
            gcmd.respond_info(f"  max_temp: {data.get('max_temp', 'N/A')}°C")
            gcmd.respond_info(f"  default_pa: {data.get('default_pa', 'N/A')}")
            if data.get('description'):
                gcmd.respond_info(f"  note: {data['description']}")
        else:
            if self._community_defaults:
                gcmd.respond_info(f"No community defaults for '{material}'")
                available = list(self._community_defaults.get('materials', {}).keys())
                gcmd.respond_info(f"Available: {', '.join(available)}")
            else:
                gcmd.respond_info("Community defaults not loaded (offline or unavailable)")

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
        
        # Community defaults status
        status['community_defaults_loaded'] = self._community_defaults is not None
        if self._community_defaults:
            status['community_defaults_version'] = self._community_defaults.get('_version', 'unknown')
        
        return status


def load_config(config):
    return ExtruderMonitor(config)
