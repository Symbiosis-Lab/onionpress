#!/usr/bin/env python3
"""
OnionPress Heartbeat Client (Linux)

Registers this OnionPress instance with an OnionHeaven hub and sends
periodic heartbeats. Runs as a systemd service.

Reads config from ~/.onionpress/config:
  REGISTER_WITH_ONIONHEAVEN=yes|no  (default: yes)
  ONIONHEAVEN_ADDRESS=<hub .onion>  (default: centralized OH)

Imports onion_auth and key_manager from /opt/onionpress/scripts/.
"""

import base64
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

# D-Bus + GLib are needed for the systemd-logind delay-inhibitor pattern
# (the proper Linux equivalent of macOS's NSWorkspaceWillSleepNotification).
# Both are usually pre-installed on Debian/Ubuntu desktop images; if missing
# the daemon still runs, just without synchronous /offline-on-sleep.
try:
    import dbus
    import dbus.mainloop.glib
    from gi.repository import GLib
    HAVE_DBUS = True
except ImportError:
    HAVE_DBUS = False

# Add scripts directory to path for onion_auth and key_manager imports
SCRIPTS_DIR = "/opt/onionpress/scripts"
sys.path.insert(0, SCRIPTS_DIR)

import key_manager
import onion_auth

# Defaults
DEFAULT_HUB = "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"
API_PORT = 8083
HEARTBEAT_INTERVAL = 60
READY_TIMEOUT = 300  # 5 min

# State
DATA_DIR = os.path.expanduser("~/.onionpress")
CONFIG_PATH = os.path.join(DATA_DIR, "config")
STATE_PATH = os.path.join(DATA_DIR, "onionheaven-registration.json")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(DATA_DIR, "onionpress.log")),
    ],
)
log = logging.getLogger("onionpress-heartbeat")

# Global for graceful shutdown
_current_hub = None
_content_addr = None
_hc_addr = None
_priv_key = None
_pub_key = None
_running = True
_registered = False  # Set True after first successful registration; read by SleepInhibitor.


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def read_config():
    """Parse KEY=VALUE config file. Returns dict."""
    config = {}
    try:
        with open(CONFIG_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return config


# ---------------------------------------------------------------------------
# Docker exec helper
# ---------------------------------------------------------------------------

def docker_exec(container, args, timeout=30):
    """Run a command inside a Docker container. Returns (success, stdout)."""
    cmd = ["docker", "exec", container] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout
        )
        if result.returncode != 0 and result.stderr.strip():
            log.debug("docker exec %s failed: %s", container, result.stderr.strip()[:200])
        return result.returncode == 0, result.stdout.strip()
    except subprocess.TimeoutExpired:
        log.warning("docker exec %s timed out after %ds", container, timeout)
        return False, ""
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Wait for readiness
# ---------------------------------------------------------------------------

def wait_for_ready():
    """Wait for content address, healthcheck address, and keys.
    Returns (content_addr, hc_addr, priv_key, pub_key) or None on timeout.
    """
    deadline = time.time() + READY_TIMEOUT
    backoff = 5

    while time.time() < deadline and _running:
        # Content address
        ok, content = docker_exec(
            "onionpress-tor",
            ["cat", "/var/lib/tor/hidden_service/wordpress/hostname"],
            timeout=10,
        )
        if not ok or not content.endswith(".onion"):
            log.info("Waiting for content address...")
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 30)
            continue

        # Healthcheck address
        ok, hc = docker_exec(
            "onionpress-tor",
            ["cat", "/var/lib/tor/hidden_service/healthcheck/hostname"],
            timeout=10,
        )
        if not ok or not hc.endswith(".onion"):
            log.info("Waiting for healthcheck address...")
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 30)
            continue

        # Keys
        try:
            priv_key, pub_key = key_manager.extract_keys()
        except Exception as e:
            log.info("Waiting for keys: %s", e)
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 30)
            continue

        return content.strip(), hc.strip(), priv_key, pub_key

    return None


# ---------------------------------------------------------------------------
# Signing + POST via tor-client
# ---------------------------------------------------------------------------

