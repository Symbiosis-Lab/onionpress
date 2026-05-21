#!/bin/bash

# Build OnionPress .deb and AppImage packages for Linux
# Usage: bash build/build-linux.sh
#
# Outputs:
#   build/onionpress_VERSION_ARCH.deb
#   build/OnionPress-VERSION-ARCH.AppImage

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$PROJECT_DIR/build"
STAGE_DIR=$(mktemp -d)

# ─── Detect version ────────────────────────────────────────────────────

VERSION=$(grep 'self\.version *= *"' "$PROJECT_DIR/src/menubar.py" | head -1 | sed 's/.*"\(.*\)".*/\1/')
if [ -z "$VERSION" ]; then
    echo "ERROR: Could not detect version from src/menubar.py"
    exit 1
fi
echo "Building OnionPress v$VERSION for Linux"

# Architecture: "all" because the package contains only scripts,
# Docker Compose files, and Python — no compiled binaries.
# Docker pulls the correct container images for the platform at runtime.
DEB_ARCH="all"
echo "Architecture: all (scripts only, works on any platform)"

# ─── Collect source files ──────────────────────────────────────────────
# These are the files that get installed to /opt/onionpress

collect_files() {
    local dest="$1"

    mkdir -p "$dest"

    # Main launcher script
    cp "$PROJECT_DIR/linux/onionpress" "$dest/onionpress"
    chmod +x "$dest/onionpress"

    # Docker Compose files
    cp -r "$PROJECT_DIR/app/Resources/docker" "$dest/docker"

    # Plugins
    if [ -d "$PROJECT_DIR/app/Resources/plugins" ]; then
        cp -r "$PROJECT_DIR/app/Resources/plugins" "$dest/plugins"
    fi

    # Themes (onionpress theme used by root redirect, directory, blank canvas,
    # page-follow, page-blogroll, page-creations, onionname header)
    if [ -d "$PROJECT_DIR/app/Resources/themes" ]; then
        cp -r "$PROJECT_DIR/app/Resources/themes" "$dest/themes"
    fi

    # Scripts
    mkdir -p "$dest/scripts"
    cp "$PROJECT_DIR/src/onionpress/onion_auth.py" "$dest/scripts/"
    cp "$PROJECT_DIR/src/onionpress/key_manager.py" "$dest/scripts/"
    if [ -f "$PROJECT_DIR/linux/onionpress-heartbeat.py" ]; then
        cp "$PROJECT_DIR/linux/onionpress-heartbeat.py" "$dest/scripts/"
    fi

    # Version
    echo "$VERSION" > "$dest/VERSION"
}

# ─── Build .deb ─────────────────────────────────────────────────────────

echo ""
echo "Building .deb package..."

DEB_NAME="onionpress_${VERSION}_${DEB_ARCH}"
DEB_ROOT="$STAGE_DIR/deb/$DEB_NAME"

# Install files
collect_files "$DEB_ROOT/opt/onionpress"

# Systemd user services. The unit files use user-unit idioms
# (WantedBy=default.target, XDG_RUNTIME_DIR=/run/user/%U) — they belong
# under /usr/lib/systemd/user/, not /lib/systemd/system/. The previous
# layout enabled them as system units, which is a no-op for
# default.target and meant services never actually started at boot.
mkdir -p "$DEB_ROOT/usr/lib/systemd/user"
cp "$PROJECT_DIR/linux/onionpress.service" "$DEB_ROOT/usr/lib/systemd/user/"
cp "$PROJECT_DIR/linux/onionpress-heartbeat.service" "$DEB_ROOT/usr/lib/systemd/user/"
cp "$PROJECT_DIR/linux/onionpress-watcher.service" "$DEB_ROOT/usr/lib/systemd/user/"
cp "$PROJECT_DIR/linux/onionpress-watcher.timer" "$DEB_ROOT/usr/lib/systemd/user/"

