Adaptive Flow — Live G-code Lookahead
=====================================

This repository provides an adaptive temperature/flow monitor and a live
G-code lookahead feature for Klipper-based printers. The key goal is to
predict upcoming extrusion demand so `auto_flow.cfg` can apply better
temperature/flow adjustments.

Files to keep
-------------
- `extruder_monitor.py` — core Klipper module (lookahead, SG reads, G-code commands).
- `auto_flow.cfg` — main adaptive flow macros (now includes lookahead macros).

Optional/support files
----------------------
(none)

Install / Deploy (host running Klipper)
--------------------------------------
1. Copy `extruder_monitor.py` to the Klipper extras directory on the machine running Klipper (e.g., Raspberry Pi). Common locations:
   - If you installed Klipper from source: `~/klipper/klippy/extras/`
   - If using an OS-packaged install, consult your Klipper installation layout.

   Example (from printer host):
   ```bash
   mkdir -p ~/klipper/klippy/extras
   cp /path/to/repo/extruder_monitor.py ~/klipper/klippy/extras/
   ```

2. Restart Klipper service so it loads the new module:
   ```bash
   sudo service klipper restart
   ```
   or use the restart command relevant to your system.

3. Ensure `auto_flow.cfg` (or the macros you prefer) are included in your `printer.cfg`.
   - Either copy the contents of `auto_flow.cfg` into your main `printer.cfg`, or add an `include` directive if your setup supports it:
     ```ini
     include auto_flow.cfg
     ```
   - The file already includes `AUTO_LOOKAHEAD` macros and instructions.


