# Copilot Instructions for Klipper Adaptive Flow

## Repository Overview

This repository provides **automatic temperature and pressure advance control** for E3D Revo hotends on Klipper 3D printers. The system dynamically adjusts nozzle temperature and Pressure Advance (PA) in real-time based on flow rate, speed, and acceleration—eliminating the need for manual tuning.

### Core Features
- **Dynamic Temperature Control**: Flow/speed/acceleration-based temperature boost
- **Dynamic Pressure Advance**: Automatically scales PA with temperature changes
- **Smart Cooling**: Adjusts part cooling fan based on flow rate and layer time
- **5-Second Lookahead**: Pre-heats nozzle before flow spikes
- **Dynamic Z-Window (DynZ)**: Learns and adapts to challenging geometries (domes, spheres)
- **Multi-Object Temperature Management**: Safe temperature transitions between sequential objects
- **Material Profiles**: Per-material tuning for PLA, PETG, ABS, ASA, TPU, Nylon, PC, HIPS

## File Structure

### Python Modules (Klipper Extras)
- `gcode_interceptor.py` - Intercepts G-code commands and broadcasts events to subscribers
- `extruder_monitor.py` - Monitors extruder load, manages lookahead buffer, and logs print data
- `analyze_print.py` - Post-print AI analysis tool (uses LLMs for tuning suggestions)
- `moonraker_hook.py` - Optional auto-analysis integration with Moonraker

### Configuration Files (Klipper/G-code Macros)
- `auto_flow.cfg` - Main control logic, core loop, macros (user-editable settings)
- `material_profiles.cfg` - Material-specific boost curves and PA settings (user-editable)
- `analysis_config.cfg` - LLM provider configuration for print analysis

### Documentation
- `README.md` - Installation, setup, and usage guide
- `docs/CONFIGURATION.md` - Detailed configuration reference
- `docs/DYNZ.md` - Dynamic Z-Window documentation
- `docs/SMART_COOLING.md` - Smart Cooling feature documentation
- `docs/ANALYSIS.md` - Print analysis setup guide

### Examples
- `PRINT_START.example` - Example slicer start G-code
- `PRINT_START_VORON24.example` - Voron 2.4-specific example

## Code Guidelines

### Python Code (Klipper Modules)
1. **Follow Klipper Conventions**:
   - Use `logging` module for all logging (no `print()` statements)
   - Register event handlers with `printer.register_event_handler()`
   - Look up other modules with `printer.lookup_object()`
   - All modules must be Klipper extras (placed in `klippy/extras/`)

2. **Thread Safety**:
   - Use `threading.Lock()` for shared state (see `extruder_monitor.py` examples)
   - The lookahead buffer and logging are multi-threaded operations

3. **Error Handling**:
   - Gracefully handle missing objects/modules
   - Provide clear error messages for configuration issues
   - Use `raise config.error()` for config validation errors

4. **Code Style**:
   - Follow PEP 8 conventions
   - Use descriptive variable names (e.g., `flow_smoothing`, not `fs`)
   - Add docstrings to classes and complex functions
   - Keep functions focused and modular

5. **Performance**:
   - Pre-compile regex patterns (see `_param_re` in `extruder_monitor.py`)
   - Use efficient data structures (e.g., `deque` for fixed-size buffers)
   - Minimize G-code processing overhead

### Configuration Files (.cfg)
1. **Structure**:
   - Use Klipper macro syntax: `[gcode_macro MACRO_NAME]`
   - Define variables with `variable_name: value`
   - Use meaningful section comments with separator lines

2. **User-Editable Settings**:
   - Place all user-configurable settings at the top with clear comments
   - Provide default values that work for most users
   - Include units in comments (°C, mm/s, mm³/s, etc.)

3. **Material Profiles**:
   - Each material is a separate macro: `[gcode_macro _AF_PROFILE_MATERIAL]`
   - Follow naming convention: `_AF_PROFILE_` prefix for internal profiles
   - Include parameter guide comments in `material_profiles.cfg`

4. **Macro Naming**:
   - User-facing commands: `AT_COMMAND` (e.g., `AT_STATUS`, `AT_SET_PA`)
   - Internal macros: `_AUTO_TEMP_*` or `_AF_*` prefix

### Documentation
1. **Keep README.md Concise**:
   - Focus on installation, quick start, and common use cases
   - Link to detailed docs in `docs/` for advanced topics
   - Include code examples for slicer setup

2. **Configuration Documentation**:
   - Provide tables with parameter descriptions and default values
   - Include usage examples and edge cases
   - Explain the "why" behind parameters, not just the "what"

3. **In-Code Comments**:
   - Explain complex algorithms and non-obvious logic
   - Document thread safety considerations
   - Note Klipper-specific behaviors or limitations

## Key Concepts

### Flow Rate Calculation
- **Volumetric Flow** = extrusion rate (mm³/s)
- Calculated from E-axis delta, move duration, and filament diameter
- Primary input for temperature boost decisions

### Temperature Boost Algorithm
```
boost = (flow × flow_k) + (speed × speed_boost_k) + accel_factor
target_temp = base_temp + min(boost, max_boost)
```
- `flow_k`: temperature increase per mm³/s of flow
- `speed_boost_k`: additional boost for high-speed moves (>100mm/s)
- Ramped up/down at controlled rates to avoid thermal shock

### Dynamic Pressure Advance
```
PA_adjusted = PA_base - (temp_boost × pa_boost_k)
```
- Higher temperature → lower filament viscosity → less PA needed
- Auto-adjusts in real-time as temperature changes

### Lookahead System
- 5-second prediction buffer fed by `extruder_monitor.py`
- G-code interceptor parses upcoming moves
- Enables pre-heating before flow demand spikes