# CLI symlink
mkdir -p "$DEB_ROOT/usr/local/bin"
ln -s /opt/onionpress/onionpress "$DEB_ROOT/usr/local/bin/onionpress"

# DEBIAN control
mkdir -p "$DEB_ROOT/DEBIAN"
cat > "$DEB_ROOT/DEBIAN/control" <<EOF
Package: onionpress
Version: $VERSION
Section: web
Priority: optional
Architecture: $DEB_ARCH
Depends: docker.io | docker-ce, docker-compose-plugin | docker-compose, jq, python3, zip, unzip
Maintainer: Brewster Kahle <brewster@archive.org>
Homepage: https://onionpress.org
Description: Your Decentralized Social Blog Site
 OnionPress turns your computer into a web server running WordPress,
 accessible via the Tor network. Your site gets its own permanent
 .onion address backed up by the Internet Archive's Wayback Machine.
 .
 Built on WordPress + Tor + Wayback Machine.
EOF

# Post-install: data dir + secrets (first install only), port-binding
# override (idempotent), enable user units.
#
# Gating ($1=configure, $2 empty = fresh install; $2 set = upgrade from
# that version). First-install logic must NOT re-run on upgrade or it
# clobbers user-edited config and re-generates secrets that don't match
# the running DB.
cat > "$DEB_ROOT/DEBIAN/postinst" <<'POSTINST'
#!/bin/bash
set -e

# Detect the human user who invoked the install. SUDO_USER is set for
# sudo invocations; logname falls back to the controlling terminal.
# Both can be empty for non-interactive paths (cloud-init, GUI software
# centers); in that case data goes to root's home and the user re-runs
# setup as themselves.
REAL_USER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"
REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)
DATA_DIR="$REAL_HOME/.onionpress"

# Idempotent: data dir creation runs on every postinst trigger.
if [ "$REAL_USER" != "root" ]; then
    install -d -o "$REAL_USER" -g "$REAL_USER" "$DATA_DIR"
    install -d -o "$REAL_USER" -g "$REAL_USER" "$DATA_DIR/shared"
    install -d -o "$REAL_USER" -g "$REAL_USER" "$DATA_DIR/shared/vanity-keys"
else
    mkdir -p "$DATA_DIR/shared/vanity-keys"
fi

# First-install-only: random secrets + default config.
if [ "$1" = "configure" ] && [ -z "$2" ]; then
    if [ ! -f "$DATA_DIR/secrets" ]; then
        WP_PASS=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)
        ROOT_PASS=$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)
        cat > "$DATA_DIR/secrets" <<EOF
# Database passwords - generated on $(date)
WORDPRESS_DB_PASSWORD='$WP_PASS'
MYSQL_PASSWORD='$WP_PASS'
MYSQL_ROOT_PASSWORD='$ROOT_PASS'
EOF
        chmod 600 "$DATA_DIR/secrets"
    fi

    if [ ! -f "$DATA_DIR/config" ]; then
        cat > "$DATA_DIR/config" <<EOF
ADDRESS_PREFIX=op2
INSTALL_IA_PLUGIN=yes
UPDATE_ON_LAUNCH=no
START_ON_BOOT=yes
REGISTER_WITH_ONIONHEAVEN=yes
ONIONHEAVEN_ADDRESS=oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion
EOF
    fi
fi

# Ownership fixup is idempotent.
if [ "$REAL_USER" != "root" ]; then
    chown -R "$REAL_USER:$REAL_USER" "$DATA_DIR"
fi

# Port-binding host. Compose now reads ${ONIONPRESS_BIND_HOST:-127.0.0.1}
# from the environment, set by the launcher (linux/onionpress) — no
# more sed-mutating docker-compose.yml in postinst. The launcher exports
# 0.0.0.0 by default on Linux for headless-Pi LAN access; users wanting
# localhost-only can set ONIONPRESS_BIND_HOST=127.0.0.1 in
# ~/.onionpress/config.

