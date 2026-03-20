# Project Audit — Klipper Adaptive Flow

**Audit date:** 2026-03-10  
**Scope:** Every file in the repository, what it does, and whether it is earning its place.

---

## What the project is (one paragraph)

Klipper Adaptive Flow is a Klipper firmware add-on for **E3D Revo hotends only**. It watches
volumetric flow rate, print speed, and acceleration in real time and automatically adjusts nozzle
temperature and Pressure Advance (PA) to match. A 5-second lookahead lets it
pre-heat before flow spikes arrive. After each print a browser dashboard (`analyze_print.py`) shows
charts, detects banding culprits, reads your slicer G-code for setting errors, and offers one-click
fixes. The whole thing is self-updating via `update.sh`.

---

## Python Scripts

### `gcode_interceptor.py` (95 lines)

- Klipper "extra" module (lives in `~/klipper/klippy/extras/`).
- Wraps Klipper's internal `gcode.run_script()` and `gcode.run_script_from_command()` at
  boot-time so every G-code command can be intercepted before execution.
- Provides a `register_gcode_callback(fn)` / `unregister_gcode_callback(fn)` API so other
  modules can subscribe to the live G-code stream without patching Klipper internals.
- Exposes `get_status()` to the Klipper status API (subscriber count, active flag).
- **Current state:** The interception infrastructure is complete and working, but nothing in the
  repo currently calls `register_gcode_callback`. The module is wired in via `[gcode_interceptor]`
  in `printer.cfg` and is a no-op until a consumer subscribes.

### `extruder_monitor.py` (1,001 lines)

- Klipper extra module that is the **data source** for the entire system.
- Reads extruder motor velocity from Klipper's stepper kinematics and converts it to volumetric
  flow rate (mm³/s) using the filament cross-section (1.75 mm filament = 2.405 mm²).
- Applies configurable exponential smoothing (`smoothing = 0.35` default) to reduce oscillation.
- Maintains a **5-second lookahead buffer**: parses upcoming G-code moves
  (`(e_delta_mm, duration_s, timestamp)` tuples) so `_AUTO_TEMP_CORE` can pre-heat before a
  flow spike arrives.
- Tracks print state: relative vs absolute extrusion mode (M82/M83), current XYZ/E/F position.
- Writes a **CSV log** for every print session to
  `~/printer_data/logs/adaptive_flow/<timestamp>.csv` with columns:
  `elapsed_s, temp_actual, temp_target, boost, flow, speed, pwm, pa, z_height,
  predicted_flow, dynz_active, accel, fan_pct, pa_delta, accel_delta,
  temp_target_delta, temp_overshoot, dynz_transition, layer_transition,
  banding_risk, event_flags`.
- Keeps the last 20 CSV logs; older ones are deleted automatically.
- Exposes two G-code commands: `EXTRUDER_MONITOR_STATUS` (live stats) and
  `EXTRUDER_MONITOR_RESET` (clear counters).

### `analyze_print.py` — web dashboard server and main entry point

The main entry point, now supported by four sub-modules (`af_config.py`, `af_analysis.py`,
`af_hardware.py`, `af_slicer.py`) that were split out for maintainability.

**A. Config read/write helpers** (now in `af_config.py`)
- Parses `auto_flow_user.cfg` and `material_profiles_*.cfg` in INI format.
- `_get_config_value(variable, material)` — reads current live value (user file overrides
  defaults file).
- `_apply_config_change(variable, new_value, material)` — writes a single variable back to the
  user config file directly from the dashboard "Apply" button.
- Logs every config change (old value, new value, timestamp) to
  `config_changes_log.json` so the dashboard can track which recommendations have been applied
  and how many prints have completed since.

**B. Hardware-aware Klipper config parser** (now in `af_hardware.py`)
- `collect_printer_hardware(config_dir)` — reads `printer.cfg` and follows all `[include]`
  directives one level deep.
- Extracts: kinematics type, max accel/velocity, build volume, Z-stepper count (quad gantry
  detection), extruder rotation distance (direct-drive vs Bowden detection), nozzle diameter,
  thermistor type, TMC driver and run current, input shaper frequencies, part fan `max_power`
  cap, probe type, and whether an MMU is present.
- This hardware dict is passed into all recommendation generators so advice is specific to your
  printer (e.g. "your fan is capped at 40%, your CoreXY input shaper limits accel to 6040 mm/s²").

