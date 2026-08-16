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
DROPGUARDS_COOLDOWN = 300
DORMANT_COOLDOWN = 120       # 2 min after DROPGUARDS → try DORMANT/ACTIVE
HALT_COOLDOWN = 300  # 5 minutes — last resort

# Detection thresholds
FAILED_NODE_THRESHOLD = 5       # failures within window → DROPGUARDS
FAILED_NODE_WINDOW = 60         # seconds
BOOTSTRAP_STALL_TIMEOUT = 120   # no progress for 2 min → DROPGUARDS
HS_DESC_UPLOAD_TIMEOUT = 60     # no descriptor upload 60s after recovery → DEL+ADD
# Reconnect delay when control port isn't available yet
CONNECT_RETRY_DELAY = 5

# Hidden service key paths
HS_BASE_DIR = "/var/lib/tor/hidden_service"

# ---------------------------------------------------------------------------
# End-to-end reachability probe
# ---------------------------------------------------------------------------
# The 2026-08-16 incident: the onion was end-to-end dead for ~9 hours while
# every local signal read green — bootstrapped=100%, circuit-established=1,
# ADD_ONION accepted, HS_DESC UPLOADED delivered — because none of those is a
# fetch. The probe below is the missing measurement: a real Tor-routed
# self-fetch of our own onion through the local SOCKS port. C Tor has no
# self-shortcut (client and service subsystems are disjoint; the rendezvous
# point is always a remote relay), so a SOCKS self-fetch is a genuine full
# rendezvous. Everything is env-tunable so tests and the live drill
# (E2E_PROBE_ADDRESS_OVERRIDE pointed at a nonexistent address) can shrink
# the timings without patching code.


def _env_int(name, default):
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


E2E_PROBE_INTERVAL_OK = _env_int("E2E_PROBE_INTERVAL_OK", 180)
E2E_PROBE_INTERVAL_BAD = _env_int("E2E_PROBE_INTERVAL_BAD", 60)
# Cold rendezvous through obfs4+Veee measured 8-16s; 30s timeouts were the
# incident's failure signature, so 45 avoids flapping on slow-but-alive.
E2E_PROBE_TIMEOUT = _env_int("E2E_PROBE_TIMEOUT", 45)
E2E_FAIL_THRESHOLD = _env_int("E2E_FAIL_THRESHOLD", 3)

SOCKS_HOST = "127.0.0.1"
SOCKS_PORT = 9050

# Control onions for the failure-attribution fetch: if our tor cannot reach
# ANY onion, the transport path is dead and HS-layer rungs are pointless.
# Primary is the OnionHeaven hub (same default as health.py); fallback is
# DuckDuckGo's v3 onion, which outlives our own infrastructure.
CONTROL_ONIONS = (
    os.environ.get("E2E_CONTROL_ONION")
    or "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion",
    "duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion",
)

# ---------------------------------------------------------------------------
# The escalation ladder
# ---------------------------------------------------------------------------
# Everything above recovers Tor *within* its current process. That is not
# enough: a host sleep can leave the pluggable transport wedged — snowflake
# stops answering entirely, no reconnect attempts, no errors — and Tor has no
# way to notice or fix that from the inside. Measured 2026-08-08: after a Mac
# idle+clamshell sleep the onion went dark and stayed dark until someone ran
# `docker restart` by hand, ~20 minutes later.
#
# The old ladder could never reach past DROPGUARDS in that state, because every
# rung above it was gated on `not state.bootstrapped` — and after a sleep Tor
# keeps reporting bootstrapped=100% (stale) while circuit-established=0. So the
# ladder now hangs off SERVING, not off bootstrapped: circuits up, descriptor
# published, service attached. That is the thing the reader actually needs.
#
# Timings come from what the code already waits for. The wake path gives
# circuits 120s, and a descriptor gets HS_DESC_UPLOAD_TIMEOUT (60s) to land, so
# 180s is the earliest moment at which "OnionPress tried and failed" is an
# honest statement rather than an impatient one.
PT_RESTART_AFTER = 180        # 120s circuit wait + 60s descriptor window
PT_RESTART_COOLDOWN = 300
# 4 more minutes: a fresh snowflake rendezvous plus a bootstrap through it ran
# 1-3 min in the captured logs, so anything tighter restarts a transport that
# was about to work.
TOR_RESTART_AFTER = 420
TOR_RESTART_COOLDOWN = 900    # at most 4 process restarts an hour
# Fighting a genuinely offline network is worse than sitting still: it burns
# battery, and it guarantees we are mid-bootstrap rather than connected at the
# moment the network returns. After this many restarts that changed nothing, we
# stop climbing and just say so.
DEGRADED_AFTER_RESTARTS = 3
DEGRADED_WINDOW = 3600

# Where the ladder publishes what it knows, for moss to read (the shared
# volume the content address already lives on). Never sit silent: if we have
# stopped trying, the file says so.
STATE_FILE = "/var/lib/onionpress/watchdog-state.json"

# Managed pluggable transports we know how to restart. Tor launches these as
# child processes and does NOT relaunch one that dies or wedges.
PT_BINARIES = ("snowflake-client", "obfs4proxy")

# A wake that arrives while we are still handling the previous one is the same
# wake. The host sends several (observed: 3 for one lid-open).
WAKE_DEBOUNCE = 60


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