def sign_and_post(endpoint, hub_addr, content_addr, hc_addr, priv_key, pub_key,
                  extra=None, max_time=60, docker_timeout=75):
    """Sign payload and POST to hub via docker exec curl through tor-client.
    Returns (success, response_dict_or_None).

    max_time / docker_timeout default to the daemon's generous values. The
    --offline-once caller passes tight values so the suspend hook never
    overshoots its system-sleep budget.
    """
    timestamp = onion_auth.make_timestamp()
    signature = onion_auth.sign_payload(
        priv_key, pub_key,
        endpoint, content_addr, hc_addr, timestamp
    )

    payload = {
        "content_address": content_addr,
        "healthcheck_address": hc_addr,
        "timestamp": timestamp,
        "signature": signature,
    }
    if extra:
        payload.update(extra)

    payload_json = json.dumps(payload)

    ok, output = docker_exec(
        "onionpress-tor",
        [
            "curl", "-s", "-X", "POST",
            "--socks5-hostname", "127.0.0.1:9050",
            "-H", "Content-Type: application/json",
            "-d", payload_json,
            "--max-time", str(max_time),
            f"http://{hub_addr}:{API_PORT}/{endpoint}",
        ],
        timeout=docker_timeout,
    )

    if ok and output:
        try:
            return True, json.loads(output)
        except json.JSONDecodeError:
            log.warning("POST /%s: non-JSON response: %s", endpoint, output[:200])
            return False, None
    log.debug("POST /%s: ok=%s output=%r", endpoint, ok, output[:200] if output else "")
    return False, None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _is_onionheaven_server():
    """Check if this node also runs an OnionHeaven server (API on port 8083)."""
    ok, output = docker_exec(
        "onionpress-tor",
        ["test", "-f", "/var/lib/onionpress/onionheaven/activate"],
        timeout=5,
    )
    return ok


def register(hub_addr, content_addr, hc_addr, priv_key, pub_key):
    """Register with hub. Retries 4x with backoff. Returns True on success."""
    arti_pem = key_manager.build_openssh_key(priv_key, pub_key)
    extra = {
        "arti_key_pem": base64.b64encode(arti_pem).decode("ascii"),
        "version": _read_version(),
    }
    if _is_onionheaven_server():
        extra["is_onionheaven"] = True

    backoff_delays = [10, 30, 30]
    for attempt in range(4):
        log.info("Registration attempt %d/4 with %s...", attempt + 1, hub_addr)
        ok, resp = sign_and_post("online", hub_addr, content_addr, hc_addr, priv_key, pub_key, extra)

        if ok and resp:
            if resp.get("registered"):
                log.info("Registration successful: %s", resp)
                _save_state({
                    "registered": True,
                    "last_attempt": datetime.now(timezone.utc).isoformat(),
                    "onionheaven_address": hub_addr,
                    "content_address": content_addr,
                })
                return True
            error = resp.get("error", "unknown")
            log.error("Registration rejected: %s", error)
            return False

        if attempt < 3:
            delay = backoff_delays[attempt]
            log.info("Registration failed, retrying in %ds...", delay)
            time.sleep(delay)

    log.error("Registration failed after 4 attempts")
    _save_state({
        "registered": False,
        "last_attempt": datetime.now(timezone.utc).isoformat(),
        "onionheaven_address": hub_addr,
    })
    return False


# ---------------------------------------------------------------------------
# Unregister
# ---------------------------------------------------------------------------

