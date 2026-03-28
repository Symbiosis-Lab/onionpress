#!/usr/bin/env python3
"""Tor control port watchdog — monitors Tor health and recovers from failures.

Runs inside every C Tor container. Connects to the local control port,
subscribes to events, and sends DROPGUARDS / NEWNYM when Tor gets stuck
(stale guards after sleep, clock jumps, bootstrap stalls, etc.).

Usage: Started by entrypoint.sh in the background after Tor launches.
       Only runs when TOR_IMPL=tor (not Arti).
"""

import os
import socket
import sys
import time

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 9051
COOKIE_PATH = "/var/lib/tor/control_auth_cookie"

# Rate limits (seconds)
DROPGUARDS_COOLDOWN = 30
DORMANT_COOLDOWN = 120       # 2 min after DROPGUARDS → try DORMANT/ACTIVE
HALT_COOLDOWN = 300  # 5 minutes — last resort

# Detection thresholds
FAILED_NODE_THRESHOLD = 5       # failures within window → DROPGUARDS
FAILED_NODE_WINDOW = 60         # seconds
BOOTSTRAP_STALL_TIMEOUT = 120   # no progress for 2 min → DROPGUARDS
HS_DESC_UPLOAD_TIMEOUT = 120    # no descriptor upload 2 min after recovery
HSFETCH_INTERVAL = 30           # flush client descriptor cache every 30s after recovery