def _derive_onion_address(public_key_path):
    """Derive .onion address from hs_ed25519_public_key file.

    v3 onion address = base32(pubkey + checksum + version)
    checksum = SHA3-256(".onion checksum" + pubkey + version)[:2]
    """
    import hashlib
    with open(public_key_path, "rb") as f:
        data = f.read()
    if len(data) != 64:
        raise ValueError(f"Public key wrong size: {len(data)} (expected 64)")
    pubkey = data[32:]  # 32-byte header + 32-byte key
    version = b'\x03'
    checksum = hashlib.sha3_256(b".onion checksum" + pubkey + version).digest()[:2]
    addr_bytes = pubkey + checksum + version
    return base64.b32encode(addr_bytes).decode("ascii").lower() + ".onion"


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

        # Read hostname for service_id (or derive from public key)
        hostname_file = os.path.join(service_dir, "hostname")
        public_key_file = os.path.join(service_dir, "hs_ed25519_public_key")
        service_id = None
        try:
            with open(hostname_file) as f:
                hostname = f.read().strip()
            service_id = hostname.replace(".onion", "")
        except OSError:
            # No hostname file — derive from public key (fresh install)
            try:
                hostname = _derive_onion_address(public_key_file)
                # Write it so we don't have to derive again
                with open(hostname_file, "w") as f:
                    f.write(hostname + "\n")
                log(f"Derived hostname for {name}: {hostname}")
                service_id = hostname.replace(".onion", "")
            except (OSError, ValueError):
                # No hostname or public key — will generate with NEW:BEST
                pass

        # Read key — if missing, flag for NEW:BEST generation
        secret_key_file = os.path.join(service_dir, "hs_ed25519_secret_key")
        key_b64 = None
        try:
            key_b64 = _read_ed25519_key(secret_key_file)
        except (OSError, ValueError) as e:
            if service_id:
                # This service HAS an address, and it is one the user has
                # published — printed on a card, sent to readers, registered in
                # the name directory. Minting a fresh one because a key read
                # failed for a moment would be worse than not serving at all:
                # the old address is what people type, and it would be gone.
                # A transient unreadable key is a bad disk or a mount that
                # isn't up yet, not permission to become a different site.
                log(f"REFUSING to mint a new address for {name}: it already "
                    f"publishes {service_id[:16]}... but its key is unreadable "
                    f"({e}). Fix the key; NEW:BEST would change the address.")
                services.append({
                    "service_id": service_id,
                    "service_name": name,
                    "key_b64": None,
                    "key_unreadable": True,
                    "ports": ports,
                    "service_dir": service_dir,
                })
                continue
            log(f"No key for {name} — will generate with NEW:BEST")

        services.append({
            "service_id": service_id if key_b64 else None,
            "service_name": name,
            "key_b64": key_b64,
            "key_unreadable": False,
            "ports": ports,
            "service_dir": service_dir,
        })

    return services


def _save_generated_key(service_dir, resp_lines):
    """Save key and hostname returned by ADD_ONION NEW:BEST to disk."""
    service_id = None
    key_b64 = None
    for line in resp_lines.splitlines():
        if line.startswith("250-ServiceID="):
            service_id = line.split("=", 1)[1]
        elif line.startswith("250-PrivateKey=ED25519-V3:"):
            key_b64 = line.split(":", 1)[1]
    if not service_id or not key_b64:
        return None
    try:
        os.makedirs(service_dir, exist_ok=True)
        # Write hostname
        with open(os.path.join(service_dir, "hostname"), "w") as f:
            f.write(service_id + ".onion\n")
        # Write secret key in C Tor format (32-byte header + 64-byte key)
        key_bytes = base64.b64decode(key_b64)
        header = b"== ed25519v1-secret: type0 ==\x00\x00\x00"
        with open(os.path.join(service_dir, "hs_ed25519_secret_key"), "wb") as f:
            f.write(header + key_bytes)
        log(f"Saved generated key for {service_id[:16]}... to {service_dir}")
    except OSError as e:
        log(f"Warning: failed to save generated key: {e}")
    return service_id


def add_all_services(cmd_sock, services):
    """ADD_ONION for all services. Returns (successes, collisions)."""
    count = 0
    collisions = 0
    for svc in services:
        port_args = " ".join(f"Port={p}" for p in svc["ports"])
        # An address we already publish, whose key we could not read. Skipping
        # is the only safe move: NEW:BEST here would silently replace the
        # user's published address. See `discover_services`.
        if svc.get("key_unreadable"):
            log(f"ADD_ONION {svc['service_name']} — SKIPPED: key unreadable, "
                f"refusing to mint a replacement address")
            continue
        if svc.get("key_b64"):
            cmd = f"ADD_ONION ED25519-V3:{svc['key_b64']} Flags=Detach {port_args}"
        else:
            cmd = f"ADD_ONION NEW:BEST Flags=Detach {port_args}"
        resp = send_cmd(cmd_sock, cmd)
        if "250" in resp:
            if svc.get("key_b64"):
                log(f"ADD_ONION {svc['service_name']} ({svc['service_id'][:16]}...) — ok")
            else:
                # Save the generated key to disk for future restarts
                new_id = _save_generated_key(svc.get("service_dir", ""), resp)
                svc["service_id"] = new_id
                log(f"ADD_ONION {svc['service_name']} (generated {new_id[:16] if new_id else '?'}...) — ok")
            count += 1
        elif "Onion address collision" in resp:
            log(f"ADD_ONION {svc['service_name']} — already active (collision)")
            collisions += 1
        else:
            log(f"ADD_ONION {svc['service_name']} — FAILED: {resp.strip()[:100]}")
    return count, collisions


def del_all_services(cmd_sock, services):
    """DEL_ONION for all services. Returns number of successes."""
    count = 0
    for svc in services:
        if not svc.get("service_id"):
            continue
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
    # Immediate signal-receipt evidence — separate from the
    # "Received USR1 (sleep) — removing onion services" line in the
    # main loop, which only prints after the DEL_ONION work runs.
    # Without this, a system-sleep race where suspend lands before
    # the main loop wakes leaves no record that USR1 arrived at all.
    log("SIGUSR1 received (sleep)")


def _handle_usr2(signum, frame):
    global _signal_wake
    _signal_wake = True
    log("SIGUSR2 received (wake)")


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
        self.last_recovery_trigger = ""  # e.g. "wake", "clock-skew", "circuits lost"
        self.hs_desc_uploaded_since_recovery = False
        self.hs_desc_upload_started_since_recovery = False
        self.hs_desc_upload_failed_since_recovery = False
        self.hs_desc_last_failed_reason = ""
        self.onion_addresses = []  # for HSFETCH
        self.services = []  # discovered onion services
        self.services_active = False  # True when ADD_ONION has been done
        self.sleeping = False  # True between USR1 (sleep) and USR2 (wake)
        # --- escalation ladder ---
        self.not_serving_since = 0   # 0 = serving (or not yet judged)
        self.last_pt_restart = 0
        self.tor_restarts = []       # timestamps, for the degraded window
        self.degraded = False
        self.degraded_reason = ""
        self.last_wake_started = 0   # wake debounce
        self.last_state_write = 0
        # --- end-to-end probe (the measured serving input, 2026-08-16) ---
        self.e2e_ok = None           # tri-state: None until the first probe completes
        self.e2e_code = ""
        self.e2e_last_ms = 0
        self.e2e_verdict = ""        # "", "network", "descriptor", "intro-wedge", "takeover"
        self.e2e_checked_at = 0
        self.e2e_fail_streak = 0
        self.last_e2e_probe = 0
        self.last_probe_takeover = False
        # ≥1 e2e_ok=True observed since the last restart action — the only
        # evidence that may clear the restart ledger.
        self.e2e_confirmed_since_restart = False
        self.awaiting_e2e_logged = False
        # HSFETCH verdict plumbing (answered async via HS_DESC events)
        self.hsfetch_pending = ""    # address id awaiting RECEIVED/FAILED
        self.hsfetch_sent_at = 0
        self.hsfetch_received = False
        self.hsfetch_failed_reason = ""
        # per-outage accounting + host handoff
        self.restarts_this_outage = 0
        self.reclaim_republished = False
        self.hs_rebuilt_this_outage = False
        self.escalate_to_host = False
        self.handoff_reason = ""


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
def _hsfetch_missing_descriptors(cmd_sock, state):
    """Check client descriptor cache and HSFETCH any missing addresses.

    Called right after bootstrap hits 100%. Queries the control port
    (read-only) for each address we care about, and only HSFETCHes
    the ones that aren't cached. Safe on cold start — no competing
    descriptors to poison the cache.
    """
    if not state.onion_addresses:
        state.onion_addresses = discover_onion_addresses()
    if not state.onion_addresses:
        return
    for addr in state.onion_addresses:
        resp = send_cmd(cmd_sock, f"GETINFO hs/client/desc/id/{addr}")
        if "hs-descriptor" in resp:
            continue  # already cached
        resp = send_cmd(cmd_sock, f"HSFETCH {addr}")
        if "250" in resp:
            log(f"HSFETCH {addr[:16]}... — descriptor not cached, fetching")


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


