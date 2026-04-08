#!/bin/bash

# OnionPress Installer for Raspberry Pi / Linux
# Usage: curl -sSL https://raw.githubusercontent.com/brewsterkahle/onionpress/main/linux/install.sh | bash
#
# Or clone the repo and run: bash linux/install.sh
#
# Installs Docker in rootless mode for security — container compromise
# cannot escalate to host root.

set -e

INSTALL_DIR="/opt/onionpress"
REPO_URL="https://github.com/brewsterkahle/onionpress"

# Resolve real user (not root when run via sudo)
if [ -n "$SUDO_USER" ]; then
    REAL_USER="$SUDO_USER"
    REAL_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
    REAL_UID=$(id -u "$SUDO_USER")
else
    REAL_USER="$(whoami)"
    REAL_HOME="$HOME"
    REAL_UID=$(id -u)
fi
DATA_DIR="$REAL_HOME/.onionpress"
USER_SYSTEMD_DIR="$REAL_HOME/.config/systemd/user"

echo ""
echo "  OnionPress Installer for Linux"
echo "  ==============================="
echo ""

# ─── Checks ──────────────────────────────────────────────────────────

# Check architecture
ARCH=$(uname -m)
case "$ARCH" in
    aarch64|arm64)
        echo "  Architecture: ARM64 (Raspberry Pi / Apple Silicon)"
        ;;
    x86_64)
        echo "  Architecture: x86_64"
        ;;
    armv7l)
        echo "  Architecture: ARM32 (may be slow, 64-bit OS recommended)"
        ;;
    *)
        echo "ERROR: Unsupported architecture: $ARCH"
        exit 1
        ;;
esac

# Check for root/sudo
if [ "$EUID" -ne 0 ]; then
    if ! command -v sudo >/dev/null 2>&1; then
        echo "ERROR: This script must be run as root or with sudo available."
        exit 1
    fi
    SUDO="sudo"
else
    SUDO=""
fi

# ─── Install Docker (rootless) ──────────────────────────────────────

# Helper to run a command as the real user (not root)
run_as_user() {
    if [ "$REAL_USER" = "$(whoami)" ]; then
        "$@"
    else
        sudo -u "$REAL_USER" "$@"
    fi
}

if run_as_user docker info >/dev/null 2>&1; then
    echo "  Docker: already installed ($(run_as_user docker --version | cut -d' ' -f3 | tr -d ','))"
else
    echo "  Installing Docker (rootless mode)..."

    # Install prerequisites for rootless Docker
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq uidmap dbus-user-session curl ca-certificates

    # Install Docker engine packages (needed for rootless setup tool)
    if ! command -v dockerd >/dev/null 2>&1; then
        # Add Docker's official GPG key and repository
        $SUDO install -m 0755 -d /etc/apt/keyrings
        $SUDO curl -fsSL https://download.docker.com/linux/$(. /etc/os-release && echo "$ID")/gpg \
            -o /etc/apt/keyrings/docker.asc
        $SUDO chmod a+r /etc/apt/keyrings/docker.asc

        echo \
          "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
          https://download.docker.com/linux/$(. /etc/os-release && echo "$ID") \
          $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
          $SUDO tee /etc/apt/sources.list.d/docker.list > /dev/null

        $SUDO apt-get update -qq
        $SUDO apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin docker-ce-rootless-extras
    fi

    # Disable the system-wide Docker daemon — we use rootless instead
    $SUDO systemctl disable --now docker.service docker.socket 2>/dev/null || true

    # Enable lingering so the user's systemd services run at boot without login
    $SUDO loginctl enable-linger "$REAL_USER"

    # Set up rootless Docker as the real user
    # XDG_RUNTIME_DIR must be set for the setup tool
    run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
        dockerd-rootless-setuptool.sh install

    # Start the user's rootless Docker daemon
    run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
        systemctl --user start docker.service
    run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
        systemctl --user enable docker.service

    echo "  Docker installed (rootless mode)"
fi

# Check docker compose plugin
if run_as_user docker compose version >/dev/null 2>&1; then
    echo "  Docker Compose: available"
else
    echo "  Installing Docker Compose plugin..."
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq docker-compose-plugin
    echo "  Docker Compose plugin installed"
fi

# Ensure jq is available (used by status command)
if ! command -v jq >/dev/null 2>&1; then
    echo "  Installing jq..."
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq jq
fi

