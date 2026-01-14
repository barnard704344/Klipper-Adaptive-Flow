# Klipper G-code Interceptor Module
# Place in klippy/extras/ and add [gcode_interceptor] to printer.cfg
#
# This module intercepts all G-code commands and fires events that
# other modules can subscribe to.

import logging


class GCodeInterceptor:
    """Intercept G-code commands and broadcast events to subscribers."""

    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.logger = logging.getLogger('GCodeInterceptor')
        
        # List of callback functions to notify
        self._subscribers = []
        
        # Register event for when Klippy is ready
        self.printer.register_event_handler('klippy:ready', self._handle_ready)

    def _handle_ready(self):
        """Hook into gcode processing after Klipper is fully initialized."""
        # Wrap the gcode script runner to intercept commands
        self._wrap_gcode_dispatch()
        self.logger.info("GCodeInterceptor: Ready and intercepting G-code")

    def _wrap_gcode_dispatch(self):
        """Wrap the gcode run_script_from_command to intercept all commands."""
        # Store original methods
        original_run_script = self.gcode.run_script_from_command
        original_run_script_async = getattr(self.gcode, 'run_script', None)
        
        interceptor = self

        def wrapped_run_script(script):
            """Intercept script before processing."""
            for line in script.split('\n'):
                line = line.strip()
                if line and not line.startswith(';'):
                    interceptor._notify_subscribers(line)
            return original_run_script(script)

        # Replace the method
        self.gcode.run_script_from_command = wrapped_run_script
        
        # Also wrap run_script if it exists (used by macros)
        if original_run_script_async:
            def wrapped_run_script_async(script):
                for line in script.split('\n'):
                    line = line.strip()
                    if line and not line.startswith(';'):
                        interceptor._notify_subscribers(line)
                return original_run_script_async(script)
            self.gcode.run_script = wrapped_run_script_async

    def _notify_subscribers(self, gcode_line):
        """Notify all subscribers of an incoming G-code line."""
        for callback in self._subscribers:
            try:
                callback(gcode_line)
            except Exception as e:
                self.logger.warning(f"GCodeInterceptor: Subscriber error: {e}")

    def register_gcode_callback(self, callback):
        """Register a callback to receive G-code lines.
        
        Args:
            callback: Function that takes a single string argument (the G-code line)
        
        Usage from another module:
            interceptor = printer.lookup_object('gcode_interceptor')
            interceptor.register_gcode_callback(my_handler)
        """
        if callback not in self._subscribers:
            self._subscribers.append(callback)
            self.logger.info(f"GCodeInterceptor: Registered callback {callback}")

    def unregister_gcode_callback(self, callback):
        """Remove a previously registered callback."""
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def get_status(self, eventtime):
        """Report status to Klipper."""
        return {
            'subscriber_count': len(self._subscribers),
            'active': True
        }


def load_config(config):
    return GCodeInterceptor(config)