# ---------------------------------------------------------------------------
# End-to-end self-fetch (SOCKS5h, pure stdlib)
# ---------------------------------------------------------------------------
def _recv_exact(s, n):
    """Read exactly n bytes or as many as arrive before EOF."""
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def socks5h_fetch(host, socks_host=SOCKS_HOST, socks_port=SOCKS_PORT,
                  timeout=None, port=80):
    """GET http://<host>/ through the local SOCKS5 proxy, hostname mode.

    Pure stdlib — the watchdog's no-external-deps rule. The hostname goes to
    the proxy verbatim (SOCKS5h): tor resolves .onion, we never touch DNS.
    For an onion destination, the wait for the CONNECT reply IS the
    rendezvous — tor answers only once the full client-side circuit is up.

    Returns a dict:
      ok        True only for a 200/301 without the takeover marker
      responded an HTTP status line arrived (transport + HS path both work)
      code      "200", "302", ... or "timeout"/"refused"/"socks-<n>"/"error:..."
      stage     where it failed: connect / socks-handshake / rendezvous / http,
                "" once a response was parsed
      takeover  X-OnionHeaven-Takeover: 1 header seen
      ms        elapsed milliseconds
    """
    timeout = E2E_PROBE_TIMEOUT if timeout is None else timeout
    start = time.time()
    res = {"ok": False, "responded": False, "code": "", "stage": "",
           "takeover": False, "ms": 0}
    stage = "connect"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((socks_host, socks_port))

        stage = "socks-handshake"
        s.sendall(b"\x05\x01\x00")  # SOCKS5, one method: no auth
        reply = _recv_exact(s, 2)
        if reply != b"\x05\x00":
            res.update(stage=stage, code="handshake")
            return res

        stage = "rendezvous"
        hostb = host.encode("idna") if not host.isascii() else host.encode()
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(hostb)]) + hostb
                  + port.to_bytes(2, "big"))
        reply = _recv_exact(s, 4)
        if len(reply) < 4:
            res.update(stage=stage, code="eof")
            return res
        if reply[1] != 0:
            res.update(stage=stage, code=f"socks-{reply[1]}")
            return res
        atyp = reply[3]
        if atyp == 1:
            _recv_exact(s, 4 + 2)
        elif atyp == 3:
            alen = _recv_exact(s, 1)
            _recv_exact(s, (alen[0] if alen else 0) + 2)
        elif atyp == 4:
            _recv_exact(s, 16 + 2)

        stage = "http"
        s.sendall(f"GET / HTTP/1.0\r\nHost: {host}\r\n"
                  f"User-Agent: OnionPress-Watchdog\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        text = buf.decode("utf-8", errors="replace")
        lines = text.split("\r\n")
        parts = lines[0].split(None, 2) if lines else []
        if len(parts) < 2 or not parts[0].startswith("HTTP/"):
            res.update(stage=stage, code="badresponse")
            return res
        code = parts[1]
        takeover = False
        for line in lines[1:]:
            if not line:
                break  # end of headers
            name, _, value = line.partition(":")
            if (name.strip().lower() == "x-onionheaven-takeover"
                    and value.strip() == "1"):
                takeover = True
        res.update(responded=True, code=code, takeover=takeover, stage="",
                   ok=(code in ("200", "301") and not takeover))
        return res
    except socket.timeout:
        res.update(stage=stage, code="timeout")
        return res
    except ConnectionRefusedError:
        res.update(stage=stage, code="refused")
        return res
    except OSError as e:
        res.update(stage=stage, code=f"error:{e.__class__.__name__}")
        return res
    finally:
        res["ms"] = int((time.time() - start) * 1000)
        try:
            s.close()
        except OSError:
            pass


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
    state.last_recovery_trigger = reason
    state.hs_desc_uploaded_since_recovery = False
    state.hs_desc_upload_started_since_recovery = False
    state.hs_desc_upload_failed_since_recovery = False
    state.hs_desc_last_failed_reason = ""
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


# ---------------------------------------------------------------------------
# Escalation ladder
# ---------------------------------------------------------------------------
def local_signals_green(state, circuit_established):
    """The four in-process signals — Tor's own belief, nothing end-to-end.

    bootstrapped + circuit-established + services attached + descriptor
    upload landed. On 2026-08-16 all four held green for ~9 hours while the
    onion was end-to-end dead, so this is deliberately NOT the serving
    verdict anymore — it gates the probe and feeds is_serving.
    """
    if not state.bootstrapped or not circuit_established:
        return False
    if not state.services:
        return True  # SOCKS-only container: circuits ARE the service
    if not state.services_active:
        return False
    # A publication window is armed and hasn't landed → the descriptor is not
    # out there, so nobody can reach us regardless of how healthy Tor looks.
    if state.last_recovery_time > 0 and not state.hs_desc_uploaded_since_recovery:
        return False
    return True


def is_serving(state, circuit_established):
    """Is the site actually reachable-in-principle right now?

    Not "is Tor alive" — a Tor that is bootstrapped, has no circuits and has
    published no descriptor is alive and useless. And not "does Tor believe
    it's fine" either: the serving verdict is local signals AND the measured
    end-to-end result. e2e_ok is tri-state — None (no completed probe yet)
    never blocks serving before evidence exists; False (E2E_FAIL_THRESHOLD
    consecutive failed self-fetches) does.
    """
    if not local_signals_green(state, circuit_established):
        return False
    return state.e2e_ok is not False


def e2e_probe_address(state):
    """The address the end-to-end probe fetches, with the .onion suffix.

    E2E_PROBE_ADDRESS_OVERRIDE wins (read per call, so the live drill can
    point the probe at a nonexistent-but-valid address while the stack stays
    genuinely healthy — reproducing the incident's signal pattern exactly).
    Otherwise the wordpress service — the one with readers — else the first
    service that has an address at all.
    """
    override = os.environ.get("E2E_PROBE_ADDRESS_OVERRIDE", "").strip()
    if override:
        return override if override.endswith(".onion") else override + ".onion"
    fallback = ""
    for svc in state.services:
        if not svc.get("service_id"):
            continue
        if svc.get("service_name") == "wordpress":
            return svc["service_id"] + ".onion"
        fallback = fallback or svc["service_id"] + ".onion"
    return fallback


def maybe_e2e_probe(cmd_sock, state, circuits_up, now=None, fetch=None):
    """Run the end-to-end self-fetch when due. Returns True if a probe ran.

    Gates: services discovered and attached, not sleeping, local signals
    green — when they are already red the existing ladder is running and the
    probe adds nothing but load. Declare rules are asymmetric on purpose:
    E2E_FAIL_THRESHOLD consecutive failures spanning minutes to declare
    down, one verified success to declare up — a 200 through a real
    rendezvous is conclusive.

    Blocks the pass for up to E2E_PROBE_TIMEOUT (plus the verdict fetches
    when down) — same order as the wake handler's existing 120s wait.
    """
    now = now or time.time()
    if not state.services or not state.services_active or state.sleeping:
        return False
    if not local_signals_green(state, circuits_up):
        return False
    bad = state.e2e_ok is False or state.e2e_fail_streak > 0
    interval = E2E_PROBE_INTERVAL_BAD if bad else E2E_PROBE_INTERVAL_OK
    if state.last_e2e_probe and now - state.last_e2e_probe < interval:
        return False
    address = e2e_probe_address(state)
    if not address:
        return False
    state.last_e2e_probe = now
    fetch = fetch or socks5h_fetch
    res = fetch(address)
    state.e2e_checked_at = now
    state.e2e_code = res.get("code", "")
    state.e2e_last_ms = res.get("ms", 0)
    if res.get("ok"):
        state.e2e_ok = True
        state.e2e_fail_streak = 0
        state.e2e_confirmed_since_restart = True
        state.e2e_verdict = ""
        state.last_probe_takeover = False
        log(f"e2e-probe ok=1 code={res.get('code')} ms={res.get('ms')}")
    else:
        state.e2e_fail_streak += 1
        state.last_probe_takeover = (bool(res.get("takeover"))
                                     or res.get("code") == "302")
        log(f"e2e-probe ok=0 stage={res.get('stage') or 'response'} "
            f"code={res.get('code')} "
            f"streak={state.e2e_fail_streak}/{E2E_FAIL_THRESHOLD}")
        if state.e2e_fail_streak >= E2E_FAIL_THRESHOLD:
            state.e2e_ok = False
            classify_down(cmd_sock, state, fetch=fetch)
    return True


def rebuild_services(cmd_sock, state, trigger, now=None):
    """DEL+ADD every service: fresh intro circuits + descriptor republish.

    The cheapest service-side rebuild, one rung below a tor restart — the
    accepted remediation for the tor#19522/#8864 class where C Tor holds
    'established' intro circuits nothing can traverse (SIGHUP provably
    moves them across a reload; DEL_ONION+ADD_ONION destroys them). The
    NEWNYM afterwards busts our own client-side descriptor cache so the
    next probe fetches the fresh descriptor instead of rendezvousing on
    the stale one — the 2026-08-14 reclaim lesson. Returns the number of
    services attached afterwards; re-arms the publish monitor on success.
    """
    now = now or time.time()
    del_all_services(cmd_sock, state.services)
    added, collisions = add_all_services(cmd_sock, state.services)
    count = added + collisions
    if count > 0:
        state.services_active = True
        state.last_recovery_time = now
        state.last_recovery_trigger = trigger
        state.hs_desc_uploaded_since_recovery = False
        state.hs_desc_upload_started_since_recovery = False
        state.hs_desc_upload_failed_since_recovery = False
        state.hs_desc_last_failed_reason = ""
    resp = send_cmd(cmd_sock, "SIGNAL NEWNYM")
    if "250" in resp:
        log("Sent SIGNAL NEWNYM — dropping the pre-republish descriptor "
            "from the client cache")
    # Whatever HSFETCH judged is about a descriptor that no longer exists.
    state.hsfetch_pending = ""
    state.hsfetch_received = False
    state.hsfetch_failed_reason = ""
    return count


_VERDICT_DETAIL = {
    "network": "control-onion fetch failed — transport path dead, HS rungs pointless",
    "descriptor": "descriptor not fetchable from the DHT",
    "intro-wedge": "descriptor fetchable but rendezvous fails — dead intro circuits",
    "takeover": "response carries the takeover marker — hub is serving our address",
}


# A pending HSFETCH whose HS_DESC answer never arrived (control-connection
# churn) is re-issued after this long, so the verdict cannot wedge at
# "undetermined" forever.
HSFETCH_RETRY_AFTER = 120


def classify_down(cmd_sock, state, fetch=None, now=None):
    """Attribute a confirmed end-to-end failure (§3.1's verdict taxonomy).

    Ordered discriminators. A response carrying the takeover marker means
    the network path works and someone (our hub) answers for our address —
    restarting would fight the reclaim. A dead control onion means OUR tor
    reaches no onion at all: transport/tunnel death, where HS-layer rungs
    are pointless. Otherwise HSFETCH our own address — answered
    asynchronously via HS_DESC events between passes — splits
    descriptor-not-in-the-DHT from the tor#19522/#8864 intro-wedge class
    (descriptor uploads fine, its intro points are dead).

    Returns the verdict; "" while HSFETCH evidence is still in flight.
    Called at the declare-down transition and then once per bad-interval
    pass, so a verdict can refine or flip as the outage evolves.
    """
    fetch = fetch or socks5h_fetch
    now = now or time.time()
    verdict = ""
    if state.last_probe_takeover:
        verdict = "takeover"
    else:
        control_ok = False
        for ctrl in CONTROL_ONIONS:
            if fetch(ctrl).get("responded"):
                control_ok = True
                break
        if not control_ok:
            verdict = "network"
        elif state.hsfetch_failed_reason:
            verdict = "descriptor"
        elif state.hsfetch_received:
            verdict = "intro-wedge"
        elif (not state.hsfetch_pending
                or (state.hsfetch_sent_at
                    and now - state.hsfetch_sent_at > HSFETCH_RETRY_AFTER)):
            addr_id = e2e_probe_address(state).replace(".onion", "")
            if addr_id:
                state.hsfetch_pending = addr_id
                state.hsfetch_sent_at = now
                state.hsfetch_received = False
                state.hsfetch_failed_reason = ""
                send_cmd(cmd_sock, f"HSFETCH {addr_id}")
    if verdict and verdict != state.e2e_verdict:
        detail = _VERDICT_DETAIL.get(verdict, "")
        if verdict == "descriptor" and state.hsfetch_failed_reason:
            detail += f" (HSFETCH reason={state.hsfetch_failed_reason})"
        log(f"verdict={verdict} ({detail})")
    if verdict:
        state.e2e_verdict = verdict
    return verdict


def update_serving_ledger(state, serving, now):
    """What a serving verdict may clear — the 11-restart bug's fix.

    During the incident every post-restart false local-green wiped
    tor_restarts, so DEGRADED took 9 hours to latch instead of 3 restarts.
    The ledger now clears only on *probe-confirmed* serving: at least one
    e2e_ok=True observed since the last restart action. Local green alone —
    including the tri-state serving=True before the first probe — logs and
    clears nothing. Containers with no services have no probe and keep the
    trust-based clear: circuits are all the evidence that exists there.
    """
    if serving:
        confirmed = state.e2e_confirmed_since_restart or not state.services
        if not confirmed:
            if not state.awaiting_e2e_logged:
                log("locally green, awaiting end-to-end confirmation — "
                    "restart ledger kept until a probe proves it")
                state.awaiting_e2e_logged = True
            return
        if state.not_serving_since:
            down_for = int(now - state.not_serving_since)
            if state.services:
                secs = state.e2e_last_ms / 1000.0
                log(f"serving CONFIRMED end-to-end ({state.e2e_code} in "
                    f"{secs:.1f}s) after {down_for}s down")
            else:
                log(f"Serving again after {down_for}s")
        state.not_serving_since = 0
        # A recovered stack is not a degraded one. Clearing here (rather than
        # never) is what lets the ladder work again after the network returns.
        state.degraded = False
        state.degraded_reason = ""
        state.tor_restarts = []
        state.restarts_this_outage = 0
        state.escalate_to_host = False
        state.handoff_reason = ""
        state.reclaim_republished = False
        state.hs_rebuilt_this_outage = False
        state.awaiting_e2e_logged = False
    else:
        state.awaiting_e2e_logged = False
        if not state.not_serving_since:
            state.not_serving_since = now


def next_escalation(state, now, has_transport):
    """Which rung is due, or None. Pure — the whole ladder in one place.

    Returns one of: None, "reclaim", "rebuild-hs", "restart-pt",
    "restart-tor", "degraded". DROPGUARDS is not here: it is event-driven
    (see `process_event`) and fires long before this does.

    The e2e verdict directs the ladder (§3.2): takeover means our hub is
    serving our readers — restarting fights the reclaim, so the only action
    is one republish to race the takeover descriptor, then patience;
    descriptor/intro-wedge get the cheap DEL+ADD rebuild before any
    restart; network skips the rebuild because no HS-layer action can fix
    a dead transport.
    """
    if state.not_serving_since <= 0:
        return None
    down_for = now - state.not_serving_since

    if state.degraded:
        return None  # stopped climbing on purpose; the state file says so

    if state.e2e_verdict == "takeover":
        if state.services and not state.reclaim_republished:
            return "reclaim"
        return None

    # Enough restarts that changed nothing → stop. Two counters on purpose:
    # the sliding window caps total churn, and the per-outage counter (which
    # survives the restarts it counts) catches the incident's shape — spaced
    # restarts that individually slid out of the window for 4.5 hours.
    recent = [t for t in state.tor_restarts if now - t < DEGRADED_WINDOW]
    if (len(recent) >= DEGRADED_AFTER_RESTARTS
            or state.restarts_this_outage >= DEGRADED_AFTER_RESTARTS):
        return "degraded"

    if (state.e2e_verdict in ("descriptor", "intro-wedge")
            and state.services
            and not state.hs_rebuilt_this_outage):
        return "rebuild-hs"

    if (down_for >= TOR_RESTART_AFTER
            and now - _last_or_zero(state.tor_restarts) >= TOR_RESTART_COOLDOWN):
        return "restart-tor"

    # Never climb back DOWN. Restarting Tor already re-execs the transport, so
    # once this outage has had a process restart, a transport restart is
    # strictly less than what we just did — and offering it again is how a
    # cooling-off period turns into a busier loop than the rung it replaced.
    if any(t >= state.not_serving_since for t in state.tor_restarts):
        return None

    if (has_transport
            and down_for >= PT_RESTART_AFTER
            and now - state.last_pt_restart >= PT_RESTART_COOLDOWN):
        return "restart-pt"

    return None


def _last_or_zero(stamps):
    return stamps[-1] if stamps else 0


def configured_transports(torrc_path="/etc/tor/torrc"):
    """Managed transports this Tor was told to launch, from its own torrc."""
    found = []
    try:
        with open(torrc_path) as f:
            for line in f:
                if line.startswith("ClientTransportPlugin "):
                    parts = line.split()
                    if len(parts) >= 2:
                        found.append(parts[1])
    except OSError:
        pass
    return found


def _pt_pids(proc_root="/proc"):
    """PIDs of managed-transport child processes, by scanning /proc.

    Deliberately not `pkill`: procps is not guaranteed in the image, and a
    dependency the recovery path needs is a dependency that can fail exactly
    when recovery matters.
    """
    pids = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, entry, "cmdline"), "rb") as f:
                cmdline = f.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        if any(binary in cmdline for binary in PT_BINARIES):
            pids.append(int(entry))
    return pids