**C. Slicer G-code parser** (now in `af_slicer.py`)
- `extract_slicer_settings(gcode_path)` — reads the G-code footer comments written by
  OrcaSlicer/PrusaSlicer/SuperSlicer and extracts: default acceleration, outer wall accel/speed,
  bridge flow, max volumetric speed, layer height, line widths, fan settings, PA value.
- `_find_gcode_for_summary(summary)` — matches a print summary to its G-code file on disk by
  filename, with fuzzy matching for time-estimate suffixes.

**D. Single-print analysis functions** (now in `af_analysis.py`, ~25 functions)
- `find_latest_summary` / `load_summary` — locate and load per-print JSON summary.
- `compute_extrusion_quality(timeline)` — flow stability (std dev), PA consistency,
  temperature tracking error, heater headroom, Speed Guard activation percentage.
- `analyze_csv_for_banding` — groups consecutive acceleration spike events into "episodes"
  and diagnoses each one (accel too high, flow inconsistency, feature transition, Speed Guard switch).
- `analyze_z_banding` — bins CSV rows by Z-height (0.5 mm bins), computes risk per bin
  (accel events, flow variability, PWM spikes), returns a heatmap of problem layers.
- `analyze_thermal_lag` — compares `target_temp` vs `actual_temp` over time, flags episodes
  where the heater is more than 3 °C behind target.
- `analyze_heater_headroom` — groups duty cycle by flow rate brackets (0-5, 5-10, 10-15,
  15-20 mm³/s), flags brackets where mean PWM exceeds 90%.
- `analyze_pa_stability` — rolling-window PA consistency; detects oscillation > 0.005 per cycle.
- `analyze_dynz_zones` — maps Speed Guard activation events to Z-height.
- `analyze_speed_flow_distribution` — speed and flow histograms showing where the printer
  spends its time.

**E. Slicer diagnostics** (now in `af_slicer.py`, ~1,250 lines)
- `analyze_slicer_vs_banding` — cross-references the acceleration fingerprint from the CSV
  against slicer settings to map observed accelerations to slicer features (infill, walls,
  supports etc.) and detect mismatches that cause banding.
- `generate_slicer_profile_advice` — comprehensive per-setting advisor covering: accel values
  vs hardware limits, wall speeds vs heater capacity, material-specific warnings (e.g. PETG
  stringing at high speed), bridge settings, and fan cooling adequacy.

**F. Cross-print aggregate analysis** (remains in `analyze_print.py`)
- `find_recent_sessions` — gather N most recent print sessions, optionally filtered by material.
- `aggregate_banding_analysis` — merges banding data across multiple prints to surface
  recurring problem zones.
- Per-tab aggregate generators for: thermal, PA stability, Speed Guard, speed/flow distribution, and
  cross-session trend lines.

**G. Recommendation engine** (remains in `analyze_print.py`)
- `generate_recommendations(summary, csv_file, slicer_settings, printer_hw)` — runs all
  analyzers and collects their outputs into a ranked list of `{title, detail, action, variable,
  suggested_value, severity}` dicts.
- `_suggest_change` — helper that applies `minimum`/`maximum` guards before recommending a
  delta change.

**H. Web dashboard server** (remains in `analyze_print.py`)
- When run with `--serve --port 7127` (as the systemd service does), launches a lightweight
  HTTP server.
- Serves a single-page HTML/JS dashboard generated entirely in Python (no external web
  framework).
- The page uses **Chart.js v4** (CDN) for all charts and vanilla JS for interactivity.
- Dashboard tabs: Recommendations, Slicer, Timeline, Z-Height, Heater, PA, Speed Guard,
  Distribution, Trends.
- Summary cards: Material, Temp Boost, Heater Duty, Speed Guard, Banding.
- Session selector dropdown; live-print detection (CSV modified within 2 minutes → "LIVE"
  badge, 5-second auto-refresh).
- Material aggregate mode: switches all tabs to pooled data across all prints of one material.
- "Apply" button posts a config change to the server, writes the file, and logs the change.
- All chart descriptions, tooltips, and "good vs bad" guides are embedded inline in the HTML.

**I. CLI mode**
- `python3 analyze_print.py` (no flags) — prints a single-print health summary to stdout.
- `python3 analyze_print.py --count 10` — multi-print banding analysis across last 10 prints.
- No external API keys or network access required for any analysis.

---

## Configuration Files

### `auto_flow_defaults.cfg` (1,386 lines) — the main control loop

