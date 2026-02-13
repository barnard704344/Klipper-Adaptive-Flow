#!/usr/bin/env python3
"""
Z-Banding and Layer Inconsistency Diagnostic Tool

Analyzes klippy.log and Adaptive Flow CSV logs to identify potential causes
of layer inconsistencies and Z-banding artifacts.

Usage:
    python3 diagnose_zbanding.py                    # Analyze most recent print
    python3 diagnose_zbanding.py --klippy <path>    # Specify klippy.log
    python3 diagnose_zbanding.py --csv <path>       # Specify CSV log
    python3 diagnose_zbanding.py --all              # Analyze all available logs
"""

import os
import sys
import re
import csv
import glob
import statistics
from pathlib import Path
from collections import defaultdict

# =============================================================================
# DIAGNOSTIC THRESHOLDS
# =============================================================================

TEMP_VARIANCE_WARNING = 1.0  # °C - temperature should stay within this range
TEMP_VARIANCE_CRITICAL = 2.0  # °C
PWM_HIGH_THRESHOLD = 0.85  # 85% PWM average indicates saturation
PWM_CRITICAL_THRESHOLD = 0.95  # 95%
TEMP_LAG_WARNING = 3.0  # °C - target minus actual
TEMP_LAG_CRITICAL = 5.0  # °C
DYNZ_EXCESSIVE_THRESHOLD = 20.0  # % of print time
FAN_OSCILLATION_THRESHOLD = 15.0  # % change per second

# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def find_klippy_log():
    """Find the most recent klippy.log file."""
    paths = [
        '/tmp/klippy.log',
        os.path.expanduser('~/printer_data/logs/klippy.log'),
        os.path.expanduser('~/klipper_logs/klippy.log'),
    ]
    
    for path in paths:
        if os.path.exists(path):
            return path
    return None

def find_csv_logs(log_dir=None):
    """Find all CSV logs from Adaptive Flow."""
    if log_dir is None:
        log_dir = os.path.expanduser('~/printer_data/logs/adaptive_flow')
    
    if not os.path.exists(log_dir):
        return []
    
    csv_files = glob.glob(os.path.join(log_dir, '*.csv'))
    csv_files.sort(key=os.path.getmtime, reverse=True)
    return csv_files

def parse_stats_line(line):
    """Extract temperature and PWM data from Stats line."""
    match = re.search(r'extruder: target=(\d+\.?\d*) temp=(\d+\.?\d*) pwm=(\d+\.?\d*)', line)
    if match:
        return {
            'target': float(match.group(1)),
            'actual': float(match.group(2)),
            'pwm': float(match.group(3))
        }
    return None

# =============================================================================
# KLIPPY LOG ANALYSIS
# =============================================================================

