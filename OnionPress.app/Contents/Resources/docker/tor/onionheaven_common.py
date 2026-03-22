"""
OnionHeaven shared module — constants, DB schema, takeover/release functions.

Imported by both onionheaven-server.py and onionheaven-heartbeat.py to ensure
consistent schema and decision logic.

Takeover/release engine: the heartbeat is the single orchestrator. It picks a
bootstrapped worker container with the fewest addresses and executes takeover/
release directly via `docker exec`. No polling loops, no DB-mediated IPC.
Workers are just Tor instances — they receive commands, not requests.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ONIONHEAVEN_DATA_DIR = "/var/lib/onionpress/onionheaven"
DB_PATH = os.path.join(ONIONHEAVEN_DATA_DIR, "registry.db")
KEYS_DIR = os.path.join(ONIONHEAVEN_DATA_DIR, "keys")
TOR_MANAGER = "/onionheaven-tor-manager.sh"

# How long to wait after the last successful healthcheck before considering
# a node stale enough for Arti takeover. Override via env for testing.
PROPAGATION_DELAY = int(os.environ.get("TOR_PROPAGATION_DELAY", "180"))

# How many missed heartbeats before considering takeover.
# With 60s heartbeat interval and 180s PROPAGATION_DELAY, this is ~3 missed beats.
CONSECUTIVE_FAILS_THRESHOLD = int(os.environ.get("ONIONHEAVEN_CONSECUTIVE_FAILS", "3"))

# Extra grace period (seconds) for OnionHeaven peers before takeover.
# Peers run OnionHeaven themselves, so a restart is expected to take longer.
# Default 30 minutes — a peer that's been down 30+ minutes is likely truly dead.
ONIONHEAVEN_PEER_GRACE = int(os.environ.get("ONIONHEAVEN_PEER_GRACE", "1800"))

# Minimum interval between SIGHUPs to Tor (seconds).
# Higher values reduce circuit rebuilds at the cost of slower takeover/release.
SIGHUP_MIN_INTERVAL = int(os.environ.get("ONIONHEAVEN_SIGHUP_INTERVAL", "60"))

# Container identity — set by entrypoint for takeover workers
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg):
    """Log to stderr with timestamp (captured by docker logs).

    Uses local time (not UTC) so timestamps match the host-side stress test
    logs and operator's clock. The container inherits /etc/localtime from
    the Colima VM, which mirrors the Mac's timezone.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"[{ts}] OnionHeaven: {msg}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Rate-limited SIGHUP
# ---------------------------------------------------------------------------

_last_sighup_time = 0.0
_sighup_pending = False


def _is_ctor():
    """Check if we're running C Tor (not Arti). SIGHUP is harmful for C Tor
    with ephemeral ADD_ONION services — it re-reads the torrc and kills them."""
    return os.environ.get("TOR_IMPL", "tor").lower() == "tor"


def sighup_tor():
    """Send SIGHUP to Arti. No-op for C Tor (would kill ephemeral services)."""
    if _is_ctor():
        return
    global _last_sighup_time, _sighup_pending
    import time as _time
    now = _time.monotonic()
    elapsed = now - _last_sighup_time

    if elapsed >= SIGHUP_MIN_INTERVAL:
        _do_sighup()
        _last_sighup_time = now
        _sighup_pending = False
    else:
        _sighup_pending = True


def flush_sighup_tor(force=False):
    """Send a final SIGHUP if pending. No-op for C Tor."""
    if _is_ctor():
        return
    global _sighup_pending, _last_sighup_time
    import time as _time
    if _sighup_pending or force:
        _do_sighup()
        _last_sighup_time = _time.monotonic()
        _sighup_pending = False


