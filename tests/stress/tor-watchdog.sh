#!/bin/sh
# Tor bootstrap watchdog — restarts Tor if it doesn't reach 100% within timeout.
#
# Usage: tor-watchdog.sh [timeout_secs]
#   Default timeout: 120 seconds
#
# Monitors Tor's bootstrap progress via the control port. If Tor hasn't reached
# 100% within the timeout, deletes /var/lib/tor/state (forces fresh guard
# selection) and sends SIGTERM to Tor. The caller (start.sh) should restart Tor
# when it exits.
#
# Requires: xxd, nc (netcat-openbsd), tor with ControlPort on 127.0.0.1:9051

TIMEOUT="${1:-120}"
CHECK_INTERVAL=10
elapsed=0

while [ "$elapsed" -lt "$TIMEOUT" ]; do
    sleep "$CHECK_INTERVAL"
    elapsed=$((elapsed + CHECK_INTERVAL))

    # Check bootstrap progress via control port
    cookie=$(xxd -p /var/lib/tor/control_auth_cookie 2>/dev/null | tr -d '\n')
    [ -z "$cookie" ] && continue

    progress=$(printf 'AUTHENTICATE %s\r\nGETINFO status/bootstrap-phase\r\nQUIT\r\n' "$cookie" \
        | nc -w 5 127.0.0.1 9051 2>/dev/null \
        | grep "PROGRESS=" | sed 's/.*PROGRESS=\([0-9]*\).*/\1/')

    [ -z "$progress" ] && continue

    if [ "$progress" -ge 100 ] 2>/dev/null; then
        echo "tor-watchdog: bootstrap complete (${elapsed}s)" >&2
        exit 0
    fi

    echo "tor-watchdog: bootstrap at ${progress}% (${elapsed}s/${TIMEOUT}s)" >&2
done

# Timed out — kill Tor and delete state to force fresh guard selection
echo "tor-watchdog: TIMEOUT — bootstrap stuck, deleting state and killing Tor" >&2
rm -f /var/lib/tor/state /var/lib/tor/lock
TOR_PID=$(pidof tor 2>/dev/null)
if [ -n "$TOR_PID" ]; then
    kill "$TOR_PID" 2>/dev/null
fi
exit 1