def do_restart_pt(cmd_sock, state, reason, kill=None, pids=None, now=None):
    """Rung 2: kill the wedged transport and make Tor launch a fresh one.

    This is the rung that was missing, and it is the one that matters: a
    snowflake-client that has stopped answering is invisible to Tor. Tor does
    not supervise a managed proxy back to life — it only launches proxies it is
    missing, at config-load time. So: kill it, then RELOAD so Tor notices the
    gap and re-execs it.

    Nothing here touches keys. The onion address is whatever ADD_ONION is given
    from disk, and this rung sends no ADD_ONION at all.
    """
    now = now or time.time()
    kill = kill or os.kill
    # A restart action voids prior end-to-end confirmation: whatever comes
    # up next must prove itself again before the ledger may clear.
    state.e2e_confirmed_since_restart = False
    found = _pt_pids() if pids is None else pids
    if not found:
        log(f"Escalating: {reason} — no transport process found to restart")
        state.last_pt_restart = now
        return False

    for pid in found:
        try:
            kill(pid, signal.SIGTERM)
            log(f"Escalating: {reason} — killed transport pid {pid}")
        except (OSError, ProcessLookupError) as e:
            log(f"Could not kill transport pid {pid}: {e}")

    resp = send_cmd(cmd_sock, "SIGNAL RELOAD")
    if "250" in resp:
        log("Sent SIGNAL RELOAD — Tor relaunching the pluggable transport")
    else:
        log(f"SIGNAL RELOAD failed: {resp.strip()[:100]}")
    state.last_pt_restart = now
    return True