# Reconnect delay when control port isn't available yet
CONNECT_RETRY_DELAY = 5


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(msg):
    ts = time.strftime("%b %d %H:%M:%S", time.gmtime())
    print(f"{ts} [tor-watchdog] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Control port connection
# ---------------------------------------------------------------------------
def read_cookie():
    """Read the Tor control auth cookie file."""
    for _ in range(60):  # retry for up to 5 minutes
        try:
            with open(COOKIE_PATH, "rb") as f:
                return f.read()
        except FileNotFoundError:
            time.sleep(CONNECT_RETRY_DELAY)
    return None


def connect_and_auth():
    """Connect to control port and authenticate. Returns socket or None."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect((CONTROL_HOST, CONTROL_PORT))

        cookie = read_cookie()
        if cookie is None:
            log("Could not read auth cookie — giving up")
            s.close()
            return None

        s.sendall(b"AUTHENTICATE " + cookie.hex().encode() + b"\r\n")
        resp = recv_response(s)
        if not resp.startswith("250"):
            log(f"Authentication failed: {resp.strip()}")
            s.close()
            return None

        return s
    except (ConnectionRefusedError, OSError) as e:
        return None


def recv_response(s):
    """Read a single control port response (may be multi-line)."""
    buf = b""
    while True:
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
        # Single-line response: "250 OK\r\n"
        # Multi-line: "250-first\r\n250 last\r\n"
        lines = buf.decode("utf-8", errors="replace").split("\r\n")
        for line in lines:
            if line and len(line) >= 4 and line[3] == " ":
                return buf.decode("utf-8", errors="replace")
    return buf.decode("utf-8", errors="replace")


def send_cmd(s, cmd):
    """Send a command and return the response."""
    try:
        s.sendall((cmd + "\r\n").encode())
        return recv_response(s)
    except (BrokenPipeError, OSError) as e:
        return f"ERROR: {e}"


# ---------------------------------------------------------------------------
# Watchdog state
# ---------------------------------------------------------------------------
# How much wall clock drift indicates a sleep/wake
SLEEP_DETECT_THRESHOLD = 30  # seconds — event timeout is 15s, so >30s means we slept


class WatchdogState:
    def __init__(self):
        self.bootstrapped = False
        self.last_bootstrap_pct = 0
        self.last_bootstrap_change = time.time()
        self.last_dropguards = 0
        self.last_dormant = 0
        self.last_halt = 0
        self.failed_node_count = 0
        self.last_loop_time = time.time()  # for sleep detection
        self.last_heartbeat_log = time.time()  # periodic "alive" log
        self.failed_node_window_start = time.time()
        self.last_recovery_time = 0  # when we last did DROPGUARDS
        self.hs_desc_uploaded_since_recovery = False
        self.last_hsfetch = 0  # periodic descriptor refresh after recovery
        self.onion_addresses = []  # populated from hostname files


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
def discover_onion_addresses():
    """Find onion addresses to HSFETCH — our own services + the content address.

    Looks in:
    - /var/lib/tor/hidden_service/*/hostname (our own onion services)
    - /var/lib/onionpress/onion_address (content address from shared volume —
      needed by onionheaven which does reachability checks via its SOCKS proxy)
    """
    import glob
    addresses = set()
    for path in glob.glob("/var/lib/tor/hidden_service/*/hostname"):
        try:
            with open(path) as f:
                addr = f.read().strip()
                if addr.endswith(".onion"):
                    addresses.add(addr.replace(".onion", ""))
        except OSError:
            pass
    # Content address (shared volume) — the address we're checking reachability for
    try:
        with open("/var/lib/onionpress/onion_address") as f:
            addr = f.read().strip()
            if addr.endswith(".onion"):
                addresses.add(addr.replace(".onion", ""))
    except OSError:
        pass
    return list(addresses)


def do_dropguards(cmd_sock, state, reason):
    """Send DROPGUARDS + NEWNYM with rate limiting."""
    now = time.time()
    if now - state.last_dropguards < DROPGUARDS_COOLDOWN:
        return

    log(f"Recovering: {reason}")

    resp = send_cmd(cmd_sock, "DROPGUARDS")
    if "250" in resp:
        log("Sent DROPGUARDS — fresh guard selection")
    else:
        log(f"DROPGUARDS failed: {resp.strip()}")

    # NEWNYM is rate-limited by Tor to 10s, but we just send it
    resp = send_cmd(cmd_sock, "SIGNAL NEWNYM")
    if "250" in resp:
        log("Sent SIGNAL NEWNYM — new circuits")
    else:
        log(f"SIGNAL NEWNYM failed: {resp.strip()}")

    state.last_dropguards = now
    state.last_recovery_time = now
    state.hs_desc_uploaded_since_recovery = False
    state.failed_node_count = 0


def do_dormant_cycle(cmd_sock, state, reason):
    """Mid-level recovery: DORMANT → ACTIVE forces clean re-bootstrap without restart."""
    now = time.time()
    if now - state.last_dormant < DORMANT_COOLDOWN:
        return

    log(f"Escalating: {reason}")

    resp = send_cmd(cmd_sock, "SIGNAL DORMANT")
    if "250" in resp:
        log("Sent SIGNAL DORMANT — Tor closing circuits and clearing state")
    else:
        log(f"SIGNAL DORMANT failed: {resp.strip()}")
        return

    time.sleep(3)

    resp = send_cmd(cmd_sock, "SIGNAL ACTIVE")
    if "250" in resp:
        log("Sent SIGNAL ACTIVE — Tor re-bootstrapping with fresh state")
    else:
        log(f"SIGNAL ACTIVE failed: {resp.strip()}")

    state.last_dormant = now
    state.bootstrapped = False


def do_halt(cmd_sock, state, reason):
    """Last resort: tell Tor to shut down. Docker restart policy brings it back."""
    now = time.time()
    if now - state.last_halt < HALT_COOLDOWN:
        return

    log(f"LAST RESORT: {reason} — sending SIGNAL HALT")
    send_cmd(cmd_sock, "SIGNAL HALT")
    state.last_halt = now


# ---------------------------------------------------------------------------
# Event processing
# ---------------------------------------------------------------------------
def process_event(line, cmd_sock, state):
    """Process a single event line from the control port."""

    # --- Clock jump ---
    if "CLOCK_SKEW" in line or "clock just jumped" in line:
        do_dropguards(cmd_sock, state, "clock jump detected")
        return

    # --- Failed to find node for hop #1 ---
    if "Failed to find node" in line:
        now = time.time()
        # Reset window if expired
        if now - state.failed_node_window_start > FAILED_NODE_WINDOW:
            state.failed_node_count = 0
            state.failed_node_window_start = now
        state.failed_node_count += 1

        if state.failed_node_count >= FAILED_NODE_THRESHOLD:
            do_dropguards(cmd_sock, state,
                          f"{state.failed_node_count} guard failures in {FAILED_NODE_WINDOW}s")
        return

    # --- Guard exhaustion ---
    if "No usable guards" in line or "All current guards excluded" in line:
        do_dropguards(cmd_sock, state, "guard exhaustion")
        return

    # --- Network recovery ---
    if "Tor now sees network activity" in line:
        do_dropguards(cmd_sock, state, "network came back")
        return

    # --- Bootstrap progress ---
    if "BOOTSTRAP" in line or "Bootstrapped" in line:
        # Try to extract percentage
        pct = _extract_bootstrap_pct(line)
        if pct is not None:
            if pct != state.last_bootstrap_pct:
                state.last_bootstrap_pct = pct
                state.last_bootstrap_change = time.time()
            if pct >= 100:
                if not state.bootstrapped:
                    log("Tor bootstrapped to 100%")
                state.bootstrapped = True
                state.failed_node_count = 0
            else:
                state.bootstrapped = False
        return

    # --- Descriptor upload (onion service containers) ---
    if "HS_DESC UPLOADED" in line:
        state.hs_desc_uploaded_since_recovery = True
        return


def _extract_bootstrap_pct(line):
    """Extract bootstrap percentage from a log or event line."""
    # Event format: "STATUS_CLIENT ... BOOTSTRAP PROGRESS=55 ..."
    if "PROGRESS=" in line:
        for part in line.split():
            if part.startswith("PROGRESS="):
                try:
                    return int(part.split("=")[1])
                except ValueError:
                    pass
    # Log format: "Bootstrapped 55% ..."
    if "Bootstrapped" in line:
        for part in line.split():
            if part.endswith("%"):
                try:
                    return int(part.rstrip("%"))
                except ValueError:
                    pass
    return None


def check_stalls(cmd_sock, state):
    """Periodic check for stalls that events alone can't catch."""
    now = time.time()

    # Sleep/wake detection: if wall clock jumped more than expected,
    # the system slept. Tor may not emit any events about this but
    # circuits are likely dead.
    elapsed = now - state.last_loop_time
    state.last_loop_time = now
    if elapsed > SLEEP_DETECT_THRESHOLD and state.bootstrapped:
        do_dropguards(cmd_sock, state,
                      f"system sleep detected (wall clock jumped {elapsed:.0f}s)")

    # Periodic heartbeat log (every 5 minutes) so we can tell the watchdog is alive
    if now - state.last_heartbeat_log > 300:
        ce = "?"
        resp = send_cmd(cmd_sock, "GETINFO status/circuit-established")
        if "circuit-established=" in resp:
            ce = resp.split("circuit-established=")[1].split()[0].strip()
        log(f"alive — bootstrapped={state.bootstrapped}, circuit-established={ce}")
        state.last_heartbeat_log = now

    # Active circuit health check — if Tor reports no circuits, recover
    if state.bootstrapped:
        resp = send_cmd(cmd_sock, "GETINFO status/circuit-established")
        if "circuit-established=0" in resp:
            do_dropguards(cmd_sock, state, "circuit-established=0 (circuits lost)")

    # Bootstrap stall: not at 100% and no progress for BOOTSTRAP_STALL_TIMEOUT
    if (not state.bootstrapped
            and state.last_bootstrap_pct > 0
            and now - state.last_bootstrap_change > BOOTSTRAP_STALL_TIMEOUT
            and now - state.last_dropguards > DROPGUARDS_COOLDOWN):
        do_dropguards(cmd_sock, state,
                      f"bootstrap stalled at {state.last_bootstrap_pct}% for {BOOTSTRAP_STALL_TIMEOUT}s")

    # Descriptor upload stall (only for onion service containers)
    if (state.last_recovery_time > 0
            and not state.hs_desc_uploaded_since_recovery
            and now - state.last_recovery_time > HS_DESC_UPLOAD_TIMEOUT
            and state.bootstrapped):
        log(f"Warning: no HS_DESC upload {HS_DESC_UPLOAD_TIMEOUT}s after recovery")
        # Reset so we don't warn repeatedly
        state.last_recovery_time = 0

    # Periodic HSFETCH after recovery — flush stale descriptor cache so
    # reachability checks (via this Tor's SOCKS) pick up fresh descriptors.
    # NEWNYM clears the client cache; HSFETCH forces a fresh fetch from HSDirs.
    if (state.last_recovery_time > 0
            and state.bootstrapped
            and now - state.last_hsfetch > HSFETCH_INTERVAL):
        # Discover addresses on first use
        if not state.onion_addresses:
            state.onion_addresses = discover_onion_addresses()
        if state.onion_addresses:
            # NEWNYM first to clear cached (stale) descriptors, then HSFETCH
            send_cmd(cmd_sock, "SIGNAL NEWNYM")
            for addr in state.onion_addresses:
                resp = send_cmd(cmd_sock, f"HSFETCH {addr}")
                if "250" in resp:
                    log(f"HSFETCH {addr[:16]}... — refreshing descriptor")
                else:
                    log(f"HSFETCH failed: {resp.strip()}")
        state.last_hsfetch = now

    # Escalation: DORMANT/ACTIVE if DROPGUARDS didn't work after 2 minutes.
    # Only safe for SOCKS-only containers — DORMANT kills onion services permanently.
    if (os.environ.get("NO_ONION_SERVICE") == "1"
            and state.last_dropguards > 0
            and not state.bootstrapped
            and now - state.last_dropguards > DORMANT_COOLDOWN
            and now - state.last_dormant > DORMANT_COOLDOWN):
        do_dormant_cycle(cmd_sock, state,
                         f"DROPGUARDS didn't recover after {DORMANT_COOLDOWN}s — trying DORMANT/ACTIVE")

    # Last resort: if DORMANT/ACTIVE also failed after 5 minutes total
    if (state.last_dropguards > 0
            and not state.bootstrapped
            and now - state.last_dropguards > HALT_COOLDOWN
            and now - state.last_halt > HALT_COOLDOWN):
        do_halt(cmd_sock, state,
                f"still not bootstrapped {HALT_COOLDOWN}s after recovery attempts")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run():
    log("Starting tor-watchdog")
    state = WatchdogState()

    while True:
        # Connect event socket
        log("Connecting to control port...")
        event_sock = None
        while event_sock is None:
            event_sock = connect_and_auth()
            if event_sock is None:
                time.sleep(CONNECT_RETRY_DELAY)

        # Connect command socket
        cmd_sock = None
        while cmd_sock is None:
            cmd_sock = connect_and_auth()
            if cmd_sock is None:
                time.sleep(CONNECT_RETRY_DELAY)

        # Subscribe to events
        resp = send_cmd(event_sock, "SETEVENTS STATUS_CLIENT STATUS_GENERAL NOTICE WARN HS_DESC")
        if "250" not in resp:
            log(f"Failed to subscribe to events: {resp.strip()}")
            event_sock.close()
            cmd_sock.close()
            time.sleep(CONNECT_RETRY_DELAY)
            continue

        # Flush stale descriptor cache from before restart so reachability
        # checks (which use this Tor's SOCKS proxy) get fresh descriptors.
        resp = send_cmd(cmd_sock, "SIGNAL NEWNYM")
        if "250" in resp:
            log("Flushed descriptor cache (NEWNYM on startup)")

        # Check current bootstrap status so we don't start with bootstrapped=False
        # when Tor is already at 100% (e.g. after watchdog restart).
        resp = send_cmd(cmd_sock, "GETINFO status/bootstrap-phase")
        if "PROGRESS=100" in resp:
            state.bootstrapped = True
            log("Tor already bootstrapped to 100%")
        else:
            pct = _extract_bootstrap_pct(resp)
            if pct is not None:
                state.last_bootstrap_pct = pct
                log(f"Tor bootstrap at {pct}%")

        log("Connected — monitoring Tor health")
        event_sock.settimeout(15)  # wake up periodically for stall checks
        buf = ""

        while True:
            # Read events
            try:
                data = event_sock.recv(4096)
                if not data:
                    log("Control port connection closed — reconnecting")
                    break
                buf += data.decode("utf-8", errors="replace")
            except socket.timeout:
                # No events — check for stalls
                check_stalls(cmd_sock, state)
                continue
            except (ConnectionResetError, BrokenPipeError, OSError):
                log("Control port connection lost — reconnecting")
                break

            # Process complete lines
            while "\r\n" in buf:
                line, buf = buf.split("\r\n", 1)
                if not line:
                    continue
                # Async events start with "650"
                if line.startswith("650"):
                    process_event(line, cmd_sock, state)

            # Also check stalls on every recv cycle
            check_stalls(cmd_sock, state)

        # Clean up and reconnect
        try:
            event_sock.close()
        except OSError:
            pass
        try:
            cmd_sock.close()
        except OSError:
            pass
        time.sleep(CONNECT_RETRY_DELAY)


if __name__ == "__main__":
    # Only run for C Tor
    if os.environ.get("TOR_IMPL", "tor") != "tor":
        log("TOR_IMPL is not 'tor' — watchdog not needed for Arti")
        sys.exit(0)

    try:
        run()
    except KeyboardInterrupt:
        log("Shutting down")
    except Exception as e:
        log(f"Fatal error: {e}")
        sys.exit(1)