def unregister(hub_addr, content_addr, hc_addr, priv_key, pub_key):
    """Unregister from hub. Retries 4x — critical to avoid false takeover."""
    backoff_delays = [5, 15, 30]
    for attempt in range(4):
        log.info("Unregister attempt %d/4 from %s...", attempt + 1, hub_addr)
        ok, resp = sign_and_post("unregister", hub_addr, content_addr, hc_addr, priv_key, pub_key)

        if ok and resp:
            if resp.get("unregistered"):
                log.info("Unregistered successfully from %s", hub_addr)
                _save_state({
                    "registered": False,
                    "unregistered_at": datetime.now(timezone.utc).isoformat(),
                    "onionheaven_address": hub_addr,
                    "content_address": content_addr,
                })
                return True
            error = resp.get("error", "unknown")
            log.error("Unregister rejected: %s", error)
            if resp.get("error"):
                return False

        if attempt < 3:
            delay = backoff_delays[min(attempt, len(backoff_delays) - 1)]
            log.info("Unregister failed, retrying in %ds...", delay)
            time.sleep(delay)

    log.error("Unregister failed after 4 attempts (best-effort)")
    return False


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def heartbeat(hub_addr, content_addr, hc_addr, priv_key, pub_key):
    """Send a single /online heartbeat. Returns True on success."""
    # Check WordPress health
    wp_healthy = False
    ok, output = docker_exec(
        "onionpress-wordpress",
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5", "http://localhost:80/"],
        timeout=10,
    )
    if ok and output.strip() in ("200", "301", "302"):
        wp_healthy = True

    log.debug("WP health check: ok=%s output=%r healthy=%s", ok, output, wp_healthy)

    # Always include key — every /online is both heartbeat and registration
    arti_pem = key_manager.build_openssh_key(priv_key, pub_key)
    extra = {
        "wordpress_healthy": wp_healthy,
        "arti_key_pem": base64.b64encode(arti_pem).decode("ascii"),
    }
    if _is_onionheaven_server():
        extra["is_onionheaven"] = True

    ok, resp = sign_and_post(
        "online", hub_addr, content_addr, hc_addr, priv_key, pub_key,
        extra=extra,
    )

    if ok and resp and resp.get("online"):
        log.info("Heartbeat OK (wp_healthy=%s)", wp_healthy)
        return True
    if ok and resp:
        log.warning("Heartbeat rejected: %s", resp.get("error", "unknown"))
    else:
        log.warning("Heartbeat failed (will retry next cycle)")
    return False


# ---------------------------------------------------------------------------
# Offline notification
# ---------------------------------------------------------------------------

def send_offline(hub_addr, content_addr, hc_addr, priv_key, pub_key):
    """Best-effort /offline notification."""
    try:
        ok, resp = sign_and_post("offline", hub_addr, content_addr, hc_addr, priv_key, pub_key)
        if ok and resp and resp.get("offline"):
            log.info("Sent /offline to %s", hub_addr)
        else:
            log.warning("Failed to send /offline to %s", hub_addr)
    except Exception as e:
        log.warning("Failed to send /offline: %s", e)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _save_state(data):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        log.warning("Failed to save state: %s", e)


def _load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _read_version():
    try:
        with open("/opt/onionpress/VERSION") as f:
            return f.read().strip()
    except OSError:
        return "unknown"


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------

def _signal_handler(signum, frame):
    global _running
    log.info("Received signal %d, shutting down...", signum)
    _running = False


# Pre-sleep (USR1) and post-wake (USR2) async kicks. The system-sleep hook
# (/usr/lib/systemd/system-sleep/onionpress) sends USR1 to flush /offline
# before suspend and USR2 after resume to send an immediate /online instead
# of waiting up to HEARTBEAT_INTERVAL. Signal handlers must not do network
# I/O directly (signal-safety), so they just flip flags and the main loop
# acts on them — time.sleep() is interrupted by the signal on Linux, so
# the loop wakes up immediately.
_pending_offline = False
_pending_online = False


def _usr1_handler(signum, frame):
    global _pending_offline
    log.info("Received SIGUSR1 — will flush /offline this cycle")
    _pending_offline = True


def _usr2_handler(signum, frame):
    global _pending_online
    log.info("Received SIGUSR2 — will send /online immediately")
    _pending_online = True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

