#!/bin/bash
# Klipper Adaptive Flow - Smart Updater
# Protects user configuration during updates

set -e

REPO_DIR=~/Klipper-Adaptive-Flow
KLIPPER_EXTRAS=~/klipper/klippy/extras
CONFIG_DIR=~/printer_data/config
PRINTER_CFG="$CONFIG_DIR/printer.cfg"

# Function to check if a line exists in printer.cfg
line_exists_in_printer_cfg() {
    local pattern="$1"
    if [ -f "$PRINTER_CFG" ]; then
        grep -qF "$pattern" "$PRINTER_CFG"
    else
        return 1
    fi
}

# Function to add configuration to printer.cfg
update_printer_cfg() {
    local REQUIRED_INCLUDES=(
        "[include auto_flow_defaults.cfg]"
        "[include auto_flow_user.cfg]"
        "[include material_profiles_defaults.cfg]"
        "[gcode_interceptor]"
        "[extruder_monitor]"
    )
    
    # Check if printer.cfg exists
    if [ ! -f "$PRINTER_CFG" ]; then
        echo "[!] Warning: printer.cfg not found at $PRINTER_CFG"
        echo "[!] Skipping automatic configuration"
        return 1
    fi
    
    # Check which includes are missing
    local MISSING_INCLUDES=()
    for include in "${REQUIRED_INCLUDES[@]}"; do
        if ! line_exists_in_printer_cfg "$include"; then
            MISSING_INCLUDES+=("$include")
        fi
    done
    
    # If nothing to add, we're done
    if [ ${#MISSING_INCLUDES[@]} -eq 0 ]; then
        echo "[OK] printer.cfg already has all required configuration"
        return 0
    fi
    
    # Create backup before modifying
    local BACKUP_FILE="${PRINTER_CFG}.backup.$(date +%Y%m%d_%H%M%S)"
    echo "[>>] Creating backup: $BACKUP_FILE"
    cp "$PRINTER_CFG" "$BACKUP_FILE"
    
    # Create temp file with missing includes at the top
    local TEMP_FILE=$(mktemp)
    
    # Add missing includes directly (no header needed)
    for include in "${MISSING_INCLUDES[@]}"; do
        echo "$include" >> "$TEMP_FILE"
    done
    
    # Add a blank line for separation
    echo "" >> "$TEMP_FILE"
    
    # Append original printer.cfg content
    cat "$PRINTER_CFG" >> "$TEMP_FILE"
    
    # Replace printer.cfg with updated version
    mv "$TEMP_FILE" "$PRINTER_CFG"
    
    echo "[OK] Added to printer.cfg:"
    for include in "${MISSING_INCLUDES[@]}"; do
        echo "     $include"
    done
    echo ""
    echo "[OK] Backup saved: $BACKUP_FILE"
    
    return 0
}

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
    echo "Migrating your old config..."
    BACKUP_FILE="$CONFIG_DIR/auto_flow.cfg.backup.$(date +%Y%m%d_%H%M%S)"
    mv "$OLD_AUTO" "$BACKUP_FILE"
    echo "[OK] Old config moved to: $BACKUP_FILE"
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
    echo "Migrating your old material profiles..."
    BACKUP_FILE="$CONFIG_DIR/material_profiles.cfg.backup.$(date +%Y%m%d_%H%M%S)"
    mv "$OLD_MAT" "$BACKUP_FILE"
    echo "[OK] Old profiles moved to: $BACKUP_FILE"
    echo ""
    echo "Please review your backup and manually copy any custom"
    echo "materials to $USER_MAT"
    echo ""
    read -p "Press Enter to continue..."
fi

# Clean up old moonraker auto-analysis service if it exists
SERVICE_NAME="adaptive-flow-hook.service"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME"

if [ -f "$SERVICE_FILE" ]; then
    echo ""
    echo "[!] CLEANUP: Old moonraker auto-analysis service detected"
    echo "The automatic print analysis service is no longer recommended."
    echo "Removing $SERVICE_NAME..."
    
    # Stop and disable the service
    sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    sudo systemctl disable "$SERVICE_NAME" 2>/dev/null || true
    
    # Remove the service file
    sudo rm -f "$SERVICE_FILE"
    
    # Reload systemd
    sudo systemctl daemon-reload
    
    echo "[OK] Old service removed successfully"
    echo ""
    echo "Note: Manual print analysis is still available."
    echo "See docs/ANALYSIS.md for usage instructions."
    echo ""
fi

# Fix critical bug: empty gcode: in user config overrides the entire core logic
# This was a design flaw in auto_flow_user.cfg.example - the empty gcode: line
# caused Klipper's configparser to replace _AUTO_TEMP_CORE's 700-line gcode with nothing
if [ -f "$USER_CFG" ]; then
    if grep -q "^gcode:" "$USER_CFG" 2>/dev/null; then
        echo ""
        echo "[!] CRITICAL FIX: Found empty 'gcode:' in auto_flow_user.cfg"
        echo "    This was overriding the entire core adaptive flow logic!"
        echo "    Removing the empty gcode: line..."
        
        # Create backup
        BACKUP_FILE="${USER_CFG}.backup.$(date +%Y%m%d_%H%M%S)"
        cp "$USER_CFG" "$BACKUP_FILE"
        
        # Remove the bare "gcode:" line (with optional trailing whitespace)
        sed -i '/^gcode:[[:space:]]*$/d' "$USER_CFG"
        
        echo "[OK] Fixed! Backup saved: $BACKUP_FILE"
        echo ""
    fi
fi

# Update printer.cfg automatically
echo ""
echo "[>>] Checking printer.cfg configuration..."
if update_printer_cfg; then
    echo "[OK] printer.cfg is configured correctly"
else
    echo ""
    echo "[!] Could not automatically update printer.cfg"
    echo "[>>] Please manually add the following to your printer.cfg:"
    echo "   [include auto_flow_defaults.cfg]"
    echo "   [include auto_flow_user.cfg]"
    echo "   [include material_profiles_defaults.cfg]"
    echo "   [include material_profiles_user.cfg]  # Optional"
    echo "   [gcode_interceptor]"
    echo "   [extruder_monitor]"
fi
echo ""
echo "[>>] Restarting Klipper..."
sudo systemctl restart klipper

echo ""
echo "[OK] All done! Your custom settings are preserved."