def _do_sighup():
    """Send SIGHUP to Tor (Arti or C Tor) via tor-manager, then check for corrupted key errors.

    Sanitizes the config BEFORE sending SIGHUP to prevent stale fragments
    from blocking reload.
    """
    _sanitize_arti_toml()

    try:
        result = subprocess.run(
            [TOR_MANAGER, "sighup"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            log(f"SIGHUP sent to Tor")
        else:
            log(f"SIGHUP failed: {result.stderr.strip()}")
    except Exception as e:
        log(f"SIGHUP error: {e}")

    # Check for corrupted key errors after SIGHUP and auto-clean
    _check_arti_key_errors()


def _sanitize_arti_toml():
    """Remove broken fragments from Arti toml before SIGHUP.

    Previous cleanup bugs left orphaned lines like bare [["80", ...]] which
    TOML parses as invalid table headers, blocking ALL config reloads.
    """
    if os.environ.get("NO_ONION_SERVICE") == "1" or os.environ.get("TAKEOVER_WORKER") == "1":
        toml_path = "/etc/arti/arti-onionheaven.toml"
    else:
        toml_path = "/etc/arti/arti.toml"

    try:
        with open(toml_path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return

    cleaned = []
    changed = False
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Remove bare proxy_ports value lines (orphaned from buggy cleanup)
        # Matches [["80", "127.0.0.1:XXXX"]] but NOT proxy_ports = [["80"...]]
        if stripped.startswith("[["  ) and "127.0.0.1" in stripped and stripped.endswith("]]"):
            log(f"sanitize-toml: removing orphaned line: {stripped}")
            changed = True
            i += 1
            continue

        # Remove orphaned comment lines not followed by a proper section header
        if stripped.startswith("# onionheaven:") and stripped.endswith(".onion"):
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines) and lines[j].strip().startswith('[onion_services."'):
                cleaned.append(line)
                i += 1
                continue
            else:
                log(f"sanitize-toml: removing orphaned comment: {stripped}")
                changed = True
                i += 1
                continue

        cleaned.append(line)
        i += 1

    if changed:
        # Collapse excessive blank lines
        final = []
        prev_blank = False
        for line in cleaned:
            if line.strip() == "":
                if prev_blank:
                    continue
                prev_blank = True
            else:
                prev_blank = False
            final.append(line)
        with open(toml_path, "w") as f:
            f.writelines(final)
        log(f"sanitize-toml: cleaned {toml_path}")


def _check_arti_key_errors():
    """Scan recent Arti log output for corrupted key errors and auto-clean.

    Arti logs errors like:
      Unable to launch onion service onionheaven_XXXX: ... PEM preamble contains invalid data (NUL byte)
      Unable to launch onion service onionheaven_XXXX: ... corrupted data in keystore

    When found, remove the corrupted key from keystore and toml config so it
    doesn't block other services on subsequent SIGHUPs.
    """
    import re
    arti_keystore = "/var/lib/arti/state/keystore/hss"

    # Read recent Arti stderr — check the container's process stderr via /proc
    arti_pid = None
    try:
        result = subprocess.run(
            ["pidof", "arti"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            arti_pid = result.stdout.strip().split()[0]
    except Exception:
        pass
    if not arti_pid:
        try:
            result = subprocess.run(
                ["sh", "-c", "ps aux | grep '[/]usr/local/bin/arti' | awk '{print $2}' | head -1"],
                capture_output=True, text=True, timeout=5
            )
            arti_pid = result.stdout.strip()
        except Exception:
            pass

    if not arti_pid:
        return

    # Read Arti's fd 2 (stderr) isn't possible after the fact, but we can check
    # the keystore directly for corrupted keys
    try:
        if not os.path.isdir(arti_keystore):
            return
        for nickname in os.listdir(arti_keystore):
            if not nickname.startswith("onionheaven_"):
                continue
            key_path = os.path.join(arti_keystore, nickname, "ks_hs_id.ed25519_expanded_private")
            if not os.path.isfile(key_path):
                continue
            # Check for NUL bytes or other corruption
            try:
                with open(key_path, "rb") as f:
                    data = f.read()
                if b"\x00" in data:
                    log(f"CORRUPTED KEY detected in Arti keystore: {nickname} — auto-cleaning")
                    _clean_corrupted_service(nickname)
                elif not data.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----"):
                    log(f"INVALID KEY detected in Arti keystore: {nickname} — auto-cleaning")
                    _clean_corrupted_service(nickname)
                elif not data.rstrip().endswith(b"-----END OPENSSH PRIVATE KEY-----"):
                    log(f"TRUNCATED KEY detected in Arti keystore: {nickname} — auto-cleaning")
                    _clean_corrupted_service(nickname)
            except Exception as e:
                log(f"Error checking key {nickname}: {e}")
    except Exception as e:
        log(f"Error scanning Arti keystore: {e}")


def _clean_corrupted_service(nickname):
    """Remove a corrupted onion service from Arti's keystore and config."""
    import shutil
    arti_keystore = "/var/lib/arti/state/keystore/hss"

    # Detect config file
    if os.environ.get("NO_ONION_SERVICE") == "1" or os.environ.get("TAKEOVER_WORKER") == "1":
        arti_toml = "/etc/arti/arti-onionheaven.toml"
    else:
        arti_toml = "/etc/arti/arti.toml"

    # Remove keystore directory
    ks_dir = os.path.join(arti_keystore, nickname)
    if os.path.isdir(ks_dir):
        shutil.rmtree(ks_dir, ignore_errors=True)
        log(f"  Removed keystore dir: {ks_dir}")

    # Remove from arti.toml config
    try:
        with open(arti_toml, "r") as f:
            lines = f.readlines()
        new_lines = []
        skip = 0
        for line in lines:
            if skip > 0:
                skip -= 1
                continue
            if f'[onion_services."{nickname}"]' in line:
                # Remove this line + next 2 (enabled, proxy_ports)
                skip = 2
                # Also remove preceding comment line if it's the marker
                if new_lines and new_lines[-1].startswith("# onionheaven:"):
                    new_lines.pop()
                # Remove preceding blank line too
                if new_lines and new_lines[-1].strip() == "":
                    new_lines.pop()
                continue
            new_lines.append(line)
        with open(arti_toml, "w") as f:
            f.writelines(new_lines)
        log(f"  Removed config for {nickname} from {arti_toml}")
    except Exception as e:
        log(f"  Warning: could not clean config for {nickname}: {e}")

    # Also clean the source key in OnionHeaven keys dir
    # Extract content_address from nickname: onionheaven_XXXX -> find matching key dir
    addr_prefix = nickname.replace("onionheaven_", "")
    keys_base = "/var/lib/onionpress/onionheaven/keys"
    if os.path.isdir(keys_base):
        for entry in os.listdir(keys_base):
            if entry.startswith(addr_prefix):
                src_key = os.path.join(keys_base, entry, "ks_hs_id.ed25519_expanded_private")
                if os.path.isfile(src_key):
                    try:
                        with open(src_key, "rb") as f:
                            data = f.read()
                        if b"\x00" in data or not data.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----"):
                            os.unlink(src_key)
                            log(f"  Removed corrupted source key for {entry}")
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def db_connect():
    """Open OnionHeaven SQLite database with WAL mode."""
    os.makedirs(ONIONHEAVEN_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def db_commit_with_retry(conn, max_retries=5, base_delay=0.5):
    """Commit with retry on 'database is locked' errors.

    The busy_timeout PRAGMA handles most contention, but under extreme load
    (800+ concurrent heartbeats) the timeout can still be exceeded.
    """
    for attempt in range(max_retries):
        try:
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                log(f"DB locked on commit, retry {attempt + 1}/{max_retries} in {delay:.1f}s")
                time.sleep(delay)
            else:
                raise


def db_ensure_schema(conn):
    """Create the registry table with the new schema.

    Starting fresh — no migration from the old fail_count/takeover_active schema.
    If the old table exists, drop it and recreate.
    """
    # Check if table exists with old schema
    cols = []
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(registry)").fetchall()]
    except sqlite3.Error:
        pass

    if cols and ("fail_count" in cols or "takeover_active" in cols or "last_contact" in cols
                 or "last_polled" in cols):
        # Old schema detected — drop and recreate
        log("Old schema detected — dropping and recreating registry table")
        conn.execute("DROP TABLE registry")
        cols = []

    if not cols:
        conn.execute("""CREATE TABLE IF NOT EXISTS registry (
            content_address     TEXT NOT NULL,
            healthcheck_address TEXT NOT NULL,
            key_hash            TEXT,
            registered_at       TEXT NOT NULL,
            unregistered_at     TEXT,
            unregistered_reason TEXT,
            version             TEXT DEFAULT 'unknown',
            status              TEXT DEFAULT 'online',
            last_checked        TEXT,
            last_healthy        TEXT,
            last_released       TEXT,
            last_taken_over     TEXT,
            last_redirect       TEXT,
            takeover_container  TEXT,
            takeover_pending    TEXT,
            release_pending     TEXT,
            consecutive_fails   INTEGER DEFAULT 0,
            wordpress_healthy   INTEGER,
            wordpress_checked_at TEXT,
            audit_result        TEXT,
            audit_at            TEXT,
            is_onionheaven      INTEGER DEFAULT 0,
            PRIMARY KEY (content_address, healthcheck_address)
        )""")
        db_commit_with_retry(conn)
    else:
        # Table exists — add any missing columns
        for col in [
            "unregistered_at", "unregistered_reason",
            "last_checked", "last_healthy", "last_released",
            "last_taken_over", "last_redirect",
            "takeover_container", "takeover_pending", "release_pending",
            "wordpress_checked_at", "audit_result", "audit_at",
        ]:
            if col not in cols:
                conn.execute(f"ALTER TABLE registry ADD COLUMN {col} TEXT")
        if "consecutive_fails" not in cols:
            conn.execute("ALTER TABLE registry ADD COLUMN consecutive_fails INTEGER DEFAULT 0")
        if "wordpress_healthy" not in cols:
            conn.execute("ALTER TABLE registry ADD COLUMN wordpress_healthy INTEGER")
        if "is_onionheaven" not in cols:
            conn.execute("ALTER TABLE registry ADD COLUMN is_onionheaven INTEGER DEFAULT 0")
        db_commit_with_retry(conn)

    # Drop deprecated tables from old polling architecture
    conn.execute("DROP TABLE IF EXISTS poll_containers")
    conn.execute("DROP TABLE IF EXISTS farm_scale_requests")

    # Migrate takeover_containers to new schema if needed
    tc_cols = []
    try:
        tc_cols = [row[1] for row in conn.execute("PRAGMA table_info(takeover_containers)").fetchall()]
    except sqlite3.Error:
        pass

    if tc_cols and "bootstrapped" not in tc_cols:
        # Old schema — drop and recreate
        conn.execute("DROP TABLE takeover_containers")
        tc_cols = []

    if not tc_cols:
        conn.execute("""CREATE TABLE IF NOT EXISTS takeover_containers (
            container_name  TEXT PRIMARY KEY,
            max_services    INTEGER DEFAULT 50,
            bootstrapped    INTEGER DEFAULT 0,
            assigned_count  INTEGER DEFAULT 0,
            created_at      TEXT
        )""")

    db_commit_with_retry(conn)


# ---------------------------------------------------------------------------
# Farm mode helpers
# ---------------------------------------------------------------------------

# Docker image for takeover workers (same as main tor image)
TAKEOVER_IMAGE = os.environ.get("ONIONHEAVEN_IMAGE",
                                "ghcr.io/brewsterkahle/onionpress-tor:latest")
MAX_SERVICES_PER_WORKER = int(os.environ.get("ONIONHEAVEN_MAX_SERVICES", "50"))

# Next index for naming new takeover containers
_next_worker_index = 0


def is_farm_mode():
    """True if we can manage takeover containers.

    True when ONIONHEAVEN=1 (heartbeat container) or when the docker
    socket is available (onionpress-tor server container).
    """
    if os.environ.get("ONIONHEAVEN") == "1":
        return True
    return os.path.exists("/var/run/docker.sock")


def _init_worker_index(conn):
    """Set _next_worker_index based on existing containers in DB."""
    global _next_worker_index
    try:
        rows = conn.execute(
            "SELECT container_name FROM takeover_containers"
        ).fetchall()
        max_idx = -1
        for row in rows:
            name = row["container_name"]
            # parse "onionheaven-takeover-N"
            try:
                idx = int(name.rsplit("-", 1)[-1])
                if idx > max_idx:
                    max_idx = idx
            except (ValueError, IndexError):
                pass
        _next_worker_index = max_idx + 1
    except sqlite3.OperationalError:
        _next_worker_index = 0


def _pick_worker(conn):
    """Pick the bootstrapped worker with the fewest addresses.

    Returns container_name or None if no bootstrapped workers exist.
    """
    try:
        row = conn.execute(
            "SELECT container_name FROM takeover_containers "
            "WHERE bootstrapped = 1 ORDER BY assigned_count ASC LIMIT 1"
        ).fetchone()
        return row["container_name"] if row else None
    except sqlite3.OperationalError:
        return None


def _ensure_capacity(conn):
    """If all bootstrapped workers are at max_services and fewer than 2
    containers are currently building, kick off 2 more.

    Called after each takeover assignment.
    """
    try:
        # Any bootstrapped worker under max_services? If so, no need to build.
        under_cap = conn.execute(
            "SELECT COUNT(*) FROM takeover_containers "
            "WHERE bootstrapped = 1 AND assigned_count < ?",
            (MAX_SERVICES_PER_WORKER,)
        ).fetchone()[0]
        if under_cap > 0:
            return

        # How many are currently building?
        building = conn.execute(
            "SELECT COUNT(*) FROM takeover_containers WHERE bootstrapped = 0"
        ).fetchone()[0]
        if building >= 2:
            return

        # Build 2 more
        to_build = 2 - building
        for _ in range(to_build):
            _spawn_worker(conn)
    except sqlite3.OperationalError:
        pass


def _spawn_worker(conn):
    """Start a new takeover worker container via docker run."""
    global _next_worker_index
    idx = _next_worker_index
    _next_worker_index += 1
    name = f"onionheaven-takeover-{idx}"
    vol = f"onionpress-arti-state-takeover-{idx}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Remove any existing container with this name
    subprocess.run(["docker", "rm", "-f", name],
                   capture_output=True, timeout=15)

    tz = os.environ.get("TZ", "UTC")
    tor_impl = os.environ.get("TOR_IMPL", "tor")

    try:
        result = subprocess.run(
            ["docker", "run", "-d",
             "--name", name,
             "--network", "onionpress-network",
             "--ulimit", "nofile=10000:10000",
             "-e", f"TZ={tz}",
             "-e", f"TOR_IMPL={tor_impl}",
             "-e", "TAKEOVER_WORKER=1",
             "-e", f"CONTAINER_NAME={name}",
             "-e", f"MAX_TAKEOVER_SERVICES={MAX_SERVICES_PER_WORKER}",
             "-v", f"{vol}:/var/lib/arti/",
             "-v", "onionpress-persistent-data:/var/lib/onionpress",
             "--restart", "unless-stopped",
             TAKEOVER_IMAGE],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            # Insert into DB as not-yet-bootstrapped
            conn.execute(
                "INSERT OR REPLACE INTO takeover_containers "
                "(container_name, max_services, bootstrapped, assigned_count, created_at) "
                "VALUES (?, ?, 0, 0, ?)",
                (name, MAX_SERVICES_PER_WORKER, now)
            )
            db_commit_with_retry(conn)
            log(f"Spawned takeover worker {name} (bootstrapping...)")
        else:
            log(f"WARNING: Failed to spawn {name}: {result.stderr.strip()}")
            _next_worker_index -= 1  # reuse index
    except Exception as e:
        log(f"ERROR spawning {name}: {e}")
        _next_worker_index -= 1


def check_worker_bootstrap(conn):
    """Check if any not-yet-bootstrapped workers are now ready.

    Called each heartbeat cycle. Checks Tor bootstrap status via docker exec.
    """
    try:
        rows = conn.execute(
            "SELECT container_name FROM takeover_containers WHERE bootstrapped = 0"
        ).fetchall()
    except sqlite3.OperationalError:
        return

    for row in rows:
        name = row["container_name"]
        try:
            # Use cookie auth — control port requires it
            result = subprocess.run(
                ["docker", "exec", name, "sh", "-c",
                 "COOKIE=$(xxd -p -c 64 /var/lib/tor/control_auth_cookie 2>/dev/null) && "
                 "printf 'AUTHENTICATE %s\\r\\nGETINFO status/bootstrap-phase\\r\\nQUIT\\r\\n' "
                 "\"$COOKIE\" | nc -q 1 127.0.0.1 9051 2>/dev/null || true"],
                capture_output=True, text=True, timeout=10
            )
            if "PROGRESS=100" in result.stdout:
                conn.execute(
                    "UPDATE takeover_containers SET bootstrapped = 1 "
                    "WHERE container_name = ?",
                    (name,)
                )
                db_commit_with_retry(conn)
                log(f"Takeover worker {name} is bootstrapped and ready")
        except Exception:
            pass  # container may not be running yet, retry next cycle


def cleanup_dead_workers(conn):
    """Remove DB entries for workers whose containers no longer exist.

    Called each heartbeat cycle.
    """
    try:
        rows = conn.execute(
            "SELECT container_name FROM takeover_containers"
        ).fetchall()
    except sqlite3.OperationalError:
        return

    for row in rows:
        name = row["container_name"]
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", name],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0 or "true" not in result.stdout.lower():
                # Container dead — clear assignments and remove
                conn.execute(
                    "UPDATE registry SET takeover_container = NULL "
                    "WHERE takeover_container = ? AND status = 'taken-over'",
                    (name,)
                )
                conn.execute(
                    "DELETE FROM takeover_containers WHERE container_name = ?",
                    (name,)
                )
                db_commit_with_retry(conn)
                log(f"Cleaned up dead worker {name}")
        except Exception:
            pass


def _exec_takeover(container_name, content_address):
    """Queue takeover on a worker via the queue manager.

    The queue manager rate-limits ADD_ONION calls and monitors descriptor
    propagation. Returns True if the address was queued successfully.
    """
    log(f"Queuing takeover of {content_address} on {container_name}")
    try:
        result = subprocess.run(
            ["docker", "exec", container_name,
             "python3", "/onionheaven-queue-manager.py", "takeover", content_address],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            try:
                resp = json.loads(result.stdout.strip())
                status = resp.get("status", "")
                if status in ("queued", "already_queued"):
                    log(f"Takeover queued: {content_address} on {container_name} ({status})")
                    return True
            except (json.JSONDecodeError, ValueError):
                pass
            log(f"Takeover queued: {content_address} on {container_name}")
            return True
        else:
            log(f"Takeover queue failed for {content_address} on {container_name}: "
                f"{result.stderr.strip()}")
            return False
    except Exception as e:
        log(f"Takeover error for {content_address} on {container_name}: {e}")
        return False


def _exec_release(container_name, content_address):
    """Release on a worker via the queue manager.

    Returns True on success, False on failure.
    """
    log(f"Releasing {content_address} via {container_name}")
    try:
        result = subprocess.run(
            ["docker", "exec", container_name,
             "python3", "/onionheaven-queue-manager.py", "release", content_address],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log(f"Release complete: {content_address} on {container_name}")
            return True
        else:
            log(f"Release failed for {content_address} on {container_name}: "
                f"{result.stderr.strip()}")
            return False
    except Exception as e:
        log(f"Release error for {content_address} on {container_name}: {e}")
        return False


def get_queue_status(container_name):
    """Get queue status from a worker's queue manager.

    Returns dict with queued/in_flight/active counts, or None on error.
    """
    try:
        result = subprocess.run(
            ["docker", "exec", container_name,
             "python3", "/onionheaven-queue-manager.py", "status"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return json.loads(result.stdout.strip())
    except Exception:
        pass
    return None


def get_addr_serving_status(conn, content_address):
    """Get the serving_status for a content_address.

    Returns one of: registered, queued-to-activate, activating, active, failed, unknown.
    """
    row = conn.execute(
        "SELECT status, takeover_container FROM registry "
        "WHERE content_address = ? AND unregistered_at IS NULL LIMIT 1",
        (content_address,)
    ).fetchone()

    if not row:
        return "unknown"

    if row["status"] == "online":
        return "registered"

    if row["status"] != "taken-over":
        return "unknown"

    container = row["takeover_container"]
    if not container:
        return "queued-to-activate"  # taken-over but no worker assigned yet

    # Ask the worker's queue manager
    try:
        result = subprocess.run(
            ["docker", "exec", container,
             "python3", "/onionheaven-queue-manager.py", "addr_state", content_address],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            resp = json.loads(result.stdout.strip())
            state = resp.get("serving_status")
            if state:
                return state
    except Exception:
        pass

    return "activating"  # fallback — assigned but can't reach worker


# ---------------------------------------------------------------------------
# Takeover function
# ---------------------------------------------------------------------------

def takeover_function(conn, content_address, healthcheck_address, force=False):
    """Handle takeover for a single registry row.

    1. Mark the row status='taken-over' and record last_taken_over.
    2. Check guards:
       - No OTHER row for same content_address already has status='taken-over'
       - No other row for content_address has status='online'
       - last_healthy is stale (now - last_healthy > PROPAGATION_DELAY) OR force=True
    3. Pick the bootstrapped worker with fewest addresses → docker exec takeover.
    4. If all workers are at max_services and <2 building, spawn 2 more.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    row = conn.execute(
        "SELECT * FROM registry WHERE content_address = ? AND healthcheck_address = ?",
        (content_address, healthcheck_address)
    ).fetchone()

    if not row:
        log(f"ERROR: takeover_function called for non-existent row: "
            f"{content_address} / {healthcheck_address}")
        return

    if row["status"] != "online" and not force:
        return

    # Mark this row as taken-over
    conn.execute(
        "UPDATE registry SET status = 'taken-over', last_taken_over = ? "
        "WHERE content_address = ? AND healthcheck_address = ?",
        (now, content_address, healthcheck_address)
    )
    db_commit_with_retry(conn)
    log(f"Marked {healthcheck_address} as taken-over for {content_address}")

    # Guards: should we actually start serving the onion service?
    already_serving = conn.execute(
        "SELECT COUNT(*) FROM registry "
        "WHERE content_address = ? AND healthcheck_address != ? AND status = 'taken-over'",
        (content_address, healthcheck_address)
    ).fetchone()[0] > 0

    if already_serving:
        log(f"Skipping takeover for {content_address} — another row already taken-over")
        return

    no_other_online = conn.execute(
        "SELECT COUNT(*) FROM registry "
        "WHERE content_address = ? AND healthcheck_address != ? AND status = 'online'",
        (content_address, healthcheck_address)
    ).fetchone()[0] == 0

    if not no_other_online:
        log(f"Skipping takeover for {content_address} — other row(s) still online")
        return

    # Check propagation delay
    last_healthy_stale = True
    if row["last_healthy"] and not force:
        try:
            lh = datetime.fromisoformat(row["last_healthy"].replace("Z", "+00:00"))
            now_dt = datetime.now(timezone.utc)
            elapsed = (now_dt - lh).total_seconds()
            last_healthy_stale = elapsed > PROPAGATION_DELAY
        except (ValueError, TypeError):
            last_healthy_stale = True

    if not last_healthy_stale:
        log(f"Skipping takeover for {content_address} — last_healthy not yet stale")
        return

    # All guards pass — execute takeover on a worker
    if is_farm_mode():
        worker = _pick_worker(conn)
        if worker:
            success = _exec_takeover(worker, content_address)
            if success:
                conn.execute(
                    "UPDATE registry SET takeover_container = ?, takeover_pending = NULL "
                    "WHERE content_address = ? AND healthcheck_address = ?",
                    (worker, content_address, healthcheck_address)
                )
                conn.execute(
                    "UPDATE takeover_containers SET assigned_count = assigned_count + 1 "
                    "WHERE container_name = ?",
                    (worker,)
                )
                db_commit_with_retry(conn)
            # Check if we need more capacity
            _ensure_capacity(conn)
            return
        else:
            # No bootstrapped workers — spawn 2 if not already building
            _ensure_capacity(conn)
            log(f"Takeover deferred for {content_address} — no bootstrapped workers yet")
            return

    # Legacy fallback — only safe inside takeover worker containers.
    if os.environ.get("TAKEOVER_WORKER") == "1":
        _takeover_local(content_address)
    else:
        log(f"ERROR: takeover_function fell through to legacy mode for "
            f"{content_address} — not farm mode and not a takeover worker.")


def _takeover_local(content_address, no_sighup=False):
    """Execute takeover via local tor-manager (takeover worker containers only).

    C Tor: uses ADD_ONION via control port (no SIGHUP needed).
    Arti: modifies config; if no_sighup=True, caller must call flush_sighup_tor().
    """
    log(f"Taking over {content_address} via tor-manager (local)")
    try:
        result = subprocess.run(
            [TOR_MANAGER, "takeover", "--no-sighup", content_address],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log(f"Takeover complete for {content_address}")
            return True
        else:
            log(f"Takeover failed for {content_address}: {result.stderr.strip()}")
            return False
    except Exception as e:
        log(f"Takeover error for {content_address}: {e}")
        return False

    if not no_sighup:
        sighup_tor()


# ---------------------------------------------------------------------------
# Release function
# ---------------------------------------------------------------------------

def release_function(conn, content_address, healthcheck_address):
    """Handle release for a single registry row.

    Owns the status change: checks if taken-over, marks online, does DEL_ONION,
    decrements assigned_count. If already online, returns early — prevents
    double-release and double-decrement.
    """
    row = conn.execute(
        "SELECT * FROM registry WHERE content_address = ? AND healthcheck_address = ?",
        (content_address, healthcheck_address)
    ).fetchone()

    if not row:
        return

    if row["status"] != "taken-over":
        return

    takeover_container = row["takeover_container"] if "takeover_container" in row.keys() else None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Mark this row as online — release_function owns this status change
    conn.execute(
        "UPDATE registry SET status = 'online', last_released = ?, "
        "takeover_container = NULL, takeover_pending = NULL, release_pending = NULL "
        "WHERE content_address = ? AND healthcheck_address = ?",
        (now, content_address, healthcheck_address)
    )
    db_commit_with_retry(conn)
    log(f"Released {content_address} (was on {takeover_container or 'none'})")

    # Execute DEL_ONION on the worker and decrement assigned_count
    if takeover_container:
        success = _exec_release(takeover_container, content_address)
        if success:
            conn.execute(
                "UPDATE takeover_containers SET assigned_count = MAX(0, assigned_count - 1) "
                "WHERE container_name = ?",
                (takeover_container,)
            )
            db_commit_with_retry(conn)
        return

    # Legacy fallback — only inside takeover worker containers
    if os.environ.get("TAKEOVER_WORKER") == "1":
        _release_local(content_address)


def _release_local(content_address, no_sighup=False):
    """Execute release via local tor-manager (takeover worker containers only).

    If no_sighup=True, skips the SIGHUP — caller is responsible for
    calling flush_sighup_tor() after a batch of releases.
    """
    log(f"Releasing {content_address} via tor-manager (local)")
    try:
        result = subprocess.run(
            [TOR_MANAGER, "release", "--no-sighup", content_address],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log(f"Release complete for {content_address}")
            return True
        else:
            log(f"Release failed for {content_address}: {result.stderr.strip()}")
            return False
    except Exception as e:
        log(f"Release error for {content_address}: {e}")
        return False

    if not no_sighup:
        sighup_tor()


# ---------------------------------------------------------------------------
# Unregister function
# ---------------------------------------------------------------------------

def unregister_entry(conn, content_address, healthcheck_address, reason="unregistered"):
    """Fully unregister an entry: release from worker, mark unregistered, delete keys.

    This is the single path for all cleanup — called by /unregister handler
    and by implicit unregistration (superseded by new healthcheck, stale
    stress test cleanup, etc.).
    """
    import shutil

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Release from worker if taken-over (DEL_ONION + decrement)
    release_function(conn, content_address, healthcheck_address)

    # Mark as unregistered
    conn.execute(
        "UPDATE registry SET unregistered_at = ?, "
        "unregistered_reason = ?, status = 'unregistered' "
        "WHERE content_address = ? AND healthcheck_address = ?",
        (now, reason, content_address, healthcheck_address)
    )
    db_commit_with_retry(conn)

    # Delete key directory if no other active rows use this content_address
    other_active = conn.execute(
        "SELECT COUNT(*) FROM registry WHERE content_address = ? "
        "AND unregistered_at IS NULL",
        (content_address,)
    ).fetchone()[0]

    if other_active == 0:
        key_dir = os.path.join(KEYS_DIR, content_address)
        if os.path.isdir(key_dir):
            shutil.rmtree(key_dir, ignore_errors=True)
            log(f"Deleted keys for {content_address}")


# ---------------------------------------------------------------------------
# Stress test cleanup — remove only stress test entries, preserve real ones
# ---------------------------------------------------------------------------

def cleanup_stress_tests(conn=None):
    """Remove all stress test entries and release their takeovers.

    Only touches registry rows where version LIKE 'stress-test%'.
    Real entries and their keys are never deleted. Takeover workers are
    removed only if they have no remaining (real) assignments.

    Returns a dict summarising what was cleaned up.
    """
    import shutil

    own_conn = conn is None
    if own_conn:
        conn = db_connect()
        db_ensure_schema(conn)

    stats = {
        "registry_deleted": 0,
        "released": 0,
        "keys_deleted": 0,
        "workers_removed": 0,
    }

    # 1. Find all stress test entries
    stress_rows = conn.execute(
        "SELECT content_address, healthcheck_address, status, takeover_container "
        "FROM registry WHERE version LIKE 'stress-test%'"
    ).fetchall()

    stats["registry_deleted"] = len(stress_rows)
    if not stress_rows:
        log("cleanup_stress_tests: no stress test entries found")
        if own_conn:
            conn.close()
        return stats

    # 2. Release taken-over entries from their workers
    for row in stress_rows:
        if row["status"] == "taken-over" and row["takeover_container"]:
            container = row["takeover_container"]
            ca = row["content_address"]
            try:
                _exec_release(container, ca)
                stats["released"] += 1
            except Exception as e:
                log(f"cleanup_stress_tests: release failed for {ca} on {container}: {e}")
            # Decrement assigned_count
            conn.execute(
                "UPDATE takeover_containers SET assigned_count = MAX(0, assigned_count - 1) "
                "WHERE container_name = ?",
                (container,)
            )

    # 3. Delete stress test rows from registry
    conn.execute("DELETE FROM registry WHERE version LIKE 'stress-test%'")
    db_commit_with_retry(conn)
    log(f"cleanup_stress_tests: deleted {stats['registry_deleted']} registry rows")

    # 4. Delete keys that belong ONLY to stress test content addresses.
    #    A key is safe to delete only if no real entry uses the same content_address.
    stress_addrs = set(row["content_address"] for row in stress_rows)
    for ca in stress_addrs:
        real_refs = conn.execute(
            "SELECT COUNT(*) FROM registry WHERE content_address = ? "
            "AND version NOT LIKE 'stress-test%'",
            (ca,)
        ).fetchone()[0]
        if real_refs == 0:
            key_dir = os.path.join(KEYS_DIR, ca)
            if os.path.isdir(key_dir):
                shutil.rmtree(key_dir, ignore_errors=True)
                stats["keys_deleted"] += 1

    if stats["keys_deleted"]:
        log(f"cleanup_stress_tests: deleted {stats['keys_deleted']} key directories")

    # 5. Remove takeover workers that are now empty (assigned_count = 0).
    #    Also discover orphaned workers not in the DB.
    try:
        empty_workers = conn.execute(
            "SELECT container_name FROM takeover_containers WHERE assigned_count <= 0"
        ).fetchall()
        for w in empty_workers:
            name = w["container_name"]
            try:
                subprocess.run(["docker", "rm", "-f", name],
                               capture_output=True, timeout=15)
                stats["workers_removed"] += 1
                log(f"cleanup_stress_tests: removed empty worker {name}")
            except Exception:
                pass
            conn.execute(
                "DELETE FROM takeover_containers WHERE container_name = ?",
                (name,)
            )
        db_commit_with_retry(conn)
    except sqlite3.OperationalError:
        pass

    # Also remove orphaned takeover containers not tracked in DB
    try:
        db_workers = set()
        try:
            rows = conn.execute("SELECT container_name FROM takeover_containers").fetchall()
            db_workers = set(r["container_name"] for r in rows)
        except sqlite3.OperationalError:
            pass

        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            for name in result.stdout.strip().splitlines():
                if name.startswith("onionheaven-takeover-") and name not in db_workers:
                    subprocess.run(["docker", "rm", "-f", name],
                                   capture_output=True, timeout=15)
                    stats["workers_removed"] += 1
                    log(f"cleanup_stress_tests: removed orphaned worker {name}")
    except Exception:
        pass

    if own_conn:
        conn.close()

    log(f"cleanup_stress_tests complete: {stats}")
    return stats


# ---------------------------------------------------------------------------
# Reset OnionHeaven — clean stress tests + refresh workers with current code
# ---------------------------------------------------------------------------

# Files to copy from the onionheaven container into takeover workers.
# These are the Python/shell files that workers execute.
_WORKER_CODE_FILES = [
    "/onionheaven_common.py",
    "/onionheaven-queue-manager.py",
    "/onionheaven-takeover-worker.py",
    "/onionheaven-tor-manager.sh",
    "/onionheaven-redirect.sh",
    "/onion_auth.py",
    "/key-convert.py",
]


def _patch_worker_code(container_name):
    """Copy current code files from this container into a takeover worker.

    Uses docker cp via the host socket. The onionheaven container is the
    source of truth — it has the latest code (either from the image or from
    docker cp during development).
    """
    # We're running inside the onionheaven container, so we copy from local
    # filesystem into the target container via `docker cp`.
    failed = []
    for fpath in _WORKER_CODE_FILES:
        if not os.path.isfile(fpath):
            continue
        try:
            result = subprocess.run(
                ["docker", "cp", fpath, f"{container_name}:{fpath}"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                failed.append(fpath)
        except Exception:
            failed.append(fpath)

    if failed:
        log(f"_patch_worker_code({container_name}): failed to copy: {failed}")
    else:
        log(f"_patch_worker_code({container_name}): patched {len(_WORKER_CODE_FILES)} files")


def reset_onionheaven(conn=None):
    """Clean stress tests and refresh all takeover workers with current code.

    Steps:
      1. Remove all stress test entries (registry rows, keys, takeovers)
      2. Collect real taken-over entries that need to be migrated
      3. Remove ALL old takeover workers
      4. Spawn fresh workers, patch them with current code, wait for bootstrap
      5. Re-assign real taken-over entries to the new workers

    Returns a dict summarising what happened.
    """
    global _next_worker_index

    own_conn = conn is None
    if own_conn:
        conn = db_connect()
        db_ensure_schema(conn)

    # Step 1: clean stress tests (this releases stress takeovers from workers)
    cleanup_stats = cleanup_stress_tests(conn)

    stats = {
        "stress_cleaned": cleanup_stats["registry_deleted"],
        "real_migrated": 0,
        "old_workers_removed": 0,
        "new_workers_spawned": 0,
    }

    # Step 2: collect real taken-over entries that need migration
    real_takeovers = conn.execute(
        "SELECT content_address, healthcheck_address, takeover_container "
        "FROM registry WHERE status = 'taken-over' AND unregistered_at IS NULL "
        "AND (version IS NULL OR version NOT LIKE 'stress-test%')"
    ).fetchall()

    # Step 3: remove ALL old takeover workers (DB + orphaned)
    old_workers = set()
    try:
        rows = conn.execute("SELECT container_name FROM takeover_containers").fetchall()
        old_workers = set(r["container_name"] for r in rows)
    except sqlite3.OperationalError:
        pass

    # Also find orphaned containers
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            for name in result.stdout.strip().splitlines():
                if name.startswith("onionheaven-takeover-"):
                    old_workers.add(name)
    except Exception:
        pass

    for name in old_workers:
        try:
            subprocess.run(["docker", "rm", "-f", name],
                           capture_output=True, timeout=15)
            stats["old_workers_removed"] += 1
            log(f"reset_onionheaven: removed old worker {name}")
        except Exception:
            pass

    # Clear takeover_containers table and reset index
    conn.execute("DELETE FROM takeover_containers")
    # Clear stale takeover_container assignments
    conn.execute(
        "UPDATE registry SET takeover_container = NULL "
        "WHERE status = 'taken-over' AND unregistered_at IS NULL"
    )
    db_commit_with_retry(conn)
    _next_worker_index = 0

    # If no real takeovers to migrate, we're done — heartbeat will spawn
    # workers on demand when new takeovers happen.
    if not real_takeovers:
        log(f"reset_onionheaven: no real takeovers to migrate")
        if own_conn:
            conn.close()
        log(f"reset_onionheaven complete: {stats}")
        return stats

    # Step 4: spawn fresh workers, patch code, wait for bootstrap
    # Figure out how many workers we need
    needed = (len(real_takeovers) + MAX_SERVICES_PER_WORKER - 1) // MAX_SERVICES_PER_WORKER
    needed = max(needed, 1)
    # Spawn in pairs (as _ensure_capacity does)
    to_spawn = needed + (needed % 2)

    new_worker_names = []
    for _ in range(to_spawn):
        idx = _next_worker_index
        _spawn_worker(conn)
        name = f"onionheaven-takeover-{idx}"
        # Check if it was actually created
        row = conn.execute(
            "SELECT container_name FROM takeover_containers WHERE container_name = ?",
            (name,)
        ).fetchone()
        if row:
            new_worker_names.append(name)
            stats["new_workers_spawned"] += 1

    # Patch code into new workers
    for name in new_worker_names:
        _patch_worker_code(name)

    # Wait for at least one worker to bootstrap (up to 90s)
    log(f"reset_onionheaven: waiting for {len(new_worker_names)} workers to bootstrap...")
    ready_worker = None
    for _ in range(18):  # 18 * 5s = 90s
        check_worker_bootstrap(conn)
        ready_worker = _pick_worker(conn)
        if ready_worker:
            break
        time.sleep(5)

    if not ready_worker:
        log("reset_onionheaven: WARNING — no workers bootstrapped in 90s, "
            "real takeovers will be re-assigned by heartbeat later")
        if own_conn:
            conn.close()
        log(f"reset_onionheaven complete: {stats}")
        return stats

    # Step 5: re-assign real takeovers to new workers
    for row in real_takeovers:
        ca = row["content_address"]
        ha = row["healthcheck_address"]
        worker = _pick_worker(conn)
        if not worker:
            log(f"reset_onionheaven: no worker available for {ca}, deferring to heartbeat")
            break
        if _exec_takeover(worker, ca):
            conn.execute(
                "UPDATE registry SET takeover_container = ? "
                "WHERE content_address = ? AND healthcheck_address = ?",
                (worker, ca, ha)
            )
            conn.execute(
                "UPDATE takeover_containers SET assigned_count = assigned_count + 1 "
                "WHERE container_name = ?",
                (worker,)
            )
            stats["real_migrated"] += 1
    db_commit_with_retry(conn)

    if own_conn:
        conn.close()

    log(f"reset_onionheaven complete: {stats}")
    return stats