# Reload the user-unit catalog and enable the units globally so they
# activate on the next login of any user. Per-user disable preferences
# (a user who explicitly opted out with `systemctl --user disable`)
# remain respected — --global only affects users whose state is unset.
systemctl --global enable onionpress.service onionpress-heartbeat.service onionpress-watcher.timer 2>/dev/null || true
systemctl --user daemon-reload 2>/dev/null || true

if [ "$1" = "configure" ] && [ -z "$2" ]; then
    cat <<EOF

OnionPress v$(cat /opt/onionpress/VERSION) installed.

Next: open a shell as your normal user and run:
    loginctl enable-linger \$USER
    systemctl --user start onionpress

CLI:  onionpress status | address | logs

EOF
fi
POSTINST
chmod 755 "$DEB_ROOT/DEBIAN/postinst"

# Pre-remove: only act on actual removal, not upgrade.
cat > "$DEB_ROOT/DEBIAN/prerm" <<'PRERM'
#!/bin/bash
set -e

case "$1" in
    remove)
        # Globally disable so the units don't relink on next login.
        # Per-user running instances are left to the user to stop with
        # `systemctl --user stop onionpress` — root can't reach into
        # arbitrary user sessions from prerm reliably.
        systemctl --global disable onionpress.service onionpress-heartbeat.service onionpress-watcher.timer 2>/dev/null || true
        ;;
    upgrade|deconfigure|failed-upgrade)
        # No-op — preserve running state across upgrade.
        ;;
esac
PRERM
chmod 755 "$DEB_ROOT/DEBIAN/prerm"

# Post-remove: clean up postinst-generated files on purge.
cat > "$DEB_ROOT/DEBIAN/postrm" <<'POSTRM'
#!/bin/bash
set -e

case "$1" in
    purge)
        # Don't auto-remove ~/.onionpress/ — it holds the onion key and
        # DB passwords. Tell the user what's still there.
        cat <<EOF
onionpress purged. User data preserved at:
    ~/.onionpress/   (config, secrets, onion key, logs)
    ~/OnionPress/    (Creations, backups)
Remove manually if you no longer need them.
EOF
        ;;
esac
POSTRM
chmod 755 "$DEB_ROOT/DEBIAN/postrm"

# Build the .deb
if command -v dpkg-deb >/dev/null 2>&1; then
    dpkg-deb --build "$DEB_ROOT" "$BUILD_DIR/${DEB_NAME}.deb"
else
    # dpkg-deb not available (e.g. building on macOS) — assemble manually.
    # A .deb is an ar archive containing: debian-binary, control.tar.gz, data.tar.gz
    echo "  dpkg-deb not available, assembling .deb manually..."

    DEB_TMP="$STAGE_DIR/deb-parts"
    mkdir -p "$DEB_TMP"

    # debian-binary
    echo "2.0" > "$DEB_TMP/debian-binary"

    # control.tar.gz
    (cd "$DEB_ROOT/DEBIAN" && tar czf "$DEB_TMP/control.tar.gz" .)

    # data.tar.gz — everything except DEBIAN/
    (cd "$DEB_ROOT" && tar czf "$DEB_TMP/data.tar.gz" --exclude='./DEBIAN' .)

    # Assemble with ar (BSD ar on macOS needs 'r' not 'rcs')
    # But macOS ar adds Mach-O warnings, so use a simple concatenation approach
    # that matches the Debian ar format: "!<arch>\n" header + members
    python3 -c "
import struct, os, time

def ar_header(name, size):
    name = name.ljust(16)
    mtime = str(int(time.time())).ljust(12)
    uid = '0'.ljust(6)
    gid = '0'.ljust(6)
    mode = '100644'.ljust(8)
    fsize = str(size).ljust(10)
    return f'{name}{mtime}{uid}{gid}{mode}{fsize}\x60\n'.encode()

parts_dir = '$DEB_TMP'
out_path = '$BUILD_DIR/${DEB_NAME}.deb'