### Dynamic Z-Window (DynZ)
- Divides Z-height into bins (typically 0.5mm each)
- Learns stress patterns (high speed + low flow + heater demand)
- Reduces acceleration in stress zones on subsequent layers
- "Memory" persists across print to handle recurring geometries

## Testing Approach

### No Automated Test Suite
This project does not include automated unit tests. Testing is performed on actual 3D printers with Klipper firmware.

### Manual Testing Protocol
When making code changes:

1. **Syntax Validation**:
   - Python: Check with `python3 -m py_compile <file.py>`
   - Config: Validate Klipper macro syntax manually

2. **Integration Testing**:
   - Copy modules to `~/klipper/klippy/extras/`
   - Restart Klipper: `sudo systemctl restart klipper`
   - Check `/tmp/klippy.log` for errors

3. **Functional Testing**:
   - Run `AT_STATUS` command to verify module loading
   - Start a test print with adaptive flow enabled
   - Monitor behavior with `AT_STATUS` during print
   - Check log files in `~/printer_data/logs/adaptive_flow/`

4. **Edge Cases to Test**:
   - Material changes mid-print
   - Sequential multi-object prints
   - Very low flow (<5mm³/s) and very high flow (>20mm³/s)
   - Rapid acceleration changes
   - Layer transitions

### Print Analysis Testing
- Run `analyze_print.py` on logged print sessions
- Verify LLM provider connections (GitHub Models, OpenAI, Anthropic)
- Check that suggestions are actionable and safe

## Common Workflows

### Adding a New Material Profile
1. Copy an existing profile in `material_profiles.cfg`
2. Rename macro to `[gcode_macro _AF_PROFILE_YOURMATERIAL]`
3. Adjust boost parameters based on material properties
4. Document in `docs/CONFIGURATION.md` if it's a common material

### Modifying Temperature Control Logic
1. Core loop is in `auto_flow.cfg` under `[gcode_macro _AUTO_TEMP_CORE]`
2. Test with conservative parameters first (lower `flow_k`, slower ramp rates)
3. Verify heater can keep up (check for "heater too slow" warnings in logs)
4. Consider impact on all material profiles

### Extending Lookahead System
1. Add functionality to `extruder_monitor.py`
2. Expose new G-code commands via `register_command()`
3. Update `auto_flow.cfg` to consume new data
4. Document new commands in README.md

### Adding New Analysis Features
1. Extend `analyze_print.py` with new metrics
2. Update LLM prompt to interpret new data
3. Test with multiple LLM providers (GitHub, OpenAI, Anthropic)
4. Add safety checks for auto-applied suggestions

## Important Constraints

### Hardware Requirements
- **E3D Revo hotend only** (HF or Standard)
- 40W or 60W heater cartridge
- Klipper firmware required

### Safety Considerations
- Never exceed material's absolute max temperature (`max_temp`)
- Respect heater power limits (tracked by `extruder_monitor.py`)
- Ensure temperature ramp rates are achievable by the heater
- Always call `AT_END` before `TURN_OFF_HEATERS` in macros

### Klipper Limitations
- Cannot modify PA during a move (Klipper restriction)
- G-code interception adds minimal overhead but must be efficient
- Lookahead buffer is separate from Klipper's internal lookahead

## Dependencies

### Python Modules (Standard Library)
- `logging`, `threading`, `time`, `json`, `os`, `csv`
- `collections.deque` - efficient fixed-size buffers
- `datetime` - timestamp generation
- `re` - regex for G-code parsing
- `urllib.parse` - URL encoding for API requests

### External APIs (Optional)
- GitHub Models API - LLM analysis (free, requires `GITHUB_TOKEN`)
- OpenAI API - LLM analysis (requires `OPENAI_API_KEY`)
- Anthropic API - LLM analysis (requires `ANTHROPIC_API_KEY`)

### Klipper Integration
- Modules are loaded as Klipper extras
- Must be compatible with Klipper's reactor-based architecture
- See Klipper documentation for extras development guidelines

## Common Pitfalls

1. **Don't assume printer state**: Always check if modules/objects exist before accessing
2. **Thread safety matters**: Lookahead buffer is accessed from multiple threads
3. **Config files are user-editable**: Don't break existing user configurations
4. **Temperature changes are slow**: Consider thermal inertia in control logic
5. **Klipper logs are critical**: Check `/tmp/klippy.log` for errors during testing
6. **PA can't change mid-move**: Queue PA changes for the next move
7. **Flow estimation isn't perfect**: Use smoothing to avoid oscillation

## Getting Help

- Check Klipper logs: `/tmp/klippy.log`
- Review print session logs: `~/printer_data/logs/adaptive_flow/`
- Enable verbose logging in modules by setting log level to `DEBUG`
- Test incrementally: small changes, frequent validation

## AI Analysis Integration

The `analyze_print.py` script uses LLMs to provide tuning suggestions:

### LLM Provider Selection
- **GitHub Models** (recommended): Free, no API key cost, good quality
- **OpenAI**: High quality, requires paid API key
- **Anthropic**: High quality, requires paid API key

### Analysis Output
- **[✓ SAFE]** suggestions: Can be auto-applied (use `--auto` flag)
- **[⚠ MANUAL]** suggestions: Require user review and manual adjustment

### Safety Guardrails
- Only specific config changes are auto-applied
- Suggestions are validated before application
- User always has final control

## Version Control

- Use meaningful commit messages
- Don't commit user-specific configs (e.g., API keys, printer settings)
- Keep documentation in sync with code changes
- Tag releases for stable versions

---

**Remember**: This system runs in real-time during 3D prints. Safety, reliability, and performance are critical. Test thoroughly on actual hardware before committing changes.
