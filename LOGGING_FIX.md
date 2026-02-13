# CSV Logging Fix - Issue Resolution

## Problem

CSV log file was only 1KB with just header row - no actual data rows. This made analysis impossible.

## Root Cause

**Insufficient file buffering strategy** in `extruder_monitor.py`:

1. Data only flushed every 60 seconds (60 samples)
2. Header written but not immediately flushed to disk
3. Short prints (&lt;60 seconds) lost all data
4. Klipper restarts during print lost buffered data
5. Errors were silent (DEBUG level only)

## Solution

### Code Changes in `extruder_monitor.py`

#### 1. Immediate Header Flush (line 419-421)
```python
self._log_writer.writerow([...header...])
# NEW: Flush header immediately to disk
self._log_file.flush()
os.fsync(self._log_file.fileno())
```
**Impact**: Prevents empty 1KB files with only header in buffer.

#### 2. Frequent Data Flush (line 666-668)
```python
# BEFORE: Flush every 60 samples (~60 seconds)
if self._log_sample_count % 60 == 0:
    self._log_file.flush()

# AFTER: Flush every 10 samples (~10 seconds)
if self._log_sample_count % 10 == 0:
    self._log_file.flush()
    os.fsync(self._log_file.fileno())
```
**Impact**: 
- Short prints now capture data
- Data loss window reduced from 60s to 10s
- Klipper crashes lose max 10s of data instead of 60s

#### 3. Final Flush Before Close (line 833-835)
```python
# NEW: Ensure all data written before close
self._log_file.flush()
os.fsync(self._log_file.fileno())
self._log_file.close()
```
**Impact**: Guarantees complete data on normal print end.

#### 4. Error Visibility (line 671-673)
```python
# BEFORE: Silent failures
logging.getLogger('ExtruderMonitor').debug(f"Log data error: {e}")

# AFTER: User-visible errors
logger.error(f"Log data error: {e}")
gcmd.respond_info(f"AT_LOG: Error writing data: {e}")
```
**Impact**: Users immediately see if logging fails.

#### 5. State Cleanup (line 491-493)
```python
except Exception as e:
    gcmd.respond_info(f"AT_LOG: Failed to start logging: {e}")
    logger.error(f"Failed to start logging: {e}")
    # NEW: Clean up partial state
    self._log_file = None
    self._log_writer = None
```
**Impact**: Failed logging doesn't leave partial state.

#### 6. Missing Start Warning (line 499-502)
```python
if not self._log_writer:
    # NEW: Warn user if AT_START wasn't called
    if not hasattr(self, '_log_warning_shown'):
        self._log_warning_shown = True
        gcmd.respond_info("AT_LOG: Logging not active. Call AT_LOG_START first.")
    return
```
**Impact**: Clear feedback if logging not initialized.

## Update Instructions

```bash
cd ~/Klipper-Adaptive-Flow
git pull
sudo systemctl restart klipper
```

## Testing

After update, run a test print:

1. **Check file is created**:
   ```bash
   ls -lh ~/printer_data/logs/adaptive_flow/
   ```
   File size should grow during print.

2. **Verify data rows**:
   ```bash
   wc -l ~/printer_data/logs/adaptive_flow/*.csv
   ```
   Should show many rows, not just 1 (header).

3. **Check console**:
   - Should see: `AT_LOG: Started logging to <path>`
   - Should see data being written
   - Should see any errors immediately

## Expected Results

- ✅ CSV files grow in size during print
- ✅ CSV files contain data rows with temperature, flow, PA, etc.
- ✅ Short prints (&lt;10 seconds) still capture data
- ✅ Data preserved even if Klipper restarted mid-print
- ✅ Errors visible in console immediately

## What This Doesn't Fix

This fix addresses **logging only**. It does NOT:
- Fix mechanical issues (belt tension, frame rigidity, etc.)
- Calibrate pressure advance
- Tune temperature settings
- Diagnose printer hardware problems

The logging fix allows you to collect data for analysis. Actual print quality issues require separate investigation.

## File Changes Summary

**Modified:**
- `extruder_monitor.py` - 6 critical bug fixes for logging

**Removed:**
- `docs/TROUBLESHOOTING_ZBANDING.md` - Incorrect leadscrew focus (Voron 2.4 uses belts)
- `ISSUE_ANALYSIS.md` - Generic mechanical troubleshooting (not applicable)
- `diagnose_zbanding.py` - Mechanical diagnostic tool (not relevant)

## Next Steps

1. Update code and restart Klipper
2. Run test print
3. Verify CSV file contains data
4. Use the CSV data with `analyze_print.py` for insights
5. Report back if logging still fails

---

**TL;DR**: CSV logging now flushes every 10 seconds instead of 60, with immediate header write and visible error messages. This fixes the "empty log file" issue.
