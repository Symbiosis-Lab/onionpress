#!/usr/bin/env python3
"""Tor bootstrap watchdog — replaces tor-watchdog.sh.

Monitors Tor's bootstrap progress via the control port using Python sockets
(no xxd/nc dependency). If Tor doesn't reach 100% within timeout, deletes
/var/lib/tor/state and kills Tor so the caller can retry.

Usage: python3 tor-watchdog.py [timeout_secs]
"""

import os
import re
import signal
import socket
import sys
import time

TIMEOUT = int(sys.argv[1]) if len(sys.argv) > 1 else 120
CHECK_INTERVAL = 10
CONTROL_PORT = 9051
COOKIE_PATH = "/var/lib/tor/control_auth_cookie"


def get_bootstrap_progress() -> int:
    """Query Tor bootstrap progress via control port. Returns 0-100."""
    try:
        cookie = open(COOKIE_PATH, "rb").read().hex()
    except (OSError, FileNotFoundError):
        return -1

    try:
        s = socket.create_connection(("127.0.0.1", CONTROL_PORT), timeout=5)
        s.sendall(
            f"AUTHENTICATE {cookie}\r\n"
            f"GETINFO status/bootstrap-phase\r\n"
            f"QUIT\r\n".encode()
        )
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()

        m = re.search(r"PROGRESS=(\d+)", data.decode("utf-8", errors="replace"))
        return int(m.group(1)) if m else -1
    except (OSError, socket.timeout):
        return -1


def main():
    elapsed = 0
    while elapsed < TIMEOUT:
        time.sleep(CHECK_INTERVAL)
        elapsed += CHECK_INTERVAL

        progress = get_bootstrap_progress()
        if progress < 0:
            continue

        if progress >= 100:
            print(f"tor-watchdog: bootstrap complete ({elapsed}s)", file=sys.stderr)
            sys.exit(0)

        print(
            f"tor-watchdog: bootstrap at {progress}% ({elapsed}s/{TIMEOUT}s)",
            file=sys.stderr,
        )

    # Timed out — delete state, kill Tor
    print(
        "tor-watchdog: TIMEOUT — bootstrap stuck, deleting state and killing Tor",
        file=sys.stderr,
    )
    for f in ("/var/lib/tor/state", "/var/lib/tor/lock"):
        try:
            os.unlink(f)
        except OSError:
            pass

    # Kill Tor — read PID from pidfile or find it
    try:
        import subprocess

        result = subprocess.run(
            ["pidof", "tor"], capture_output=True, text=True, timeout=5
        )
        if result.stdout.strip():
            for pid in result.stdout.strip().split():
                os.kill(int(pid), signal.SIGTERM)
    except Exception:
        pass

    sys.exit(1)


if __name__ == "__main__":
    main()