# Ensure python3 is available
if ! command -v python3 >/dev/null 2>&1; then
    echo "  Installing python3..."
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq python3
fi

# Ensure unzip and zip are available (needed for plugin installs and backups)
if ! command -v unzip >/dev/null 2>&1 || ! command -v zip >/dev/null 2>&1; then
    echo "  Installing zip/unzip..."
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq zip unzip
fi

# ─── Stop existing services (if reinstalling) ────────────────────────

# Stop user services (rootless)
if run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
    systemctl --user is-active --quiet onionpress 2>/dev/null; then
    echo "  Stopping existing OnionPress service..."
    run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
        systemctl --user stop onionpress-heartbeat 2>/dev/null || true
    run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
        systemctl --user stop onionpress-watcher.timer 2>/dev/null || true
    run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
        systemctl --user stop onionpress 2>/dev/null || true
fi

# Also stop old system-level services from previous installs
if $SUDO systemctl is-active --quiet onionpress 2>/dev/null; then
    echo "  Stopping old system-level OnionPress service..."
    $SUDO systemctl stop onionpress-heartbeat 2>/dev/null || true
    $SUDO systemctl stop onionpress-watcher.timer 2>/dev/null || true
    $SUDO systemctl stop onionpress 2>/dev/null || true
    $SUDO systemctl disable onionpress onionpress-heartbeat onionpress-watcher.timer 2>/dev/null || true
    $SUDO rm -f /etc/systemd/system/onionpress.service \
                /etc/systemd/system/onionpress-heartbeat.service \
                /etc/systemd/system/onionpress-watcher.service \
                /etc/systemd/system/onionpress-watcher.timer
    $SUDO systemctl daemon-reload
    echo "  Migrated from system services to user services"
fi

# ─── Install OnionPress files ────────────────────────────────────────

echo ""
echo "  Installing OnionPress to $INSTALL_DIR..."

$SUDO mkdir -p "$INSTALL_DIR"

# Determine source: if we're in the repo, use local files; otherwise clone
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/onionpress" ] && [ -d "$SCRIPT_DIR/../OnionPress.app" ]; then
    # Running from cloned repo
    REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
    echo "  Source: local repo at $REPO_DIR"
else
    # Download from GitHub
    echo "  Downloading from GitHub..."
    TMPDIR=$(mktemp -d)
    git clone --depth 1 "$REPO_URL" "$TMPDIR/onionpress"
    REPO_DIR="$TMPDIR/onionpress"
fi

# Verify source directory exists
if [ ! -d "$REPO_DIR/OnionPress.app/Contents/Resources/docker" ]; then
    echo "ERROR: Docker resources not found at $REPO_DIR/OnionPress.app/Contents/Resources/docker"
    echo "       Make sure you're running from the full OnionPress repo."
    exit 1
fi

# Copy files
$SUDO cp "$REPO_DIR/linux/onionpress" "$INSTALL_DIR/onionpress"
$SUDO chmod +x "$INSTALL_DIR/onionpress"

$SUDO cp -r "$REPO_DIR/OnionPress.app/Contents/Resources/docker" "$INSTALL_DIR/docker"
$SUDO cp -r "$REPO_DIR/OnionPress.app/Contents/Resources/plugins" "$INSTALL_DIR/plugins"

if [ -d "$REPO_DIR/OnionPress.app/Contents/Resources/scripts" ]; then
    $SUDO cp -r "$REPO_DIR/OnionPress.app/Contents/Resources/scripts" "$INSTALL_DIR/scripts"
fi

# Copy shared scripts
$SUDO mkdir -p "$INSTALL_DIR/scripts"
$SUDO cp "$REPO_DIR/src/onion_auth.py" "$INSTALL_DIR/scripts/"
$SUDO cp "$REPO_DIR/src/key_manager.py" "$INSTALL_DIR/scripts/"
if [ -f "$REPO_DIR/linux/onionpress-heartbeat.py" ]; then
    $SUDO cp "$REPO_DIR/linux/onionpress-heartbeat.py" "$INSTALL_DIR/scripts/"
fi

# Copy systemd service files into install dir (so they survive temp dir cleanup)
$SUDO cp "$REPO_DIR/linux/onionpress.service" "$INSTALL_DIR/"
$SUDO cp "$REPO_DIR/linux/onionpress-heartbeat.service" "$INSTALL_DIR/"
$SUDO cp "$REPO_DIR/linux/onionpress-watcher.service" "$INSTALL_DIR/"
$SUDO cp "$REPO_DIR/linux/onionpress-watcher.timer" "$INSTALL_DIR/"