class SleepInhibitor:
    """Hold a systemd-logind sleep delay inhibitor and use the
    PrepareForSleep signal to do a synchronous /offline POST before
    the kernel actually suspends.

    The Linux equivalent of macOS's NSWorkspaceWillSleepNotification:
    PrepareForSleep(true) is broadcast by logind before the kernel
    freezes processes, and the delay-type inhibitor keeps the suspend
    operation from completing until we release it (up to
    InhibitDelayMaxSec, default 5s, often configured higher).

    NetworkManager handles the same signal independently and starts
    tearing down the WiFi immediately; that race is inherent to the
    Linux desktop sleep flow. The inhibitor gives us a real window
    (vs. the system-sleep hook, which fires *after* NM teardown
    completes), and the existing Tor circuits to the hub often
    survive long enough for the POST to land.

    Falls back gracefully — if logind isn't reachable or the import
    failed, the daemon still runs, just without sync-on-sleep
    (heartbeat-timeout fallback path picks up after a few minutes).
    """

    def __init__(self):
        self._inhibitor_fd = None
        self._bus = None
        self._login_obj = None
        self._loop = None
        self._thread = None

    def start(self):
        if not HAVE_DBUS:
            log.warning("sleep-inhibitor: dbus/glib not importable, skipping")
            return False
        try:
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            self._bus = dbus.SystemBus()
            self._login_obj = self._bus.get_object(
                "org.freedesktop.login1", "/org/freedesktop/login1"
            )
            if not self._acquire():
                return False
            self._bus.add_signal_receiver(
                self._on_prepare_for_sleep,
                signal_name="PrepareForSleep",
                dbus_interface="org.freedesktop.login1.Manager",
                bus_name="org.freedesktop.login1",
                path="/org/freedesktop/login1",
            )
            self._loop = GLib.MainLoop()
            self._thread = threading.Thread(
                target=self._loop.run, daemon=True, name="sleep-inhibitor"
            )
            self._thread.start()
            log.info("sleep-inhibitor: watching logind PrepareForSleep")
            return True
        except Exception as e:
            log.warning("sleep-inhibitor: start failed: %s", e)
            return False

    def _acquire(self):
        if self._inhibitor_fd is not None:
            return True
        try:
            manager = dbus.Interface(
                self._login_obj, "org.freedesktop.login1.Manager"
            )
            fd_obj = manager.Inhibit(
                "sleep",
                "OnionPress",
                "Notify OnionHeaven hub before suspend",
                "delay",
            )
            self._inhibitor_fd = fd_obj.take()
            return True
        except Exception as e:
            log.warning("sleep-inhibitor: Inhibit() failed: %s", e)
            self._inhibitor_fd = None
            return False

    def _release(self):
        if self._inhibitor_fd is None:
            return
        try:
            os.close(self._inhibitor_fd)
        except OSError:
            pass
        self._inhibitor_fd = None

    def _on_prepare_for_sleep(self, going_to_sleep):
        if bool(going_to_sleep):
            log.info("sleep-inhibitor: PrepareForSleep(true) — sending /offline")
            try:
                self._send_offline_now()
            except Exception as e:
                log.warning("sleep-inhibitor: send_offline raised: %s", e)
            self._release()
            log.info("sleep-inhibitor: delay lock released, suspend can proceed")
        else:
            log.info("sleep-inhibitor: PrepareForSleep(false) — woke up")
            # Re-acquire the inhibitor for the next sleep cycle.
            self._acquire()
            # Trigger an immediate /online via the existing pending-flag
            # machinery so the daemon's heartbeat loop posts it on the
            # next wake — same path USR2 uses, no duplicate plumbing.
            global _pending_online
            _pending_online = True

    def _send_offline_now(self):
        """Best-effort synchronous /offline using whatever state the daemon
        has gathered. Uses tight timeouts so we don't burn through the
        whole InhibitDelayMaxSec budget if Tor is wedged."""
        if not _registered:
            log.info("sleep-inhibitor: not registered yet, skipping /offline")
            return
        if _content_addr is None or _content_addr == _current_hub:
            return
        if _hc_addr is None or _priv_key is None or _pub_key is None:
            log.info("sleep-inhibitor: state incomplete, skipping /offline")
            return
        ok, resp = sign_and_post(
            "offline", _current_hub, _content_addr, _hc_addr,
            _priv_key, _pub_key,
            max_time=4, docker_timeout=6,
        )
        if ok and resp and resp.get("offline"):
            log.info("sleep-inhibitor: /offline acknowledged by hub")
        else:
            log.warning("sleep-inhibitor: /offline POST failed (ok=%s)", ok)