def analyze_klippy_log(log_path, sample_limit=1000):
    """Analyze klippy.log for temperature and heater performance."""
    
    print(f"\n{'='*60}")
    print(f"KLIPPY LOG ANALYSIS: {log_path}")
    print(f"{'='*60}\n")
    
    if not os.path.exists(log_path):
        print(f"❌ ERROR: Klippy log not found at {log_path}")
        return None
    
    # Extract temperature data from Stats lines
    stats_data = []
    
    with open(log_path, 'r', errors='ignore') as f:
        # Read last N lines for recent print
        lines = f.readlines()
        stats_lines = [l for l in lines if l.startswith('Stats ')]
        
        # Take the most recent sample_limit stats
        recent_stats = stats_lines[-sample_limit:] if len(stats_lines) > sample_limit else stats_lines
        
        for line in recent_stats:
            data = parse_stats_line(line)
            if data and data['target'] > 150:  # Only analyze printing temps (>150C = likely extruder)
                stats_data.append(data)
    
    if not stats_data:
        print("⚠️  No temperature data found in klippy.log")
        print("    - Log may be from before the print started")
        print("    - Or Stats lines are not being generated")
        return None
    
    # Calculate statistics
    targets = [d['target'] for d in stats_data]
    actuals = [d['actual'] for d in stats_data]
    pwms = [d['pwm'] for d in stats_data]
    lags = [t - a for t, a in zip(targets, actuals)]
    
    target_temp = statistics.mean(targets) if targets else 0
    actual_mean = statistics.mean(actuals)
    actual_stdev = statistics.stdev(actuals) if len(actuals) > 1 else 0
    actual_min = min(actuals)
    actual_max = max(actuals)
    pwm_mean = statistics.mean(pwms)
    pwm_max = max(pwms)
    lag_mean = statistics.mean(lags)
    lag_max = max(lags)
    
    # Print results
    print("📊 TEMPERATURE ANALYSIS")
    print(f"   Target: {target_temp:.1f}°C")
    print(f"   Actual: {actual_mean:.1f}°C (±{actual_stdev:.2f}°C)")
    print(f"   Range:  {actual_min:.1f}°C - {actual_max:.1f}°C")
    
    # Temperature stability assessment
    temp_range = actual_max - actual_min
    if temp_range <= TEMP_VARIANCE_WARNING:
        print(f"   ✓ EXCELLENT: Temperature very stable (range {temp_range:.1f}°C)")
    elif temp_range <= TEMP_VARIANCE_CRITICAL:
        print(f"   ⚠️  MODERATE: Some temperature variation (range {temp_range:.1f}°C)")
        print(f"      → Consider PID re-tuning: PID_CALIBRATE HEATER=extruder TARGET={int(target_temp)}")
    else:
        print(f"   ❌ CRITICAL: Excessive temperature variation (range {temp_range:.1f}°C)")
        print(f"      → PID tuning required: PID_CALIBRATE HEATER=extruder TARGET={int(target_temp)}")
    
    print(f"\n⚡ HEATER PERFORMANCE")
    print(f"   PWM (avg): {pwm_mean:.1%}")
    print(f"   PWM (max): {pwm_max:.1%}")
    print(f"   Thermal Lag (avg): {lag_mean:.1f}°C")
    print(f"   Thermal Lag (max): {lag_max:.1f}°C")
    
    # Heater saturation assessment
    if pwm_mean < PWM_HIGH_THRESHOLD:
        print(f"   ✓ GOOD: Heater has headroom (avg PWM {pwm_mean:.1%})")
    elif pwm_mean < PWM_CRITICAL_THRESHOLD:
        print(f"   ⚠️  WARNING: Heater working hard (avg PWM {pwm_mean:.1%})")
        print(f"      → Consider reducing flow_k or print speed")
        print(f"      → Check heater cartridge (40W or 60W recommended)")
    else:
        print(f"   ❌ CRITICAL: Heater saturated (avg PWM {pwm_mean:.1%})")
        print(f"      → Reduce print speed or flow rate immediately")
        print(f"      → Verify heater hardware is functioning")
    
    # Thermal lag assessment
    if lag_mean < TEMP_LAG_WARNING:
        print(f"   ✓ GOOD: Thermal response is quick (avg lag {lag_mean:.1f}°C)")
    elif lag_mean < TEMP_LAG_CRITICAL:
        print(f"   ⚠️  WARNING: Thermal lag present (avg lag {lag_mean:.1f}°C)")
        print(f"      → Heater struggling to keep up with demand")
        print(f"      → Consider increasing ramp_rate_rise in config")
    else:
        print(f"   ❌ CRITICAL: Severe thermal lag (avg lag {lag_mean:.1f}°C)")
        print(f"      → Heater cannot keep up with thermal demand")
        print(f"      → Reduce flow_k, increase max_boost, or reduce speed")
    
    # Check for Adaptive Flow presence
    has_adaptive_flow = False
    with open(log_path, 'r', errors='ignore') as f:
        for line in f:
            if 'extruder_monitor' in line or 'gcode_interceptor' in line:
                has_adaptive_flow = True
                break
    
    print(f"\n🔧 ADAPTIVE FLOW STATUS")
    if has_adaptive_flow:
        print(f"   ✓ Adaptive Flow modules detected in config")
    else:
        print(f"   ❌ Adaptive Flow modules NOT detected")
        print(f"      → Layer issues likely not related to Adaptive Flow")
        print(f"      → Check mechanical components (Z-axis, belts, frame)")
    
    return {
        'target_temp': target_temp,
        'actual_mean': actual_mean,
        'actual_stdev': actual_stdev,
        'temp_range': temp_range,
        'pwm_mean': pwm_mean,
        'pwm_max': pwm_max,
        'lag_mean': lag_mean,
        'lag_max': lag_max,
        'has_adaptive_flow': has_adaptive_flow,
    }

# =============================================================================
# CSV LOG ANALYSIS
# =============================================================================