with open(out_path, 'wb') as out:
    out.write(b'!<arch>\n')
    for fname in ['debian-binary', 'control.tar.gz', 'data.tar.gz']:
        fpath = os.path.join(parts_dir, fname)
        fsize = os.path.getsize(fpath)
        out.write(ar_header(fname, fsize))
        with open(fpath, 'rb') as f:
            out.write(f.read())
        # ar members must be 2-byte aligned
        if fsize % 2 != 0:
            out.write(b'\n')

print(f'  .deb assembled: {out_path}')
"
fi

DEB_SIZE=$(du -h "$BUILD_DIR/${DEB_NAME}.deb" | cut -f1)
echo "  .deb created: build/${DEB_NAME}.deb ($DEB_SIZE)"

# ─── Build AppImage ────────────────────────────────────────────────────

echo ""
echo "Building AppImage..."

APPIMAGE_NAME="OnionPress-${VERSION}"
APPDIR="$STAGE_DIR/appimage/$APPIMAGE_NAME.AppDir"

# Install files
collect_files "$APPDIR/opt/onionpress"

# Systemd services (for optional install)
mkdir -p "$APPDIR/opt/onionpress/systemd"
cp "$PROJECT_DIR/linux/onionpress.service" "$APPDIR/opt/onionpress/systemd/"
cp "$PROJECT_DIR/linux/onionpress-heartbeat.service" "$APPDIR/opt/onionpress/systemd/"
cp "$PROJECT_DIR/linux/onionpress-watcher.service" "$APPDIR/opt/onionpress/systemd/"
cp "$PROJECT_DIR/linux/onionpress-watcher.timer" "$APPDIR/opt/onionpress/systemd/"

# AppRun — the entry point
cat > "$APPDIR/AppRun" <<'APPRUN'
#!/bin/bash

# OnionPress AppImage entry point
# Runs the OnionPress launcher from the bundled files

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"

# Set install dir to our bundled files
export ONIONPRESS_INSTALL_DIR="$SELF_DIR/opt/onionpress"

# Handle special commands
case "${1:-}" in
    install-service)
        echo "Installing OnionPress systemd services..."
        REAL_USER="${SUDO_USER:-$(whoami)}"
        REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)

        # Get the actual AppImage path (not the mounted AppDir)
        APPIMAGE_PATH="${APPIMAGE:-$(readlink -f "$0")}"

        for svc in onionpress onionpress-heartbeat onionpress-watcher; do
            if [ -f "$SELF_DIR/opt/onionpress/systemd/$svc.service" ]; then
                sudo cp "$SELF_DIR/opt/onionpress/systemd/$svc.service" "/etc/systemd/system/$svc.service"
                # Point ExecStart at the AppImage
                sudo sed -i "s|/opt/onionpress/onionpress|$APPIMAGE_PATH|g" "/etc/systemd/system/$svc.service"
                if [ "$REAL_USER" != "root" ]; then
                    sudo sed -i "/^\[Service\]/a User=$REAL_USER\nEnvironment=HOME=$REAL_HOME" \
                        "/etc/systemd/system/$svc.service"
                fi
            fi
        done
        if [ -f "$SELF_DIR/opt/onionpress/systemd/onionpress-watcher.timer" ]; then
            sudo cp "$SELF_DIR/opt/onionpress/systemd/onionpress-watcher.timer" "/etc/systemd/system/"
        fi

        sudo systemctl daemon-reload
        sudo systemctl enable onionpress onionpress-heartbeat
        sudo systemctl enable --now onionpress-watcher.timer
        echo "Services installed. Start with: sudo systemctl start onionpress"
        exit 0
        ;;
esac

# Run the launcher
exec "$SELF_DIR/opt/onionpress/onionpress" "$@"
APPRUN
chmod +x "$APPDIR/AppRun"