def do_halt(cmd_sock, state, reason, now=None):
    """Rung 3: tell Tor to shut down. Docker's restart policy brings it back.

    Heavier than rung 2 and strictly more thorough: the restart re-execs the
    transport from scratch along with Tor, which is what a hand-run
    `docker restart onionpress-tor` did for the user.

    The address survives this the same way it survives a normal container
    restart — the keys are on the mounted volume, and the entrypoint hands them
    back to `discover_services` on the way up. Nothing here writes a key.
    """
    now = now or time.time()
    if now - state.last_halt < HALT_COOLDOWN:
        return

    log(f"LAST RESORT: {reason} — sending SIGNAL HALT")
    send_cmd(cmd_sock, "SIGNAL HALT")
    state.last_halt = now
    state.tor_restarts.append(now)
    state.restarts_this_outage += 1
    state.e2e_confirmed_since_restart = False
    # Persist the stamp on this very pass — the container dies with us, and
    # a stamp that only lived in memory is how the counter used to reset on
    # exactly the event it was counting.
    state.last_state_write = 0


def do_degrade(state, reason):
    """Rung 4: stop climbing and hand the outage to the actor above us.

    Restarting into a network that is simply gone is worse than waiting: it
    burns the user's battery and guarantees we are mid-bootstrap, rather than
    connected, at the moment the network comes back. But this is no longer a
    dead end — the incident's fix lives host-side (`launcher restart`
    recreates containers, networks, and the tunnel binding; 22 seconds
    against our 9 hours), so the state file now carries an explicit
    escalation request for the host supervisor. We stay quiescent until
    either a probe-confirmed serving clears the ledger or a fresh container
    start gives the situation a new chance.
    """
    if state.degraded:
        return
    state.degraded = True
    state.degraded_reason = reason
    state.escalate_to_host = True
    state.handoff_reason = reason
    log(f"HANDOFF: {reason} — no longer escalating; requesting host "
        f"escalation (verdict={state.e2e_verdict or 'undetermined'})")


