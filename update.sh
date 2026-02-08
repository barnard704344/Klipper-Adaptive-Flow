#!/bin/bash
# Klipper Adaptive Flow - Smart Updater
# Protects user configuration during updates

set -e

REPO_DIR=~/Klipper-Adaptive-Flow
KLIPPER_EXTRAS=~/klipper/klippy/extras
CONFIG_DIR=~/printer_data/config

echo "[>>] Updating Klipper Adaptive Flow..."
echo ""

cd "$REPO_DIR"

# Pull latest changes
echo "[>>] Fetching updates from GitHub..."
git pull

# Copy Python modules (always safe to update)
echo "[>>] Updating Python modules..."
cp gcode_interceptor.py extruder_monitor.py "$KLIPPER_EXTRAS/"

# Copy optional Python modules if they exist
if [ -f "analyze_print.py" ]; then
    cp analyze_print.py "$KLIPPER_EXTRAS/"
fi

if [ -f "moonraker_hook.py" ]; then
    cp moonraker_hook.py "$KLIPPER_EXTRAS/"
fi

# Copy system defaults (always safe to update)
echo "[>>] Updating system defaults..."
cp auto_flow_defaults.cfg material_profiles_defaults.cfg "$CONFIG_DIR/"

# Copy analysis config if it exists
if [ -f "analysis_config.cfg" ]; then
    cp analysis_config.cfg "$CONFIG_DIR/"
fi

# Check if user config files exist
USER_CFG="$CONFIG_DIR/auto_flow_user.cfg"
USER_MAT="$CONFIG_DIR/material_profiles_user.cfg"

if [ ! -f "$USER_CFG" ]; then
    echo ""
    echo "[!] First-time setup detected!"
    echo "[>>] Creating user configuration template..."
    cp auto_flow_user.cfg.example "$USER_CFG"
    echo "[OK] Created: $USER_CFG"
    echo ""
    echo "[>>] Edit $USER_CFG to customize your settings"
fi

if [ ! -f "$USER_MAT" ]; then
    echo "[>>] Creating user material profiles template..."
    cp material_profiles_user.cfg.example "$USER_MAT"
    echo "[OK] Created: $USER_MAT"
fi

# Check if old config files exist (migration needed)
OLD_AUTO="$CONFIG_DIR/auto_flow.cfg"
OLD_MAT="$CONFIG_DIR/material_profiles.cfg"

if [ -f "$OLD_AUTO" ] && [ ! -L "$OLD_AUTO" ]; then
    echo ""
    echo "[!] MIGRATION NEEDED!"
    echo "Detected old auto_flow.cfg from previous installation"
    echo ""
    echo "Backing up your old config..."
    BACKUP_FILE="$CONFIG_DIR/auto_flow.cfg.backup.$(date +%Y%m%d_%H%M%S)"
    cp "$OLD_AUTO" "$BACKUP_FILE"
    echo "[OK] Backup saved to: $BACKUP_FILE"
    echo ""
    echo "Please review your backup and manually copy any custom"
    echo "settings to $USER_CFG"
    echo ""
    read -p "Press Enter to continue..."
fi

if [ -f "$OLD_MAT" ] && [ ! -L "$OLD_MAT" ]; then
    echo ""
    echo "[!] MIGRATION NEEDED!"
    echo "Detected old material_profiles.cfg from previous installation"
    echo ""
    echo "Backing up your old material profiles..."
    BACKUP_FILE="$CONFIG_DIR/material_profiles.cfg.backup.$(date +%Y%m%d_%H%M%S)"
    cp "$OLD_MAT" "$BACKUP_FILE"
    echo "[OK] Backup saved to: $BACKUP_FILE"
    echo ""
    echo "Please review your backup and manually copy any custom"
    echo "materials to $USER_MAT"
    echo ""
    read -p "Press Enter to continue..."
fi

# Update printer.cfg includes
echo ""
echo "[OK] Update complete!"
echo ""
echo "[>>] Make sure your printer.cfg includes:"
echo "   [include auto_flow_defaults.cfg]"
echo "   [include auto_flow_user.cfg]"
echo "   [include material_profiles_defaults.cfg]"
echo "   [include material_profiles_user.cfg]  # Optional"
echo "   [gcode_interceptor]"
echo "   [extruder_monitor]"
echo ""
echo "[>>] Restarting Klipper..."
sudo systemctl restart klipper

echo ""
echo "[OK] All done! Your custom settings are preserved."
