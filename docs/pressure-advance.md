# Pressure Advance Configuration

The system automatically manages Pressure Advance (PA) values. No manual configuration required.

---

## Default PA Values

These values are applied automatically if you haven't calibrated your own:

| Material | Default PA |
|----------|------------|
| PLA | 0.040 |
| PETG | 0.060 |
| ABS | 0.050 |
| ASA | 0.050 |
| TPU | 0.200 |
| NYLON | 0.055 |
| PC | 0.045 |
| HIPS | 0.045 |

---

## How PA Is Selected

When `AT_START` runs, the system chooses PA in this order:

1. **Saved calibrated value** — If you've run `AT_SET_PA MATERIAL=PLA PA=0.045`, that value is used
2. **Learned value** — If PA auto-learning has saved an adjusted value from previous prints
3. **Default** — Falls back to the table above

---

## Saving Your Calibrated PA

After running a PA calibration test (tower or line method), save your value:

```gcode
AT_SET_PA MATERIAL=PLA PA=0.045
AT_SET_PA MATERIAL=PETG PA=0.055
```

View saved values:
```gcode
AT_LIST_PA
```

---

## PA Auto-Learning (Experimental)

When enabled, the system learns optimal PA during printing:

### How It Works

1. **Corner Detection** — Monitors sharp corners (>45°) in your print
2. **Thermal Analysis** — Measures temperature deviation after corners
3. **Adjustment** — Makes tiny PA changes based on thermal feedback

| Thermal Response | Meaning | Action |
|------------------|---------|--------|
| Too hot after corner | Over-extrusion (bulging) | Increase PA |
| Too cold after corner | Under-extrusion (gaps) | Decrease PA |

### Settings

In `auto_flow.cfg`:
```ini
variable_pa_auto_learning: True    # Enable/disable
variable_pa_learning_rate: 0.002   # Adjustment per 30 corners
```

### Learning Rate

- Adjustments are very small (±0.002 per evaluation)
- Evaluates every 30 corners
- Takes many prints to converge on optimal value
- Prevents oscillation while slowly improving

### Persistence

At print end (`AT_END`):
- Reports learning statistics
- Saves learned PA per material
- Value is automatically loaded next print

---

## Dynamic PA Adjustment

During printing, PA is also adjusted based on temperature boost:

**Why?** Hotter plastic is more fluid (lower viscosity) and requires less PA.

The system slightly reduces PA when boosting temperature, preventing:
- Gaps at corners during high-speed sections
- Over-retraction when running hot

This is independent of PA auto-learning and happens in real-time.

---

## Disabling PA Management

To use your slicer's PA instead:

1. Set a fixed PA in your slicer
2. Disable auto-learning:
   ```ini
   variable_pa_auto_learning: False
   ```
3. Don't call `AT_SET_PA` — the system won't override slicer values if no saved value exists

---

## Commands Reference

| Command | Description |
|---------|-------------|
| `AT_SET_PA MATERIAL=PLA PA=0.045` | Save calibrated PA |
| `AT_GET_PA MATERIAL=PLA` | Show PA for material |
| `AT_LIST_PA` | List all saved PA values |