def write_state_file(state, circuit_established, path=None, now=None):
    """Publish what the ladder knows. Atomic; failure here is never fatal."""
    now = now or time.time()
    path = path or STATE_FILE
    payload = {
        "serving": is_serving(state, circuit_established),
        "bootstrapped": state.bootstrapped,
        "bootstrap_pct": state.last_bootstrap_pct,
        "circuit_established": bool(circuit_established),
        "services_active": state.services_active,
        "descriptor_published": (
            state.last_recovery_time == 0 or state.hs_desc_uploaded_since_recovery
        ),
        "not_serving_since": state.not_serving_since or None,
        "degraded": state.degraded,
        "degraded_reason": state.degraded_reason,
        "tor_restarts_recent": len([t for t in state.tor_restarts if now - t < DEGRADED_WINDOW]),
        "updated_at": int(now),
        # --- end-to-end evidence + handoff (§3.3) ---
        "e2e_ok": state.e2e_ok,
        "e2e_code": state.e2e_code,
        "e2e_verdict": state.e2e_verdict,
        "e2e_checked_at": int(state.e2e_checked_at) if state.e2e_checked_at else None,
        "e2e_fail_streak": state.e2e_fail_streak,
        "restarts_this_outage": state.restarts_this_outage,
        "escalate_to_host": state.escalate_to_host,
        "handoff_reason": state.handoff_reason,
    }
    payload["tor_restart_stamps"] = [
        int(t) for t in state.tor_restarts if now - t < DEGRADED_WINDOW
    ]
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except OSError as e:
        log(f"Could not write state file: {e}")


def load_restart_history(state, path=None, now=None):
    """Recover the restart count from the last run.

    Rung 3 is `SIGNAL HALT`, which ends the container — and takes this process
    with it. So a counter that lives only in memory resets on exactly the event
    it is counting, and rung 4 could never be reached: the ladder would restart
    Tor forever, every 15 minutes, against a network that is simply gone.
    The state file is on the shared volume, so it outlives the restart.

    Restored: the windowed stamps, and — when the previous instance died
    mid-outage — the outage start and its per-outage restart count. The
    sliding window alone missed the incident (restarts ~45min apart never
    accumulated 3 in one hour); the per-outage counter only works if it
    survives the restarts it counts.

    The `degraded` FLAG and `escalate_to_host` are deliberately not restored.
    Coming back up is a fresh chance to serve; if the situation is still
    hopeless the counts say so again within one pass — the designed
    yield-back to a host that just restarted us — and if it isn't, we are
    already working.
    """
    now = now or time.time()
    path = path or STATE_FILE
    try:
        with open(path) as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return
    stamps = payload.get("tor_restart_stamps") or []
    state.tor_restarts = [t for t in stamps if now - t < DEGRADED_WINDOW]
    if state.tor_restarts:
        log(f"Resuming with {len(state.tor_restarts)} Tor restart(s) "
            f"in the last {DEGRADED_WINDOW // 60}min")
    not_serving_since = payload.get("not_serving_since") or 0
    if not_serving_since and not_serving_since <= now:
        state.not_serving_since = not_serving_since
        try:
            state.restarts_this_outage = int(payload.get("restarts_this_outage") or 0)
        except (TypeError, ValueError):
            state.restarts_this_outage = 0
        log(f"Resuming mid-outage: not serving since {int(now - not_serving_since)}s "
            f"ago, {state.restarts_this_outage} restart(s) this outage")