- **System-managed.** Updated by `git pull` via `update.sh`; users must not edit this file.
- Contains one enormous macro, `[gcode_macro _AUTO_TEMP_CORE]`, which is the real-time
  control loop (~700 lines of Klipper Jinja2 G-code). Called every ~0.5 s during a print.
- **What the core loop does:**
  - Reads extruder velocity from `printer.motion_report` and calculates volumetric flow.
  - Reads lookahead flow prediction from `extruder_monitor`.
  - Applies exponential smoothing to the flow reading.
  - Calculates temperature boost:
    `boost = (excess_flow × flow_k) + (excess_speed × speed_boost_k) + accel_factor`
  - Caps boost at `max_boost` and `max_temp`.
  - Adjusts PA in real time: `PA = PA_base − (boost × pa_boost_k)` (higher temp = lower
    viscosity = less PA needed).
  - Monitors heater PWM duty cycle; reduces boost demand when duty > 95% to prevent
    thermal fault.
  - Runs Speed Guard: tracks stress score per Z-bin, applies temperature reduction or accel limit
    when score exceeds threshold.
  - Manages multi-object temperature transitions (sequential print mode).
  - Skips boosting on the first layer for consistent first-layer squish.
- **Hardware auto-scaling:** On `AT_START`, all 40 W base parameters (`flow_k`, `max_boost`,
  `ramp_rise`) are multiplied by `1.3` if `heater_wattage > 40`. HF nozzle users get
  `PA × 1.40`, wider `smooth_time`, and a 5 °C temperature offset applied automatically.
- **Defined user-facing commands:** `AT_START`, `AT_END`, `AT_STATUS`, `AT_THERMAL_STATUS`,
  `AT_DYNZ_STATUS`, `AT_ENABLE`, `AT_DISABLE`, `AT_RESET_STATE`, `AT_INIT_MATERIAL`,
  `AT_SET_PA`, `AT_GET_PA`, `AT_LIST_PA`, `AT_SET_FLOW_K`, `AT_SET_FLOW_GATE`, `AT_SET_MAX`.
- Persists PA values and Speed Guard state between sessions using Klipper's `[save_variables]` to
  `~/printer_data/config/sfs_auto_flow_vars.cfg`.

### `auto_flow_user.cfg.example` (80 lines)

- **User-editable template.** Copied to `auto_flow_user.cfg` (git-ignored) on first install.
- Contains commented-out override variables for every hardware setting in `_AUTO_TEMP_CORE`:
  nozzle type, heater wattage, flow smoothing, PA deadband, thermal safety thresholds, HF
  offsets.
- Users uncomment and edit only what they need. This file is never overwritten by updates.
- Has a known historical bug (now fixed by `update.sh`): an empty `gcode:` line in this file
  could silently replace the entire 700-line core loop with nothing.

### `material_profiles_defaults.cfg` (261 lines) — material library

- **System-managed.** Updated by git; never edited by users.
- Each material is a `[gcode_macro _AF_PROFILE_MATERIALNAME]` with nine variables:
  `flow_k`, `speed_boost_k`, `max_boost`, `max_temp`, `ramp_rise`, `ramp_fall`,
  `flow_gate`, `flow_gate_std`, `default_pa`.
- **Included profiles:** PLA, PETG, ABS, ASA, TPU, NYLON, PC, HIPS, DEFAULT.
- All values are calibrated for a 40 W E3D Revo; the core loop auto-scales them at runtime for
  60 W heaters and HF nozzles.
- Each profile includes comments explaining safe flow limits and the reasoning behind values.

### `material_profiles_user.cfg.example` (66 lines)

- Template for adding custom material profiles (e.g. brand-specific PLA, recycled filaments).
- Shows how to create `[gcode_macro _AF_PROFILE_MYPLA]` with all required variables.
- Explains which variables auto-scale at runtime (flow_k, max_boost, ramp_rise) vs which do not
  (default_pa base value — user must account for HF manually if needed).
- Copied to `material_profiles_user.cfg` (git-ignored) on first install.

---

## Shell Scripts and Services

### `update.sh` (~200 lines)