def analyze_csv_log(csv_path):
    """Analyze Adaptive Flow CSV log for patterns."""
    
    print(f"\n{'='*60}")
    print(f"CSV LOG ANALYSIS: {os.path.basename(csv_path)}")
    print(f"{'='*60}\n")
    
    if not os.path.exists(csv_path):
        print(f"❌ ERROR: CSV log not found at {csv_path}")
        return None
    
    # Read CSV data
    data = []
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            data.append(row)
    
    if len(data) == 0:
        print("⚠️  CSV log is empty (only header present)")
        print("   Possible causes:")
        print("   1. Logging not started with AT_START macro")
        print("   2. extruder_monitor not properly loaded")
        print("   3. Print ended before logging began")
        print("\n   To fix: Ensure PRINT_START macro calls AT_START MATERIAL=<material>")
        return None
    
    print(f"📊 PRINT SESSION SUMMARY")
    print(f"   Data points: {len(data)}")
    
    # Extract key metrics
    try:
        flows = [float(row['flow']) for row in data if row['flow']]
        boosts = [float(row['boost']) for row in data if row['boost']]
        pwms = [float(row['pwm']) for row in data if row['pwm']]
        fans = [float(row['fan_pct']) for row in data if row['fan_pct']]
        dynz_active = [int(row['dynz_active']) for row in data if row['dynz_active']]
        
        print(f"\n   Flow (mm³/s):")
        print(f"      Avg: {statistics.mean(flows):.1f}, Max: {max(flows):.1f}")
        
        print(f"\n   Temperature Boost (°C):")
        print(f"      Avg: {statistics.mean(boosts):.1f}, Max: {max(boosts):.1f}")
        
        print(f"\n   Heater PWM:")
        print(f"      Avg: {statistics.mean(pwms):.1%}, Max: {max(pwms):.1%}")
        
        print(f"\n   Cooling Fan:")
        print(f"      Avg: {statistics.mean(fans):.0f}%, Range: {min(fans):.0f}-{max(fans):.0f}%")
        
        # Check for fan oscillation
        fan_changes = [abs(fans[i] - fans[i-1]) for i in range(1, len(fans))]
        avg_fan_change = statistics.mean(fan_changes) if fan_changes else 0
        
        if avg_fan_change > FAN_OSCILLATION_THRESHOLD:
            print(f"      ⚠️  WARNING: Fan oscillating significantly (avg change {avg_fan_change:.1f}%)")
            print(f"         → May cause thermal cycling and layer inconsistencies")
            print(f"         → Consider disabling Smart Cooling or tuning sc_flow_gate")
        else:
            print(f"      ✓ Fan behavior looks normal")
        
        # DynZ analysis
        dynz_pct = (sum(dynz_active) / len(dynz_active)) * 100
        print(f"\n   DynZ Activation:")
        print(f"      Active: {dynz_pct:.1f}% of print time")
        
        if dynz_pct > DYNZ_EXCESSIVE_THRESHOLD:
            print(f"      ⚠️  WARNING: DynZ active more than expected")
            print(f"         → May indicate heater struggling or geometry with many transitions")
            print(f"         → Consider increasing dynz_activate_score threshold")
        elif dynz_pct > 0:
            print(f"      ✓ Normal DynZ activation for challenging geometry")
        else:
            print(f"      ✓ No DynZ activation (no stress detected)")
    
    except (KeyError, ValueError) as e:
        print(f"   ❌ Error parsing CSV data: {e}")
        print(f"      CSV format may be incompatible")
        return None
    
    return {
        'data_points': len(data),
        'avg_flow': statistics.mean(flows),
        'max_flow': max(flows),
        'avg_boost': statistics.mean(boosts),
        'avg_pwm': statistics.mean(pwms),
        'dynz_pct': dynz_pct,
    }

# =============================================================================
# RECOMMENDATIONS ENGINE
# =============================================================================

