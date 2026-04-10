#!/bin/bash
# Bump the OnionPress version across all files.
#
# Usage: build/bump-version.sh 2.4.47
#
# This updates the 2 canonical sources (src/menubar.py and the outer Info.plist)
# plus derived files (__init__.py, MenubarApp plist). setup.py reads from
# menubar.py dynamically at build time, so it needs no update.

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Usage: $0 <version>"
    echo "Example: $0 2.4.47"
    exit 1
fi

NEW_VERSION="$1"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Validate version format (X.Y.Z)
if ! echo "$NEW_VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "ERROR: Version must be in X.Y.Z format (got: $NEW_VERSION)"
    exit 1
fi

echo "Bumping OnionPress to v$NEW_VERSION"
echo ""

# 1. src/menubar.py — self.version (canonical source #1)
sed -i '' "s/self\.version = \"[^\"]*\"/self.version = \"$NEW_VERSION\"/" \
    "$PROJECT_DIR/src/menubar.py"
echo "  Updated src/menubar.py"

# 2. app/Info.plist (canonical source #2)
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $NEW_VERSION" \
    "$PROJECT_DIR/app/Info.plist"
echo "  Updated app/Info.plist"

# 3. src/onionpress/__init__.py (derived)
sed -i '' "s/__version__ = \"[^\"]*\"/__version__ = \"$NEW_VERSION\"/" \
    "$PROJECT_DIR/src/onionpress/__init__.py"
echo "  Updated src/onionpress/__init__.py"

# 4. MenubarApp plist (derived — also rebuilt by py2app, but update for non-rebuild releases)
MENUBAR_PLIST="$PROJECT_DIR/OnionPress.app/Contents/Resources/MenubarApp/Contents/Info.plist"
if [ -f "$MENUBAR_PLIST" ]; then
    /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $NEW_VERSION" "$MENUBAR_PLIST"
    /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $NEW_VERSION" "$MENUBAR_PLIST"
    echo "  Updated MenubarApp/Contents/Info.plist"
fi

# Verify all locations match
echo ""
echo "Verification:"
MENUBAR_VER=$(grep 'self\.version *= *"' "$PROJECT_DIR/src/menubar.py" | head -1 | sed 's/.*"\(.*\)".*/\1/')
PLIST_VER=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$PROJECT_DIR/app/Info.plist")
INIT_VER=$(grep '__version__' "$PROJECT_DIR/src/onionpress/__init__.py" | sed 's/.*"\(.*\)".*/\1/')

ALL_MATCH=true
for name_ver in "src/menubar.py:$MENUBAR_VER" "Info.plist:$PLIST_VER" "__init__.py:$INIT_VER"; do
    name="${name_ver%%:*}"
    ver="${name_ver##*:}"
    if [ "$ver" = "$NEW_VERSION" ]; then
        echo "  $name: $ver"
    else
        echo "  $name: $ver  *** MISMATCH ***"
        ALL_MATCH=false
    fi
done

if [ -f "$MENUBAR_PLIST" ]; then
    MB_VER=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$MENUBAR_PLIST")
    if [ "$MB_VER" = "$NEW_VERSION" ]; then
        echo "  MenubarApp plist: $MB_VER"
    else
        echo "  MenubarApp plist: $MB_VER  *** MISMATCH ***"
        ALL_MATCH=false
    fi
fi

echo ""
if $ALL_MATCH; then
    echo "All versions updated to $NEW_VERSION"
else
    echo "ERROR: Some versions did not update correctly!"
    exit 1
fi