def main():
    global _current_hub, _content_addr, _hc_addr, _priv_key, _pub_key, _running
    global _pending_offline, _pending_online

    os.makedirs(DATA_DIR, exist_ok=True)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGUSR1, _usr1_handler)
    signal.signal(signal.SIGUSR2, _usr2_handler)

    log.info("heartbeat client starting...")

    # Wait for addresses and keys
    result = wait_for_ready()
    if result is None:
        log.error("Timed out waiting for addresses/keys after %ds", READY_TIMEOUT)
        sys.exit(1)

    _content_addr, _hc_addr, _priv_key, _pub_key = result
    log.info("Content address: %s", _content_addr)
    log.info("Healthcheck address: %s", _hc_addr)

    # Read initial config
    config = read_config()
    enabled = config.get("REGISTER_WITH_ONIONHEAVEN", "yes").lower() != "no"
    _current_hub = config.get("ONIONHEAVEN_ADDRESS", DEFAULT_HUB)

    # Self-registration check: if our address IS the hub, skip all client activity
    if _content_addr == _current_hub:
        log.info("This node IS the OnionHeaven hub (%s) — skipping client registration", _current_hub)
        # Sleep forever (or until signal) — the server container handles everything
        while _running:
            time.sleep(60)
        return

    global _registered
    _registered = False

    if enabled:
        _registered = register(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)
    else:
        log.info("Registration disabled (REGISTER_WITH_ONIONHEAVEN=no)")

    # Start the logind sleep-inhibitor now that we have keys + a hub.
    # The inhibitor sends /offline synchronously on PrepareForSleep(true)
    # — the only Linux mechanism that beats NetworkManager's sleep
    # teardown reliably. See SleepInhibitor docstring above.
    sleep_inhibitor = SleepInhibitor()
    sleep_inhibitor.start()

    # Heartbeat loop. Uses the _registered global so the sleep-inhibitor
    # thread sees the current registration state without needing locks
    # (single-writer/single-reader, value semantics on a bool).
    while _running:
        time.sleep(HEARTBEAT_INTERVAL)
        if not _running:
            break

        # Async sleep/wake kicks from the system-sleep hook. USR1 means the
        # laptop is about to suspend — flush /offline so the hub takes over
        # without waiting for missed heartbeats to time out. USR2 means we
        # just woke — send /online right now instead of waiting up to a
        # full HEARTBEAT_INTERVAL for the regular cycle to fire.
        if _pending_offline and _registered and _content_addr != _current_hub:
            _pending_offline = False
            log.info("USR1: sending /offline before suspend...")
            send_offline(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)
        elif _pending_offline:
            _pending_offline = False  # nothing to do, swallow the flag
        if _pending_online:
            _pending_online = False
            if _registered and _content_addr != _current_hub:
                log.info("USR2: sending immediate /online after wake")
                heartbeat(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)

        # Re-read config each cycle
        config = read_config()
        new_enabled = config.get("REGISTER_WITH_ONIONHEAVEN", "yes").lower() != "no"
        new_hub = config.get("ONIONHEAVEN_ADDRESS", DEFAULT_HUB)

        # Handle hub address change
        if new_hub != _current_hub and _registered:
            log.info("Hub address changed from %s to %s", _current_hub, new_hub)
            # Unregister from old hub to prevent false takeover
            unregister(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)
            _registered = False
            _current_hub = new_hub
            if new_enabled:
                # Self-check for new hub
                if _content_addr == _current_hub:
                    log.info("New hub is this node — stopping client activity")
                    while _running:
                        time.sleep(60)
                    break
                _registered = register(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)
            continue
        _current_hub = new_hub

        # Handle enable/disable toggle
        if not new_enabled and _registered:
            log.info("Registration disabled — sending /offline")
            send_offline(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)
            _registered = False
            enabled = False
            continue

        if new_enabled and not enabled and not _registered:
            log.info("Registration re-enabled — registering")
            if _content_addr == _current_hub:
                log.info("This node IS the hub — skipping")
                enabled = True
                continue
            _registered = register(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)
            enabled = True
            continue

        enabled = new_enabled

        # Retry registration if not yet registered
        if enabled and not _registered:
            if _content_addr != _current_hub:
                log.info("Retrying registration with %s...", _current_hub)
                _registered = register(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)
            continue

        # Send heartbeat if registered
        if _registered:
            heartbeat(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)

    # Graceful shutdown: send /offline
    if _registered and _current_hub:
        log.info("Sending /offline before shutdown...")
        send_offline(_current_hub, _content_addr, _hc_addr, _priv_key, _pub_key)

    log.info("heartbeat client stopped")


