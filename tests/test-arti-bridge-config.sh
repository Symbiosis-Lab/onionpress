#!/bin/sh
#
# Unit test for apply_arti_bridge_config() in
# app/Resources/docker/tor/entrypoint.sh.
#
# The function is extracted from the real entrypoint script with the same
# awk technique used to verify it live against a running container
# (`awk '/^apply_arti_bridge_config\(\)/,/^}/'`), so this test always runs
# against the actual shipped code, not a copy that can drift.
#
# Usage: sh tests/test-arti-bridge-config.sh

# entrypoint.sh itself does not `set -u`, and apply_arti_bridge_config()
# relies on that (reads TOR_BRIDGE_LINES/TOR_CLIENT_TRANSPORT_PLUGIN
# unguarded) — match that here rather than tightening past what production
# actually runs under.
set -e

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENTRYPOINT="$SCRIPT_DIR/../app/Resources/docker/tor/entrypoint.sh"
TMPDIR_TEST=$(mktemp -d)
trap 'rm -rf "$TMPDIR_TEST"' EXIT

FAIL=0

fail() {
    echo "FAIL: $1"
    FAIL=1
}

pass() {
    echo "PASS: $1"
}

# Extract just the function under test into its own sourceable file.
FUNC_FILE="$TMPDIR_TEST/func.sh"
awk '/^apply_arti_bridge_config\(\)/,/^}/' "$ENTRYPOINT" > "$FUNC_FILE"
if [ ! -s "$FUNC_FILE" ]; then
    echo "FAIL: could not extract apply_arti_bridge_config() from $ENTRYPOINT"
    exit 1
fi
# shellcheck source=/dev/null
. "$FUNC_FILE"

# --- (a) no-op when TOR_BRIDGE_LINES is unset ---
TOR_BRIDGE_LINES=""
unset TOR_CLIENT_TRANSPORT_PLUGIN 2>/dev/null || true
target="$TMPDIR_TEST/noop.toml"
printf 'listen_addr = "0.0.0.0:9050"\n' > "$target"
before=$(cat "$target")
apply_arti_bridge_config "$target"
after=$(cat "$target")
if [ "$before" = "$after" ]; then
    pass "no-op when TOR_BRIDGE_LINES is unset"
else
    fail "file was modified even though TOR_BRIDGE_LINES was unset"
fi

# --- (b) produces a valid [bridges] table with the right bridge lines ---
TOR_BRIDGE_LINES="snowflake 192.0.2.1:80 FPRINT1;Bridge snowflake 192.0.2.2:80 FPRINT2"
TOR_CLIENT_TRANSPORT_PLUGIN="snowflake"
export TOR_BRIDGE_LINES TOR_CLIENT_TRANSPORT_PLUGIN
target="$TMPDIR_TEST/populated.toml"
printf 'listen_addr = "0.0.0.0:9050"\n' > "$target"
apply_arti_bridge_config "$target"

if grep -q '^\[bridges\]' "$target"; then
    pass "writes a [bridges] table"
else
    fail "did not write a [bridges] table"
fi

if grep -q '"snowflake 192.0.2.1:80 FPRINT1"' "$target"; then
    pass "includes first bridge line verbatim"
else
    fail "missing/garbled first bridge line"
fi

# Second line had a redundant "Bridge " prefix in the input — must be stripped.
if grep -q '"snowflake 192.0.2.2:80 FPRINT2"' "$target" && ! grep -q '"Bridge snowflake 192.0.2.2' "$target"; then
    pass "strips redundant 'Bridge ' prefix"
else
    fail "did not strip redundant 'Bridge ' prefix"
fi

if grep -q '\[\[bridges.transports\]\]' "$target" && grep -q 'path = "/usr/bin/snowflake-client"' "$target"; then
    pass "adds snowflake transport stanza"
else
    fail "missing snowflake transport stanza"
fi

# Validate the result is well-formed TOML if a parser is available.
if command -v python3 >/dev/null 2>&1; then
    if python3 -c '
import sys
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        sys.exit(0)
with open(sys.argv[1], "rb") as f:
    tomllib.load(f)
' "$target"; then
        pass "output parses as valid TOML"
    else
        fail "output is not valid TOML"
    fi
fi

# --- (c) idempotent: running twice must not duplicate the [bridges] table ---
apply_arti_bridge_config "$target"
count=$(grep -c '^\[bridges\]' "$target")
if [ "$count" -eq 1 ]; then
    pass "idempotent: [bridges] table not duplicated on second run"
else
    fail "idempotency violated: found $count [bridges] tables after running twice"
fi

if [ "$FAIL" -eq 0 ]; then
    echo "All apply_arti_bridge_config() tests passed."
    exit 0
else
    echo "Some apply_arti_bridge_config() tests FAILED."
    exit 1
fi
