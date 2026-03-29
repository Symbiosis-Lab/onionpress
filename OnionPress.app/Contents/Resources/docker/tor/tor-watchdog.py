#!/usr/bin/env python3
"""Tor control port watchdog — monitors Tor health and manages onion services.

Runs inside every C Tor container. Connects to the local control port,
manages onion services via ADD_ONION/DEL_ONION, subscribes to events,
and recovers from failures (stale guards, bootstrap stalls, etc.).

Signal protocol (from host MenubarApp via docker exec kill):
  USR1 = sleep  → DEL_ONION all services (Tor stays running with circuits)
  USR2 = wake   → ADD_ONION all services (re-publish on existing circuits)

Usage: Started by entrypoint.sh in the background after Tor launches.
       Only runs when TOR_IMPL=tor (not Arti).
"""

import base64
import glob
import json
import os
import signal
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
HS_DESC_UPLOAD_TIMEOUT = 60     # no descriptor upload 60s after recovery → HSFETCH
# Reconnect delay when control port isn't available yet
CONNECT_RETRY_DELAY = 5

# Hidden service key paths
HS_BASE_DIR = "/var/lib/tor/hidden_service"


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
# Onion service management (ADD_ONION / DEL_ONION)
# ---------------------------------------------------------------------------

def _read_ed25519_key(secret_key_path):
    """Read a C Tor hs_ed25519_secret_key file and return base64 for ADD_ONION."""
    with open(secret_key_path, "rb") as f:
        data = f.read()
    if len(data) != 96:
        raise ValueError(f"Secret key wrong size: {len(data)} (expected 96)")
    # 32-byte header + 64-byte expanded key
    expanded_key = data[32:]
    return base64.b64encode(expanded_key).decode("ascii")


def discover_services():
    """Find onion services from /etc/tor/onion-services.json + keys on disk.

    The JSON file is written by entrypoint.sh with service names and port
    mappings. Keys and hostnames live at /var/lib/tor/hidden_service/<name>/.

    Returns list of dicts with 'service_id', 'service_name', 'key_b64', 'ports'.
    """
    # Read service definitions from JSON
    try:
        with open("/etc/tor/onion-services.json") as f:
            svc_defs = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"No onion-services.json found ({e}) — no services to manage")
        return []

    services = []
    for svc_def in svc_defs:
        name = svc_def.get("name", "")
        ports = svc_def.get("ports", [])
        if not name or not ports:
            continue

        service_dir = os.path.join(HS_BASE_DIR, name)

        # Read hostname for service_id
        hostname_file = os.path.join(service_dir, "hostname")
        try:
            with open(hostname_file) as f:
                hostname = f.read().strip()
            service_id = hostname.replace(".onion", "")
        except OSError:
            log(f"Warning: no hostname file for {name}, skipping")
            continue

        # Read key
        secret_key_file = os.path.join(service_dir, "hs_ed25519_secret_key")
        try:
            key_b64 = _read_ed25519_key(secret_key_file)
        except (OSError, ValueError) as e:
            log(f"Warning: can't read key for {name}: {e}")
            continue

        services.append({
            "service_id": service_id,
            "service_name": name,
            "key_b64": key_b64,
            "ports": ports,
        })

    return services


def add_all_services(cmd_sock, services):
    """ADD_ONION for all services. Returns number of successes."""
    count = 0
    for svc in services:
        port_args = " ".join(f"Port={p}" for p in svc["ports"])
        cmd = f"ADD_ONION ED25519-V3:{svc['key_b64']} Flags=Detach {port_args}"
        resp = send_cmd(cmd_sock, cmd)
        if "250" in resp:
            log(f"ADD_ONION {svc['service_name']} ({svc['service_id'][:16]}...) — ok")
            count += 1
        elif "Onion address collision" in resp:
            log(f"ADD_ONION {svc['service_name']} — already active (collision)")
            count += 1
        else:
            log(f"ADD_ONION {svc['service_name']} — FAILED: {resp.strip()[:100]}")
    return count


