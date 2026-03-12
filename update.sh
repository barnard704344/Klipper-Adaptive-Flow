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
    # Copy the helper modules that analyze_print.py imports
    for af_mod in af_config.py af_hardware.py af_slicer.py af_analysis.py; do
        if [ -f "$af_mod" ]; then
            cp "$af_mod" "$KLIPPER_EXTRAS/"
        fi
    done
fi

# Symlink system defaults (always safe to update, git pull auto-applies)
echo "[>>] Linking system defaults..."
for f in auto_flow_defaults.cfg material_profiles_defaults.cfg; do
    if [ -L "$CONFIG_DIR/$f" ]; then
        # Already a symlink — nothing to do
        echo "[OK] $f already symlinked"
    elif [ -f "$CONFIG_DIR/$f" ]; then
        # Existing plain file — check if user modified it
        if ! diff -q "$REPO_DIR/$f" "$CONFIG_DIR/$f" >/dev/null 2>&1; then
            BACKUP="${CONFIG_DIR}/${f}.backup.$(date +%Y%m%d_%H%M%S)"
            echo "[!] $f has local modifications — backing up to $(basename "$BACKUP")"
            cp "$CONFIG_DIR/$f" "$BACKUP"
            echo "    If you customized settings, move them to the matching _user.cfg file"
        fi
        rm -f "$CONFIG_DIR/$f"
        ln -s "$REPO_DIR/$f" "$CONFIG_DIR/$f"
        echo "[OK] $f → symlinked to git repo"
    else
        # No file at all — create symlink
        ln -s "$REPO_DIR/$f" "$CONFIG_DIR/$f"
        echo "[OK] $f → symlinked to git repo"
    fi
done

# Clean up deprecated files from previous versions
for OLD_FILE in "$KLIPPER_EXTRAS/moonraker_hook.py" "$CONFIG_DIR/analysis_config.cfg" "$CONFIG_DIR/moonraker_hook.py"; do
    if [ -f "$OLD_FILE" ]; then
        rm -f "$OLD_FILE"
        echo "[OK] Removed deprecated: $OLD_FILE"
    fi
done

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
    if [ -t 0 ]; then read -p "Press Enter to continue..."; fi
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
    if [ -t 0 ]; then read -p "Press Enter to continue..."; fi
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

# Ensure heater_wattage is present and uncommented in user config
# Older templates had it commented out, so existing users may be missing it.
# The default (40W) is used if absent, but having it visible encourages
# users to set it correctly for their hardware.
if [ -f "$USER_CFG" ]; then
    # First: deduplicate — keep only the first occurrence
    DUP_COUNT=$(grep -c "^variable_heater_wattage:" "$USER_CFG" 2>/dev/null || true)
    if [ "$DUP_COUNT" -gt 1 ]; then
        # Keep the first uncommented line, remove subsequent ones
        awk '/^variable_heater_wattage:/ { if (!seen) { seen=1; print } else { next } } !/^variable_heater_wattage:/' "$USER_CFG" > "${USER_CFG}.tmp" && mv "${USER_CFG}.tmp" "$USER_CFG"
        echo "[OK] Removed duplicate heater_wattage lines in auto_flow_user.cfg"
    fi

    if grep -q "^variable_heater_wattage:" "$USER_CFG" 2>/dev/null; then
        : # already uncommented — nothing to do
    elif grep -q "^# *variable_heater_wattage:" "$USER_CFG" 2>/dev/null; then
        # Present but commented — uncomment it
        sed -i 's/^# *variable_heater_wattage:/variable_heater_wattage:/' "$USER_CFG"
        echo "[OK] Uncommented heater_wattage in auto_flow_user.cfg (was commented)"
    else
        # Missing entirely — add it after the nozzle type line
        if grep -q "^variable_use_high_flow_nozzle:" "$USER_CFG" 2>/dev/null; then
            sed -i '/^variable_use_high_flow_nozzle:/a\# Heater cartridge wattage: 40 = stock Revo, 60 = upgrade\nvariable_heater_wattage: 40' "$USER_CFG"
            echo "[OK] Added heater_wattage to auto_flow_user.cfg (default: 40W)"
        fi
    fi
fi

# Remove defunct Smart Cooling / Heater-Adaptive Fan settings
# These features were removed; the variables no longer exist in defaults.
# Leaving them causes no harm (Klipper ignores unknown variables in a merge)
# but they clutter the config and confuse users.
if [ -f "$USER_CFG" ]; then
    if grep -q "variable_sc_" "$USER_CFG" 2>/dev/null; then
        sed -i '/^# *SMART COOLING/d' "$USER_CFG"
        sed -i '/^# *HEATER-ADAPTIVE FAN/d' "$USER_CFG"
        sed -i '/^# *variable_sc_/d' "$USER_CFG"
        sed -i '/^variable_sc_/d' "$USER_CFG"
        # Clean up any resulting blank line runs (3+ blank lines → 1)
        sed -i '/^$/N;/^\n$/d' "$USER_CFG"
        echo "[OK] Removed defunct Smart Cooling settings from auto_flow_user.cfg"
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

# ==========================================================================
# DASHBOARD SERVICE
# ==========================================================================
DASH_SERVICE="adaptive-flow-dashboard.service"
DASH_FILE="/etc/systemd/system/$DASH_SERVICE"

echo ""
echo "[>>] Setting up Adaptive Flow Dashboard service..."

# Generate the service file dynamically using the current user's name and
# home directory so it works on any username (not just 'pi').
CURRENT_USER="$(whoami)"
CURRENT_HOME="$(getent passwd "$CURRENT_USER" | cut -d: -f6)"
# Fall back to $HOME if getent is unavailable (e.g. on some minimal installs)
CURRENT_HOME="${CURRENT_HOME:-$HOME}"

sudo tee "$DASH_FILE" > /dev/null <<EOF
[Unit]
Description=Adaptive Flow Dashboard
Documentation=https://github.com/barnard704344/Klipper-Adaptive-Flow
After=network.target klipper.service
Wants=klipper.service

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${CURRENT_HOME}/Klipper-Adaptive-Flow
ExecStart=/usr/bin/python3 ${CURRENT_HOME}/Klipper-Adaptive-Flow/analyze_print.py --serve --port 7127
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

if [ $? -eq 0 ]; then
    sudo systemctl daemon-reload
    sudo systemctl enable "$DASH_SERVICE" 2>/dev/null

    # Restart dashboard to pick up new code
    if systemctl is-active --quiet "$DASH_SERVICE"; then
        sudo systemctl restart "$DASH_SERVICE"
        echo "[OK] Dashboard service restarted"
    else
        sudo systemctl start "$DASH_SERVICE"
        echo "[OK] Dashboard service started"
    fi

    # Show access URL
    IP_ADDR=$(hostname -I 2>/dev/null | awk '{print $1}')
    if [ -n "$IP_ADDR" ]; then
        echo "[OK] Dashboard: http://${IP_ADDR}:7127"
    else
        echo "[OK] Dashboard: http://localhost:7127"
    fi
else
    echo "[!] Could not install dashboard service (sudo required)"
fi

echo ""
echo "[OK] All done! Your custom settings are preserved."