- The **one-command install and updater**. Intended to be run as `./update.sh`.
- Steps it performs:
  1. `git pull` to fetch the latest code.
  2. Copies `gcode_interceptor.py`, `extruder_monitor.py`, and `analyze_print.py` to
     `~/klipper/klippy/extras/`.
  3. Creates symlinks in `~/printer_data/config/` for `auto_flow_defaults.cfg` and
     `material_profiles_defaults.cfg` so git updates are applied automatically.
  4. Creates `auto_flow_user.cfg` and `material_profiles_user.cfg` from the `.example`
     templates if they don't exist yet (first-time install).
  5. Handles migration from the old single-file layout (`auto_flow.cfg` → backs up and removes).
  6. Removes the old `adaptive-flow-hook.service` moonraker service if it exists.
  7. **Critical bug fix:** detects and removes bare `gcode:` lines from `auto_flow_user.cfg`
     that would silently disable the entire core loop.
  8. Adds missing `[include ...]` and `[gcode_interceptor]` / `[extruder_monitor]` sections to
     `printer.cfg` (with backup).
  9. Installs/updates the `adaptive-flow-dashboard.service` systemd unit, enables it, and
     starts/restarts it.
  10. Restarts Klipper (`sudo systemctl restart klipper`).

### `adaptive_flow_dashboard.service`

- Systemd unit file installed to `/etc/systemd/system/` by `update.sh`.
- Runs as user `pi` (hardcoded — not portable to other usernames).
- Launches: `python3 /home/pi/Klipper-Adaptive-Flow/analyze_print.py --serve --port 7127`
- Set to `Restart=on-failure` with a 10-second delay.
- Makes the web dashboard accessible at `http://<printer-ip>:7127` after every boot.

---

## Documentation Pages (`docs/`)

### `docs/ANALYSIS.md` (457 lines)

- User guide for the web dashboard (`analyze_print.py --serve`).
- Covers: dashboard layout (material selector, summary cards, tabs), real-time monitoring,
  recommendations panel with severity codes, one-click config apply, applied recommendation
  tracking lifecycle (applied → monitoring → verified), material aggregate analysis mode,
  interactive Chart.js charts, CLI usage, and troubleshooting.

### `docs/COMMANDS.md` (474 lines)

- Complete reference for every Klipper G-code command the system adds.
- For each command: description, parameters, example usage, example output, notes on
  persistence and side-effects.
- Includes a quick-reference table and common workflow examples.
- Documents internal commands (`EXCLUDE_OBJECT_START`, `M486`) and Python module commands
  (`EXTRUDER_MONITOR_STATUS`, `EXTRUDER_MONITOR_RESET`).

### `docs/CONFIGURATION.md` (349 lines)

- Detailed configuration reference for `auto_flow_user.cfg` and material profiles.
- Explains all variables with their units, defaults, and interaction effects.
- Sections: quick start (just two settings for most users), temperature control algorithm,
  dynamic PA algorithm, Speed Guard (brief overview with link to SPEED_GUARD.md), material profile parameters,
  hardware auto-scaling tables.

### `docs/SPEED_GUARD.md` (179 lines)

- Dedicated documentation for the Speed Guard system (formerly Dynamic Z-Window / DynZ).
- Explains the problem (complex geometry creates stress), the algorithm (Z-bin scoring,
  detection thresholds, score decay, relief methods), configuration variables, and when to
  enable/disable it.
- Includes worked examples of what Speed Guard does and does not help with.

---

## Example Files

### `PRINT_START.example`

- Generic `PRINT_START` / `PRINT_END` macro template for any Klipper printer.
- Shows the minimum required structure: home → heat → `AT_START MATERIAL={params.MATERIAL}`.
- Comments explain how to configure slicer start G-code and how to disable slicer-side PA.

### `PRINT_START_VORON24.example`

- Voron 2.4-specific template that adds: conditional homing (`_CG28`), Quad Gantry Level,
  rapid adaptive bed mesh, filament sensor control, and a double purge line.
- Demonstrates passing `MATERIAL=` from the slicer automatically via `{filament_type[0]}`.

---

## Planning / Internal Files

### `HARDWARE_AWARE_PLAN.md` (209 lines)

- **Internal planning document** — not user-facing.
- Describes what data `analyze_print.py` reads today vs what it _should_ read from Klipper's
  config files (printer kinematics, stepper specs, input shaper, TMC drivers, fan `max_power`
  cap, probe type, MMU presence).
- Lists 10 specific "big wins" from hardware-aware parsing (e.g. auto-detect Bowden vs direct
  drive, flag the 40% fan cap in btt.cfg, validate slicer accel vs shaper limits).
- Proposes the `collect_printer_hardware()` architecture, which was subsequently implemented in
  `analyze_print.py`.