def del_all_services(cmd_sock, services):
    """DEL_ONION for all services. Returns number of successes."""
    count = 0
    for svc in services:
        resp = send_cmd(cmd_sock, f"DEL_ONION {svc['service_id']}")
        if "250" in resp:
            log(f"DEL_ONION {svc['service_name']} ({svc['service_id'][:16]}...) — ok")
            count += 1
        else:
            log(f"DEL_ONION {svc['service_name']} — FAILED: {resp.strip()[:100]}")
    return count


# ---------------------------------------------------------------------------
# Signal handling (USR1=sleep, USR2=wake)
# ---------------------------------------------------------------------------
_signal_sleep = False
_signal_wake = False


def _handle_usr1(signum, frame):
    global _signal_sleep
    _signal_sleep = True


def _handle_usr2(signum, frame):
    global _signal_wake
    _signal_wake = True


# ---------------------------------------------------------------------------
# Watchdog state
# ---------------------------------------------------------------------------
class WatchdogState:
    def __init__(self):
        self.bootstrapped = False
        self.last_bootstrap_pct = 0
        self.last_bootstrap_change = time.time()
        self.last_dropguards = 0
        self.last_dormant = 0
        self.last_halt = 0
        self.failed_node_count = 0
        self.last_heartbeat_log = time.time()  # periodic "alive" log
        self.failed_node_window_start = time.time()
        self.last_recovery_time = 0  # when we last detected a wake
        self.hs_desc_uploaded_since_recovery = False
        self.onion_addresses = []  # for HSFETCH
        self.services = []  # discovered onion services
        self.services_active = False  # True when ADD_ONION has been done


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
def discover_onion_addresses():
    """Find onion addresses for HSFETCH — our own services + the content address."""
    addresses = set()
    for path in glob.glob(f"{HS_BASE_DIR}/*/hostname"):
        try:
            with open(path) as f:
                addr = f.read().strip()
                if addr.endswith(".onion"):
                    addresses.add(addr.replace(".onion", ""))
        except OSError:
            pass
    # Content address (shared volume) — for reachability checks
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
    # Don't DROPGUARDS here — Tor recovers naturally and USR2 handles wake.
    # DROPGUARDS throws away guards right when ADD_ONION needs them to publish.
    if "Tor now sees network activity" in line:
        log("Network came back — letting Tor recover naturally")
        return

    # --- Bootstrap progress ---
    if "BOOTSTRAP" in line or "Bootstrapped" in line:
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
    if "PROGRESS=" in line:
        for part in line.split():
            if part.startswith("PROGRESS="):
                try:
                    return int(part.split("=")[1])
                except ValueError:
                    pass
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

    # Periodic heartbeat log (every 5 minutes)
    if now - state.last_heartbeat_log > 300:
        ce = "?"
        resp = send_cmd(cmd_sock, "GETINFO status/circuit-established")
        if "circuit-established=" in resp:
            ce = resp.split("circuit-established=")[1].split()[0].strip()
        log(f"alive — bootstrapped={state.bootstrapped}, "
            f"circuit-established={ce}, services_active={state.services_active}")
        state.last_heartbeat_log = now

    # Active circuit health check — if Tor reports no circuits, recover
    if state.bootstrapped:
        resp = send_cmd(cmd_sock, "GETINFO status/circuit-established")
        if "circuit-established=0" in resp:
            do_dropguards(cmd_sock, state, "circuit-established=0 (circuits lost)")

    # Bootstrap stall
    if (not state.bootstrapped
            and state.last_bootstrap_pct > 0
            and now - state.last_bootstrap_change > BOOTSTRAP_STALL_TIMEOUT
            and now - state.last_dropguards > DROPGUARDS_COOLDOWN):
        do_dropguards(cmd_sock, state,
                      f"bootstrap stalled at {state.last_bootstrap_pct}% for {BOOTSTRAP_STALL_TIMEOUT}s")

    # Descriptor upload stall — HSFETCH if stuck
    if (state.last_recovery_time > 0
            and not state.hs_desc_uploaded_since_recovery
            and now - state.last_recovery_time > HS_DESC_UPLOAD_TIMEOUT
            and state.bootstrapped):
        log(f"Warning: no HS_DESC upload {HS_DESC_UPLOAD_TIMEOUT}s after recovery — flushing descriptor cache")
        if not state.onion_addresses:
            state.onion_addresses = discover_onion_addresses()
        if state.onion_addresses:
            send_cmd(cmd_sock, "SIGNAL NEWNYM")
            for addr in state.onion_addresses:
                resp = send_cmd(cmd_sock, f"HSFETCH {addr}")
                if "250" in resp:
                    log(f"HSFETCH {addr[:16]}... — refreshing descriptor")
        state.last_recovery_time = 0

    # Escalation: DORMANT/ACTIVE if DROPGUARDS didn't work after 2 minutes.
    # Only safe for SOCKS-only containers — DORMANT kills onion services permanently.
    if (os.environ.get("NO_ONION_SERVICE") == "1"
            and state.last_dropguards > 0
            and not state.bootstrapped
            and now - state.last_dropguards > DORMANT_COOLDOWN
            and now - state.last_dormant > DORMANT_COOLDOWN):
        do_dormant_cycle(cmd_sock, state,
                         f"DROPGUARDS didn't recover after {DORMANT_COOLDOWN}s — trying DORMANT/ACTIVE")

    # Last resort: HALT
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
    global _signal_sleep, _signal_wake

    log("Starting tor-watchdog")
    state = WatchdogState()

    # Discover onion services from disk (keys + torrc port mappings)
    state.services = discover_services()
    if state.services:
        log(f"Discovered {len(state.services)} onion service(s): "
            + ", ".join(s["service_name"] for s in state.services))
    else:
        log("No onion services found on disk (SOCKS-only container?)")

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

        # Check current bootstrap status
        resp = send_cmd(cmd_sock, "GETINFO status/bootstrap-phase")
        if "PROGRESS=100" in resp:
            state.bootstrapped = True
            log("Tor already bootstrapped to 100%")
        else:
            pct = _extract_bootstrap_pct(resp)
            if pct is not None:
                state.last_bootstrap_pct = pct
                log(f"Tor bootstrap at {pct}%")

        # ADD_ONION for all services — do it before bootstrap so Tor publishes
        # descriptors as soon as it has circuits (no delay after bootstrap).
        if state.services and not state.services_active:
            n = add_all_services(cmd_sock, state.services)
            state.services_active = n > 0

        log("Connected — monitoring Tor health")
        event_sock.settimeout(5)  # wake up frequently to check signals + stalls
        buf = ""

        while True:
            # Check for USR1 (sleep) signal
            if _signal_sleep:
                _signal_sleep = False
                log("Received USR1 (sleep) — removing onion services")
                if state.services and state.services_active:
                    del_all_services(cmd_sock, state.services)
                    state.services_active = False

            # Check for USR2 (wake) signal
            if _signal_wake:
                _signal_wake = False
                log("Received USR2 (wake) — re-adding onion services")
                state.last_recovery_time = time.time()
                state.hs_desc_uploaded_since_recovery = False
                if state.services:
                    n = add_all_services(cmd_sock, state.services)
                    state.services_active = n > 0

            # Read events
            try:
                data = event_sock.recv(4096)
                if not data:
                    log("Control port connection closed — reconnecting")
                    state.services_active = False
                    break
                buf += data.decode("utf-8", errors="replace")
            except socket.timeout:
                # No events — check for stalls
                check_stalls(cmd_sock, state)

                # If services not yet added (e.g. after reconnect), add them now
                if state.services and not state.services_active:
                    n = add_all_services(cmd_sock, state.services)
                    state.services_active = n > 0

                continue
            except (ConnectionResetError, BrokenPipeError, OSError):
                log("Control port connection lost — reconnecting")
                state.services_active = False
                break

            # Process complete lines
            while "\r\n" in buf:
                line, buf = buf.split("\r\n", 1)
                if not line:
                    continue
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

    # Install signal handlers
    signal.signal(signal.SIGUSR1, _handle_usr1)
    signal.signal(signal.SIGUSR2, _handle_usr2)

    try:
        run()
    except KeyboardInterrupt:
        log("Shutting down")
    except Exception as e:
        log(f"Fatal error: {e}")
        sys.exit(1)