def generate_recommendations(klippy_analysis, csv_analysis):
    """Generate specific recommendations based on analysis."""
    
    print(f"\n{'='*60}")
    print("RECOMMENDATIONS")
    print(f"{'='*60}\n")
    
    recommendations = []
    
    # If no Adaptive Flow data
    if klippy_analysis and not klippy_analysis['has_adaptive_flow']:
        print("🔍 DIAGNOSIS: Issue is NOT related to Adaptive Flow\n")
        print("Most likely causes (in order of probability):\n")
        print("1. **Z-AXIS MECHANICAL ISSUES** (60% of cases)")
        print("   → Lead screw binding or wobble")
        print("   → Check: Manually move Z-axis - should be smooth")
        print("   → Action: Clean and lubricate lead screw")
        print("   → Action: Verify Z-axis alignment and couplings\n")
        
        print("2. **PRESSURE ADVANCE TUNING** (20% of cases)")
        print("   → PA not calibrated for this filament")
        print("   → Action: Run PA calibration test")
        print("   → Action: AT_SET_PA MATERIAL=<material> PA=<value>\n")
        
        print("3. **BELT TENSION** (10% of cases)")
        print("   → Loose or over-tightened belts")
        print("   → Action: Check belt tension (should feel like guitar string)")
        print("   → Action: Use belt tension meter if available\n")
        
        print("4. **FRAME STABILITY** (10% of cases)")
        print("   → Loose frame bolts or flex")
        print("   → Action: Tighten all frame bolts")
        print("   → Action: Check for frame squareness\n")
        
        return recommendations
    
    # Temperature issues
    if klippy_analysis:
        if klippy_analysis['temp_range'] > TEMP_VARIANCE_CRITICAL:
            recommendations.append({
                'priority': 'HIGH',
                'issue': 'Temperature Instability',
                'action': f"PID_CALIBRATE HEATER=extruder TARGET={int(klippy_analysis['target_temp'])}",
                'reason': f"Temperature varying ±{klippy_analysis['temp_range']:.1f}°C (should be <1°C)"
            })
        
        if klippy_analysis['pwm_mean'] > PWM_HIGH_THRESHOLD:
            recommendations.append({
                'priority': 'HIGH' if klippy_analysis['pwm_mean'] > PWM_CRITICAL_THRESHOLD else 'MEDIUM',
                'issue': 'Heater Saturation',
                'action': 'Reduce flow_k or print speed',
                'reason': f"Heater PWM averaging {klippy_analysis['pwm_mean']:.1%} (struggling to keep up)"
            })
        
        if klippy_analysis['lag_mean'] > TEMP_LAG_WARNING:
            recommendations.append({
                'priority': 'MEDIUM',
                'issue': 'Thermal Lag',
                'action': 'Increase ramp_rate_rise or reduce flow demands',
                'reason': f"Temperature lagging {klippy_analysis['lag_mean']:.1f}°C behind target"
            })
    
    # CSV-based recommendations
    if csv_analysis:
        if csv_analysis['dynz_pct'] > DYNZ_EXCESSIVE_THRESHOLD:
            recommendations.append({
                'priority': 'MEDIUM',
                'issue': 'Excessive DynZ Activation',
                'action': 'Increase dynz_activate_score or reduce print speeds',
                'reason': f"DynZ active {csv_analysis['dynz_pct']:.1f}% of print (detecting stress)"
            })
    
    # Print recommendations
    if recommendations:
        for i, rec in enumerate(recommendations, 1):
            print(f"{i}. [{rec['priority']}] {rec['issue']}")
            print(f"   Reason: {rec['reason']}")
            print(f"   Action: {rec['action']}\n")
    else:
        print("✓ No critical issues detected in Adaptive Flow system")
        print("\nIf you're still experiencing layer inconsistencies, check:")
        print("  • Z-axis mechanical components (lead screw, linear rails)")
        print("  • Belt tension and condition")
        print("  • Frame rigidity and squareness")
        print("  • Pressure advance calibration for this filament\n")
    
    return recommendations

# =============================================================================
# MAIN
# =============================================================================

def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Diagnose Z-banding and layer inconsistencies',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python3 diagnose_zbanding.py                    # Auto-detect logs
    python3 diagnose_zbanding.py --klippy /tmp/klippy.log
    python3 diagnose_zbanding.py --csv ~/logs/print.csv
        """
    )
    
    parser.add_argument('--klippy', help='Path to klippy.log', default=None)
    parser.add_argument('--csv', help='Path to CSV log', default=None)
    parser.add_argument('--all', action='store_true', help='Analyze all available logs')
    parser.add_argument('--samples', type=int, default=1000, help='Number of Stats lines to analyze')
    
    args = parser.parse_args()
    
    print("\n" + "="*60)
    print("Z-BANDING DIAGNOSTIC TOOL")
    print("Klipper Adaptive Flow")
    print("="*60)
    
    # Find logs
    klippy_path = args.klippy or find_klippy_log()
    csv_files = find_csv_logs()
    
    if args.csv:
        csv_path = args.csv
    elif csv_files:
        csv_path = csv_files[0]  # Most recent
    else:
        csv_path = None
    
    # Analyze klippy log
    klippy_analysis = None
    if klippy_path:
        klippy_analysis = analyze_klippy_log(klippy_path, sample_limit=args.samples)
    else:
        print("\n⚠️  Klippy log not found")
        print("    Searched: /tmp/klippy.log, ~/printer_data/logs/klippy.log")
    
    # Analyze CSV log
    csv_analysis = None
    if csv_path:
        csv_analysis = analyze_csv_log(csv_path)
    else:
        print("\n⚠️  No CSV logs found from Adaptive Flow")
        print("    Searched: ~/printer_data/logs/adaptive_flow/")
        print("    To enable: Add AT_START MATERIAL=<material> to PRINT_START macro")
    
    # Generate recommendations
    if klippy_analysis or csv_analysis:
        generate_recommendations(klippy_analysis, csv_analysis)
    else:
        print("\n❌ No log data available for analysis")
        print("   Please provide klippy.log or CSV log path")
    
    print("\n" + "="*60)
    print("For more help, see: docs/TROUBLESHOOTING_ZBANDING.md")
    print("="*60 + "\n")

if __name__ == '__main__':
    main()