# Desktop file (required by AppImage spec)
cat > "$APPDIR/onionpress.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=OnionPress
Comment=Your Decentralized Social Blog Site
Exec=onionpress
Icon=onionpress
Categories=Network;WebDevelopment;
Terminal=true
EOF

# Icon (required by AppImage spec — use the app icon if available, else create a placeholder)
if [ -f "$PROJECT_DIR/app/Resources/app-icon-source.png" ]; then
    cp "$PROJECT_DIR/app/Resources/app-icon-source.png" "$APPDIR/onionpress.png"
elif [ -f "$PROJECT_DIR/docs/favicon.png" ]; then
    cp "$PROJECT_DIR/docs/favicon.png" "$APPDIR/onionpress.png"
else
    # Minimal 1x1 PNG placeholder
    printf '\x89PNG\r\n\x1a\n' > "$APPDIR/onionpress.png"
fi

# .DirIcon symlink
ln -sf onionpress.png "$APPDIR/.DirIcon"

# Build the AppImage using appimagetool if available, otherwise create a
# self-extracting archive as a fallback
if command -v appimagetool >/dev/null 2>&1; then
    appimagetool "$APPDIR" "$BUILD_DIR/${APPIMAGE_NAME}.AppImage"
else
    echo "  appimagetool not found — creating self-extracting archive instead"

    # Create a tar.gz of the AppDir
    ARCHIVE="$STAGE_DIR/appdir.tar.gz"
    (cd "$APPDIR/.." && tar czf "$ARCHIVE" "$(basename "$APPDIR")")

    # Create self-extracting script
    cat > "$BUILD_DIR/${APPIMAGE_NAME}.AppImage" <<SFXHEADER
#!/bin/bash
# OnionPress v$VERSION — Self-extracting AppImage
# Usage: ./$(basename "$BUILD_DIR/${APPIMAGE_NAME}.AppImage") [command]

set -e
EXTRACT_DIR="\${XDG_CACHE_HOME:-\$HOME/.cache}/onionpress-appimage"
MARKER="\$EXTRACT_DIR/.version"

# Extract only if version changed or not yet extracted
if [ ! -f "\$MARKER" ] || [ "\$(cat "\$MARKER")" != "$VERSION" ]; then
    rm -rf "\$EXTRACT_DIR"
    mkdir -p "\$EXTRACT_DIR"
    # Skip the header lines of this script and extract the archive
    SKIP=\$(awk '/^__ARCHIVE_BELOW__\$/{print NR + 1; exit 0; }' "\$0")
    tail -n +\$SKIP "\$0" | tar xzf - -C "\$EXTRACT_DIR" --strip-components=1
    echo "$VERSION" > "\$MARKER"
fi

export ONIONPRESS_INSTALL_DIR="\$EXTRACT_DIR/opt/onionpress"
exec "\$EXTRACT_DIR/AppRun" "\$@"
__ARCHIVE_BELOW__
SFXHEADER

    cat "$ARCHIVE" >> "$BUILD_DIR/${APPIMAGE_NAME}.AppImage"
    chmod +x "$BUILD_DIR/${APPIMAGE_NAME}.AppImage"
fi

APPIMAGE_SIZE=$(du -h "$BUILD_DIR/${APPIMAGE_NAME}.AppImage" | cut -f1)
echo "  AppImage created: build/${APPIMAGE_NAME}.AppImage ($APPIMAGE_SIZE)"

# ─── Clean up ───────────────────────────────────────────────────────────

rm -rf "$STAGE_DIR"

echo ""
echo "✅ Linux packages built:"
echo "   .deb:      build/${DEB_NAME}.deb"
echo "   AppImage:  build/${APPIMAGE_NAME}.AppImage"
echo ""
echo "Install .deb:     sudo apt-get install ./build/${DEB_NAME}.deb"
echo "Run AppImage:     ./build/${APPIMAGE_NAME}.AppImage"
echo "Install service:  ./build/${APPIMAGE_NAME}.AppImage install-service"