# ---------------------------------------------------------------------------
# Event processing
# ---------------------------------------------------------------------------
def process_event(line, cmd_sock, state):
    """Process a single event line from the control port."""

    # --- Clock jump ---
    # Don't DROPGUARDS — Tor recovers naturally after clock skew.
    # USR1/USR2 signals handle sleep/wake.
    if "CLOCK_SKEW" in line or "clock just jumped" in line:
        log("Clock skew detected — letting Tor recover naturally")
        state.last_recovery_time = time.time()
        state.last_recovery_trigger = "clock-skew"
        state.hs_desc_uploaded_since_recovery = False
        state.hs_desc_upload_started_since_recovery = False
        state.hs_desc_upload_failed_since_recovery = False
        state.hs_desc_last_failed_reason = ""
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

    # --- Descriptor publication events (onion service containers) ---
    # Event format: 650 HS_DESC <ACTION> <HSAddress> <AuthType> <HsDir> ...
    # Match with trailing space so UPLOAD doesn't match UPLOADED.
    if "HS_DESC UPLOADED " in line:
        # Log the first UPLOADED after each recovery arming, with elapsed
        # time and the trigger that armed the monitor. This is the positive
        # robustness signal: it confirms Tor actually republished after our
        # ADD path armed the stall monitor, and the elapsed time lets us
        # spot regressions in publication latency per trigger type.
        if (state.last_recovery_time > 0
                and not state.hs_desc_uploaded_since_recovery):
            elapsed = time.time() - state.last_recovery_time
            log(f"HS_DESC UPLOADED (trigger={state.last_recovery_trigger} "
                f"elapsed={elapsed:.1f}s)")
        state.hs_desc_uploaded_since_recovery = True
        return
    if "HS_DESC UPLOAD " in line:
        state.hs_desc_upload_started_since_recovery = True
        return
    if "HS_DESC RECEIVED " in line:
        # Fetch success. Only meaningful for the verdict HSFETCH we issued:
        # event format is `650 HS_DESC RECEIVED <Address> <AuthType> <HsDir> ...`.
        parts = line.split()
        addr = parts[3] if len(parts) > 3 else ""
        if state.hsfetch_pending and addr == state.hsfetch_pending:
            state.hsfetch_received = True
            state.hsfetch_pending = ""
        return
    if "HS_DESC FAILED " in line:
        # FAILED covers both upload failures and fetch failures; the address
        # token tells them apart. An answer to OUR pending verdict HSFETCH is
        # fetch evidence, not an upload failure.
        parts = line.split()
        addr = parts[3] if len(parts) > 3 else ""
        reason = ""
        if "REASON=" in line:
            reason = line.split("REASON=", 1)[1].split()[0]
        if state.hsfetch_pending and addr == state.hsfetch_pending:
            state.hsfetch_failed_reason = reason or "UNKNOWN"
            state.hsfetch_pending = ""
            return
        state.hs_desc_upload_failed_since_recovery = True
        if reason:
            state.hs_desc_last_failed_reason = reason
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

    # Circuit health, and the serving verdict the ladder hangs off. Read once
    # per pass and reused by everything below, heartbeat included.
    resp = send_cmd(cmd_sock, "GETINFO status/circuit-established")
    circuits_up = "circuit-established=1" in resp
    if state.bootstrapped and "circuit-established=0" in resp:
        do_dropguards(cmd_sock, state, "circuit-established=0 (circuits lost)")

    # End-to-end self-fetch — the measured serving input. Runs on its own
    # cadence inside; may block this pass for up to the probe timeout.
    maybe_e2e_probe(cmd_sock, state, circuits_up, now=now)

    # Periodic heartbeat log (every 5 minutes)
    if now - state.last_heartbeat_log > 300:
        log(f"alive — bootstrapped={state.bootstrapped}, "
            f"circuit-established={'1' if circuits_up else '0'}, "
            f"services_active={state.services_active}, "
            f"e2e_ok={state.e2e_ok}, "
            f"serving={is_serving(state, circuits_up)}")
        state.last_heartbeat_log = now

    serving = is_serving(state, circuits_up)
    update_serving_ledger(state, serving, now)

    # Bootstrap stall
    if (not state.bootstrapped
            and state.last_bootstrap_pct > 0
            and now - state.last_bootstrap_change > BOOTSTRAP_STALL_TIMEOUT
            and now - state.last_dropguards > DROPGUARDS_COOLDOWN):
        do_dropguards(cmd_sock, state,
                      f"bootstrap stalled at {state.last_bootstrap_pct}% for {BOOTSTRAP_STALL_TIMEOUT}s")

    # Descriptor upload stall — decide between DEL+ADD vs leave-alone based
    # on which HS_DESC events fired since recovery. The structured log line
    # lets analytics count branch frequencies across users.
    if (state.last_recovery_time > 0
            and not state.hs_desc_uploaded_since_recovery
            and now - state.last_recovery_time > HS_DESC_UPLOAD_TIMEOUT
            and state.bootstrapped):
        rearmed = False
        elapsed = int(now - state.last_recovery_time)
        started = state.hs_desc_upload_started_since_recovery
        failed = state.hs_desc_upload_failed_since_recovery
        trigger = state.last_recovery_trigger or "unknown"
        failed_reason = state.hs_desc_last_failed_reason or ""

        fields = (f"trigger={trigger} elapsed={elapsed}s "
                  f"upload_started={'yes' if started else 'no'} "
                  f"uploaded=no "
                  f"failed={'yes' if failed else 'no'}")
        if failed and failed_reason:
            fields += f" failed_reason={failed_reason}"

        if started and not failed:
            log(f"HS_DESC stall after recovery ({fields}) — UPLOAD in flight, leaving alone")
        elif state.services:
            log(f"HS_DESC stall after recovery ({fields}) — DEL+ADD to force republish")
            # Re-arm rather than disarm (rebuild_services re-arms on success).
            # Disarming is how the old code went quiet: it did exactly one
            # DEL+ADD per recovery and then stopped watching, so a descriptor
            # that never landed looked identical to one that did. Staying
            # armed is also what keeps `is_serving` false, which is what lets
            # the ladder climb.
            if rebuild_services(cmd_sock, state, "hs-desc-stall", now=now) > 0:
                rearmed = True
        else:
            # SOCKS-only container (no services). Fall back to HSFETCH to
            # refresh the client-side descriptor cache for known addresses.
            if not state.onion_addresses:
                state.onion_addresses = discover_onion_addresses()
            if state.onion_addresses:
                log(f"HS_DESC stall after recovery ({fields}) — no services configured; HSFETCH only")
                send_cmd(cmd_sock, "SIGNAL NEWNYM")
                for addr in state.onion_addresses:
                    resp = send_cmd(cmd_sock, f"HSFETCH {addr}")
                    if "250" in resp:
                        log(f"HSFETCH {addr[:16]}... — refreshing descriptor")
            else:
                log(f"HS_DESC stall after recovery ({fields}) — no services or addresses known; no action")
        if not rearmed:
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

    # Last resort for the bootstrap-stuck case (Tor never came up at all).
    # The not-serving ladder below covers the harder case: Tor that looks
    # perfectly healthy and reaches nobody.
    if (state.last_dropguards > 0
            and not state.bootstrapped
            and now - state.last_dropguards > HALT_COOLDOWN
            and now - state.last_halt > HALT_COOLDOWN):
        do_halt(cmd_sock, state,
                f"still not bootstrapped {HALT_COOLDOWN}s after recovery attempts")

    # ── The escalation ladder ────────────────────────────────────────────────
    transports = configured_transports()
    rung = next_escalation(state, now, has_transport=bool(transports))
    if rung:
        down_for = int(now - state.not_serving_since)
        if rung == "reclaim":
            log("verdict=takeover — hub is serving our address; republishing "
                "once to race the takeover descriptor, no restarts "
                "(the heartbeat reclaim is the cure)")
            rebuild_services(cmd_sock, state, "takeover-reclaim", now=now)
            state.reclaim_republished = True
        elif rung == "rebuild-hs":
            log(f"verdict={state.e2e_verdict} — not serving for {down_for}s; "
                f"DEL+ADD rebuild for fresh intro circuits + republish")
            rebuild_services(cmd_sock, state, f"verdict-{state.e2e_verdict}",
                             now=now)
            state.hs_rebuilt_this_outage = True
        elif rung == "restart-pt":
            do_restart_pt(cmd_sock, state,
                          f"not serving for {down_for}s — restarting "
                          f"{'/'.join(transports)}", now=now)
        elif rung == "restart-tor":
            do_halt(cmd_sock, state,
                    f"not serving for {down_for}s and a transport restart "
                    f"did not help", now=now)
        elif rung == "degraded":
            recent = len([t for t in state.tor_restarts
                          if now - t < DEGRADED_WINDOW])
            do_degrade(state,
                       f"{max(recent, state.restarts_this_outage)} Tor "
                       f"restarts this outage changed nothing "
                       f"(not serving for {down_for}s)")
        # Any rung action must reach disk on this very pass — for HALT the
        # container is about to die, and for the handoff the host reads it.
        state.last_state_write = 0

    # Publish what we know, whether or not we acted. moss has to be able to
    # answer "is my site live" at any moment, not only after a failure.
    if now - state.last_state_write > 15:
        write_state_file(state, circuits_up, now=now)
        state.last_state_write = now


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run():
    global _signal_sleep, _signal_wake

    log("Starting tor-watchdog")
    state = WatchdogState()
    # We may BE the restart the ladder just performed — see load_restart_history.
    load_restart_history(state)

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
        if state.services and not state.services_active and not state.sleeping:
            n, _c = add_all_services(cmd_sock, state.services)
            state.services_active = (n + _c) > 0
            if state.services_active:
                # Arm the HS_DESC stall monitor for the post-ADD publication
                # window. Without this, the stall handler doesn't engage until
                # the first USR2 fires. The menubar app used to send a
                # spurious USR2 here just to set last_recovery_time, which
                # re-entered the wake handler and collided with the ADD we
                # just did — forcing a wasteful DEL+ADD on every cold start.
                state.last_recovery_time = time.time()
                state.last_recovery_trigger = "startup"
                state.hs_desc_uploaded_since_recovery = False
                state.hs_desc_upload_started_since_recovery = False
                state.hs_desc_upload_failed_since_recovery = False
                state.hs_desc_last_failed_reason = ""

        log("Connected — monitoring Tor health")
        event_sock.settimeout(5)  # wake up frequently to check signals + stalls
        buf = ""

        while True:
            # Check for USR1 (sleep) signal
            if _signal_sleep:
                _signal_sleep = False
                log("Received USR1 (sleep) — removing onion services")
                state.sleeping = True
                if state.services and state.services_active:
                    del_all_services(cmd_sock, state.services)
                    state.services_active = False

            # Check for USR2 (wake) signal
            if _signal_wake:
                _signal_wake = False
                # One lid-open produces several USR2s (observed: 3), and the
                # handler below blocks for up to 120s waiting for circuits — so
                # the extras used to land mid-recovery and restart the wait from
                # the top, which is why the log showed "waiting for circuits"
                # twice for one wake and the ladder never made progress.
                if time.time() - state.last_wake_started < WAKE_DEBOUNCE:
                    log("Duplicate wake within "
                        f"{WAKE_DEBOUNCE}s — already recovering, ignoring")
                    continue
                state.last_wake_started = time.time()
                state.sleeping = False
                state.last_recovery_time = time.time()
                state.last_recovery_trigger = "wake"
                state.hs_desc_uploaded_since_recovery = False
                state.hs_desc_upload_started_since_recovery = False
                state.hs_desc_upload_failed_since_recovery = False
                state.hs_desc_last_failed_reason = ""

                # Wait for Tor to have live circuits before ADD_ONION.
                # After sleep, Tor may report bootstrapped=True (stale) but
                # circuit-established=0 if the network isn't up yet.  ADD_ONION
                # without circuits silently fails to upload descriptors.
                log("Received USR2 (wake) — waiting for circuits before ADD_ONION...")
                waited = 0
                while waited < 120:
                    resp = send_cmd(cmd_sock, "GETINFO status/circuit-established")
                    if "circuit-established=1" in resp:
                        log(f"Circuits established after {waited}s — proceeding with ADD_ONION")
                        break
                    time.sleep(5)
                    waited += 5
                else:
                    log(f"WARNING: No circuits after {waited}s — attempting ADD_ONION anyway")

                if state.services:
                    n, collisions = add_all_services(cmd_sock, state.services)
                    if collisions > 0:
                        # Services were re-added during sleep (race condition).
                        # DEL then ADD to force fresh descriptor publication
                        # (rebuild_services also NEWNYMs so our own client
                        # cache drops the pre-republish descriptor).
                        log(f"Collision on wake — DEL+ADD to force fresh descriptors")
                        state.services_active = (
                            rebuild_services(cmd_sock, state, "wake") > 0)
                    else:
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
                # But NOT while sleeping — DEL_ONION was intentional
                # And only once circuits are established — ADD_ONION without
                # circuits silently fails to upload descriptors.
                if state.services and not state.services_active and not state.sleeping:
                    resp = send_cmd(cmd_sock, "GETINFO status/circuit-established")
                    if "circuit-established=1" in resp:
                        n, _c = add_all_services(cmd_sock, state.services)
                        state.services_active = (n + _c) > 0
                        if state.services_active:
                            # Arm HS_DESC stall monitor for the post-reconnect
                            # publication window — same reason as the startup
                            # ADD path above.
                            state.last_recovery_time = time.time()
                            state.last_recovery_trigger = "reconnect"
                            state.hs_desc_uploaded_since_recovery = False
                            state.hs_desc_upload_started_since_recovery = False
                            state.hs_desc_upload_failed_since_recovery = False
                            state.hs_desc_last_failed_reason = ""

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