- **Can likely be deleted** now that the feature it planned is shipped.

### `0` (empty file)

- Zero-byte file with no extension at the repository root.
- No content, no purpose identified.
- **Safe to delete.**

### `.gitattributes`

- Forces Unix line endings (`eol=lf`) for `.sh`, `.py`, `.cfg`, and `.md` files so shell
  scripts stay executable when cloned on Windows and Klipper does not choke on CR+LF.

### `.gitignore`

- Excludes: `__pycache__/`, `*.pyc`, `*.backup` (auto-generated by `update.sh`), and all
  `*_user.cfg` files except the `.example` templates, ensuring user configs are never
  accidentally committed.

### `LICENSE`

- Standard open-source license file (specific license not audited here).

---

## Summary Table

| File | Size | Role | Status |
|------|------|------|--------|
| `auto_flow_defaults.cfg` | 1,386 lines | Core control loop + all user commands | Active — central to the system |
| `analyze_print.py` | 3,157 lines | Dashboard server, recommendations, aggregation, CLI | Active — split into modules (see below) |
| `af_analysis.py` | 1,951 lines | Per-print analysis (banding, thermal, PA, quality) | Active |
| `af_config.py` | 307 lines | Config read/write helpers | Active |
| `af_hardware.py` | 243 lines | Hardware-aware Klipper config parser | Active |
| `af_slicer.py` | 1,440 lines | Slicer G-code parser + slicer diagnostics | Active |
| `extruder_monitor.py` | 1,001 lines | Flow monitoring + CSV logging + lookahead | Active |
| `gcode_interceptor.py` | 95 lines | G-code interception infrastructure | Active but unused — no subscribers wired up |
| `material_profiles_defaults.cfg` | 261 lines | Material tuning library | Active |
| `update.sh` | ~200 lines | Install / update / migrate / restart | Active |
| `adaptive_flow_dashboard.service` | 13 lines | Systemd unit for dashboard | Active — hardcoded `pi` user |
| `auto_flow_user.cfg.example` | 80 lines | User hardware config template | Active |
| `material_profiles_user.cfg.example` | 66 lines | Custom material template | Active |
| `PRINT_START.example` | ~40 lines | Generic slicer macro example | Active |
| `PRINT_START_VORON24.example` | ~60 lines | Voron 2.4 macro example | Active |
| `docs/ANALYSIS.md` | 457 lines | Dashboard user guide | Active |
| `docs/COMMANDS.md` | 474 lines | G-code command reference | Active |
| `docs/CONFIGURATION.md` | 349 lines | Config variable reference | Active |
| `docs/SPEED_GUARD.md` | 179 lines | Speed Guard algorithm guide | Active |
| `README.md` | 239 lines | Install + quick start | Active |
| `HARDWARE_AWARE_PLAN.md` | 209 lines | Internal planning doc | **Stale — feature is shipped** |
| `0` | 0 bytes | Unknown — empty file | **Dead weight — delete it** |
| `.gitattributes` | 4 lines | Line-ending enforcement | Active |
| `.gitignore` | 5 lines | Excludes user configs and caches | Active |
| `LICENSE` | — | Open-source license | Active |

---

## Potential Concerns Worth Noting

- **`analyze_print.py` has been split into modules.** The former 6,978-line monolith is now
  divided: `af_config.py` (config I/O), `af_analysis.py` (per-print analysis),
  `af_hardware.py` (hardware detection), `af_slicer.py` (slicer parsing + diagnostics).
  `analyze_print.py` (3,157 lines) still handles the web server, recommendation engine,
  aggregation, and CLI.
- **`gcode_interceptor.py` has no consumers.** The module is installed and loaded, adding a
  small overhead at boot, but nothing calls `register_gcode_callback`. It is either an unused
  foundation for a future feature or can be removed.
- **`adaptive_flow_dashboard.service` hardcodes the `pi` username.** On any printer not
  running as `pi` (MainsailOS with a different user, a Debian-based install, etc.) the service
  will fail silently.
- **`HARDWARE_AWARE_PLAN.md` is a leftover planning file.** It describes a feature that has
  been implemented and shipped. Leaving it in the repo root creates confusion about project
  status.
- **The empty `0` file** at the repo root has no purpose and should be deleted.
- **E3D Revo hardware lock-in is intentional but total.** The system is useless on any other
  hotend. The README is clear about this, but it is worth reconfirming if the scope is meant to
  widen.