# Bind WordPress and SOCKS ports to 0.0.0.0 for LAN access (Pi is headless,
# users access from another device). The main compose file uses 127.0.0.1.
$SUDO sed -i 's/127\.0\.0\.1:\${ONIONPRESS_WP_PORT/0.0.0.0:${ONIONPRESS_WP_PORT/' "$INSTALL_DIR/docker/docker-compose.yml"
$SUDO sed -i 's/127\.0\.0\.1:\${ONIONPRESS_SOCKS_PORT/0.0.0.0:${ONIONPRESS_SOCKS_PORT/' "$INSTALL_DIR/docker/docker-compose.yml"

# Write version file (VERSION file is the single source of truth)
VERSION="unknown"
if [ -f "$REPO_DIR/VERSION" ]; then
    VERSION=$(cat "$REPO_DIR/VERSION")
elif [ -f "$REPO_DIR/OnionPress.app/Contents/Info.plist" ] && command -v python3 >/dev/null 2>&1; then
    VERSION=$(python3 -c "
import xml.etree.ElementTree as ET, sys
try:
    tree = ET.parse(sys.argv[1])
    keys = list(tree.iter())
    for i, el in enumerate(keys):
        if el.tag == 'key' and el.text == 'CFBundleShortVersionString':
            print(keys[i+1].text)
            break
except:
    print('unknown')
" "$REPO_DIR/OnionPress.app/Contents/Info.plist" 2>/dev/null || echo "unknown")
fi
echo "$VERSION" | $SUDO tee "$INSTALL_DIR/VERSION" > /dev/null

echo "  OnionPress $VERSION installed to $INSTALL_DIR"

# Clean up temp dir if we cloned
if [ -n "${TMPDIR:-}" ] && [ -d "${TMPDIR:-}" ]; then
    rm -rf "$TMPDIR"
fi

# ─── Create data directory & secrets ─────────────────────────────────

echo ""
echo "  Setting up data directory..."

# Use install -d -o to create dirs owned by the real user, not root
if [ -n "$SUDO_USER" ]; then
    install -d -o "$SUDO_USER" -g "$SUDO_USER" -m 700 "$DATA_DIR"
    install -d -o "$SUDO_USER" -g "$SUDO_USER" "$DATA_DIR/shared"
    install -d -o "$SUDO_USER" -g "$SUDO_USER" "$DATA_DIR/shared/vanity-keys"
else
    mkdir -p "$DATA_DIR"
    chmod 700 "$DATA_DIR"
    mkdir -p "$DATA_DIR/shared/vanity-keys"
fi

# Generate secrets if they don't exist
if [ ! -f "$DATA_DIR/secrets" ]; then
    WP_PASS=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)
    ROOT_PASS=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)

    touch "$DATA_DIR/secrets"
    chmod 600 "$DATA_DIR/secrets"
    cat > "$DATA_DIR/secrets" <<EOF
# Database passwords - generated on $(date)
# DO NOT SHARE THESE PASSWORDS
WORDPRESS_DB_PASSWORD='$WP_PASS'
MYSQL_PASSWORD='$WP_PASS'
MYSQL_ROOT_PASSWORD='$ROOT_PASS'
EOF
    echo "  Database passwords generated"
else
    echo "  Existing secrets preserved"
fi

# Create default config
if [ ! -f "$DATA_DIR/config" ]; then
    cat > "$DATA_DIR/config" <<EOF
ADDRESS_PREFIX=op2
INSTALL_IA_PLUGIN=yes
UPDATE_ON_LAUNCH=no
START_ON_BOOT=yes
REGISTER_WITH_ONIONHEAVEN=yes
ONIONHEAVEN_ADDRESS=oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion
EOF
    echo "  Default config created"
fi

# Ensure all data dir files are owned by the real user (not root)
if [ -n "$SUDO_USER" ]; then
    chown -R "$SUDO_USER:$SUDO_USER" "$DATA_DIR"
fi

# ─── Install systemd user services ──────────────────────────────────

echo ""
echo "  Installing systemd user services..."

# Create user systemd directory
if [ -n "$SUDO_USER" ]; then
    install -d -o "$SUDO_USER" -g "$SUDO_USER" "$USER_SYSTEMD_DIR"
