#!/bin/bash
# Quick rebuild of just the MenubarApp (py2app) and install into OnionPress.app.
# The full build-dmg-simple.sh calls this section too, but this script is
# faster for iterating on src/ changes during development.
#
# Usage: build/rebuild-menubar.sh
#
# After running, quit and relaunch OnionPress.app to pick up changes.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS_DIR="$PROJECT_DIR/src"
APP_PATH="/Applications/OnionPress.app"
MENUBAR_APP_DIR="$APP_PATH/Contents/Resources/MenubarApp"
MENUBAR_BUILD_DIR=$(mktemp -d)

trap 'rm -rf "$MENUBAR_BUILD_DIR"' EXIT

echo "=== Creating venv and installing deps..."
UNIVERSAL_PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14"
if [ -x "$UNIVERSAL_PYTHON" ]; then
    echo "Using universal2 Python: $UNIVERSAL_PYTHON"
    "$UNIVERSAL_PYTHON" -m venv "$MENUBAR_BUILD_DIR/venv"
else
    echo "WARNING: universal2 Python not found, using system python3 (app may be arm64-only)"
    python3 -m venv "$MENUBAR_BUILD_DIR/venv"
fi
"$MENUBAR_BUILD_DIR/venv/bin/pip" install --upgrade pip -q
"$MENUBAR_BUILD_DIR/venv/bin/pip" install py2app -q
"$MENUBAR_BUILD_DIR/venv/bin/pip" install -r "$SCRIPTS_DIR/requirements.txt" -q

echo "=== Copying local modules to site-packages..."
SITE_PACKAGES=$("$MENUBAR_BUILD_DIR/venv/bin/python3" -c "import site; print(site.getsitepackages()[0])")
cp "$SCRIPTS_DIR/key_manager.py" "$SITE_PACKAGES/"
cp "$SCRIPTS_DIR/backup_manager.py" "$SITE_PACKAGES/"
cp "$SCRIPTS_DIR/setup_window.py" "$SITE_PACKAGES/"
cp "$SCRIPTS_DIR/onion_auth.py" "$SITE_PACKAGES/"
cp "$SCRIPTS_DIR/onionheaven.py" "$SITE_PACKAGES/"
cp -r "$SCRIPTS_DIR/onionpress" "$SITE_PACKAGES/"

echo "=== Building with py2app..."
cd "$PROJECT_DIR"
if ! "$MENUBAR_BUILD_DIR/venv/bin/python3" setup.py py2app \
    --dist-dir "$MENUBAR_BUILD_DIR/dist" \
    --bdist-base "$MENUBAR_BUILD_DIR/build" \
    2>&1 | tail -5; then
    echo "py2app failed — retrying with setuptools<81..."
    "$MENUBAR_BUILD_DIR/venv/bin/pip" install 'setuptools<81' -q
    rm -rf "$MENUBAR_BUILD_DIR/build" "$MENUBAR_BUILD_DIR/dist"
    "$MENUBAR_BUILD_DIR/venv/bin/python3" setup.py py2app \
        --dist-dir "$MENUBAR_BUILD_DIR/dist" \
        --bdist-base "$MENUBAR_BUILD_DIR/build" \
        2>&1 | tail -5
fi

echo "=== Installing into $MENUBAR_APP_DIR..."
rm -rf "$MENUBAR_APP_DIR"
if [ -d "$MENUBAR_BUILD_DIR/dist/OnionPress.app" ]; then
    mv "$MENUBAR_BUILD_DIR/dist/OnionPress.app" "$MENUBAR_APP_DIR"
elif [ -d "$MENUBAR_BUILD_DIR/dist/menubar.app" ]; then
    mv "$MENUBAR_BUILD_DIR/dist/menubar.app" "$MENUBAR_APP_DIR"
else
    echo "ERROR: py2app output not found in $MENUBAR_BUILD_DIR/dist/"
    ls "$MENUBAR_BUILD_DIR/dist/"
    exit 1
fi

# Remove broken .pyo symlinks
find "$MENUBAR_APP_DIR" -name '*.pyo' -type l -delete 2>/dev/null || true

echo "=== Done! Quit and relaunch OnionPress.app to pick up changes."