def _cached_content_address():
    """Read content address from the registration state file (instant)."""
    try:
        with open(STATE_PATH) as f:
            return (json.load(f).get("content_address") or "").strip()
    except (OSError, json.JSONDecodeError):
        return ""


def _cached_hc_address():
    """Read healthcheck address from ~/.onionpress/healthcheck-address (instant)."""
    try:
        with open(os.path.join(DATA_DIR, "healthcheck-address")) as f:
            return f.read().strip()
    except OSError:
        return ""


def offline_once():
    """One-shot synchronous /offline POST for the system-sleep hook.

    The long-running daemon receives SIGUSR1 from the hook and sets a
    flag, but the actual POST happens later in the main loop — by which
    time systemd-logind has often already proceeded with the suspend
    (it only waits for the hook to return, not for the heartbeat process
    to finish its async work). This entry point runs in the hook's own
    process so the hook can't return until the /offline POST has been
    attempted.

    Reads cached addresses from disk to avoid extra docker exec calls
    on the hot path — the daemon writes the registration state file
    and the healthcheck-address file during normal operation, so by
    the time the user can suspend the box those files are populated.
    Only the key extraction needs a docker exec, and the curl POST
    itself.
    """
    # Drop the StreamHandler the daemon installs — when invoked by the
    # launcher's sleep-pre our stderr is already redirected into the
    # same onionpress.log file the FileHandler writes to, so each line
    # would land twice without this.
    root = logging.getLogger()
    for h in list(root.handlers):
        if type(h) is logging.StreamHandler:
            root.removeHandler(h)

    config = read_config()
    if config.get("REGISTER_WITH_ONIONHEAVEN", "yes").lower() == "no":
        return 0
    hub = config.get("ONIONHEAVEN_ADDRESS", DEFAULT_HUB)

    content = _cached_content_address()
    if not content.endswith(".onion"):
        # Fall back to container hostname file with a generous timeout —
        # docker exec on rootless docker can take a few seconds when the
        # box is preparing to suspend.
        ok, content = docker_exec(
            "onionpress-tor",
            ["cat", "/var/lib/tor/hidden_service/wordpress/hostname"],
            timeout=5,
        )
        content = content.strip() if ok else ""
        if not content.endswith(".onion"):
            log.warning("offline-once: content address unavailable, skipping /offline")
            return 1
    if content == hub:
        return 0  # We ARE the hub — nothing to notify.

    hc = _cached_hc_address()
    if not hc.endswith(".onion"):
        ok, hc = docker_exec(
            "onionpress-tor",
            ["cat", "/var/lib/tor/hidden_service/healthcheck/hostname"],
            timeout=5,
        )
        hc = hc.strip() if ok else ""
        if not hc.endswith(".onion"):
            log.warning("offline-once: healthcheck address unavailable, skipping /offline")
            return 1

    try:
        priv_key, pub_key = key_manager.extract_keys()
    except Exception as e:
        log.warning("offline-once: keys unavailable: %s", e)
        return 1

    log.info("offline-once: posting /offline to %s", hub)
    ok, resp = sign_and_post(
        "offline", hub, content, hc, priv_key, pub_key,
        max_time=4, docker_timeout=6,
    )
    if ok and resp and resp.get("offline"):
        log.info("offline-once: /offline acknowledged by hub")
        return 0
    log.warning("offline-once: /offline POST failed (ok=%s resp=%s)", ok, resp)
    return 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--offline-once":
        sys.exit(offline_once())
    main()