else
    mkdir -p "$USER_SYSTEMD_DIR"
fi

# Copy service files to user systemd directory (from install dir, not repo — repo may be cleaned up)
cp "$INSTALL_DIR/onionpress.service" "$USER_SYSTEMD_DIR/onionpress.service"
cp "$INSTALL_DIR/onionpress-heartbeat.service" "$USER_SYSTEMD_DIR/onionpress-heartbeat.service"
cp "$INSTALL_DIR/onionpress-watcher.service" "$USER_SYSTEMD_DIR/onionpress-watcher.service"
cp "$INSTALL_DIR/onionpress-watcher.timer" "$USER_SYSTEMD_DIR/onionpress-watcher.timer"

# Ensure owned by user
if [ -n "$SUDO_USER" ]; then
    chown "$SUDO_USER:$SUDO_USER" "$USER_SYSTEMD_DIR"/onionpress*
fi

# Enable lingering (ensures user services start at boot, even without login)
$SUDO loginctl enable-linger "$REAL_USER"

# Reload and enable user services
run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
    systemctl --user daemon-reload
run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
    systemctl --user enable onionpress
run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
    systemctl --user enable --now onionpress-watcher.timer
run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
    systemctl --user enable onionpress-heartbeat
echo "  Systemd user services installed and enabled (starts on boot)"

# ─── Start OnionPress ─────────────────────────────────────────────────

echo ""
echo "  Starting OnionPress (this may take a few minutes on first run)..."
echo "  Docker will pull container images for WordPress, MariaDB, and Tor."
echo ""

# Use restart (not start) so a stale service from a previous install is replaced.
# If the service isn't running, restart acts like start.
run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
    systemctl --user restart onionpress

# Start heartbeat client (will wait for containers to be ready)
run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
    systemctl --user restart onionpress-heartbeat

# Wait for the service to finish starting
echo "  Waiting for services..."
local_ip=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

# Wait for WordPress container to respond.
# First run on a Pi can take 2-3 minutes (image pulls + DB bootstrap).
wp_ready=false
wp_wait=0
while [ $wp_wait -lt 180 ]; do
    if curl -s --max-time 3 "http://localhost:8080" >/dev/null 2>&1; then
        wp_ready=true
        break
    fi
    sleep 3
    wp_wait=$((wp_wait + 3))
done

if [ "$wp_ready" != "true" ]; then
    echo "  WARNING: WordPress did not respond within 3 minutes."
    echo "  It may still be starting. Check: journalctl --user -u onionpress -f"
fi

# Check if it started successfully
if run_as_user env XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
    systemctl --user is-active --quiet onionpress; then
    # Try to get the onion address
    onion_addr=$("$INSTALL_DIR/onionpress" address 2>/dev/null) || true

    echo ""
    echo "  ======================================="
    echo "  OnionPress is running!"
    echo "  ======================================="
    echo ""
    echo "  Local access:  http://${local_ip}:8080"
    echo "  Status page:   http://${local_ip}:8080/onionpress-status"
    echo ""
    if [ "$onion_addr" != "Generating..." ] && [ -n "$onion_addr" ]; then
        echo "  Onion address: http://${onion_addr}"
    else
        echo "  Onion address: Still generating... (run 'onionpress address' to check)"
    fi
    echo ""
    # Note: first-time WordPress setup (WP install, multisite, plugins) is handled
    # automatically by start_containers when running headless (non-interactive).
    # No need to call 'onionpress setup' here — it would race with the systemd service.

    echo ""
    echo "  Commands:"
    echo "    onionpress status       - Show container status"
    echo "    onionpress address      - Show .onion address"
    echo "    onionpress logs         - Stream container logs"
    echo "    onionpress write-status - Update status page data"
    echo "    systemctl --user restart onionpress - Restart"
    echo "    systemctl --user stop onionpress    - Stop"
    echo ""
    echo "  Log file: $DATA_DIR/onionpress.log"
    echo ""
else
    echo ""
    echo "  WARNING: OnionPress may still be starting."
    echo "  Check status with: systemctl --user status onionpress"
    echo "  Check logs with:   journalctl --user -u onionpress"
    echo "  Or:                cat $DATA_DIR/onionpress.log"
    echo ""
fi

# Create symlink for easy CLI access
$SUDO ln -sf "$INSTALL_DIR/onionpress" /usr/local/bin/onionpress 2>/dev/null || true
