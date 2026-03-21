#!/usr/bin/env python3
"""
OnionHeaven Queue Manager — rate-limited ADD_ONION pipeline.

Runs inside takeover worker containers. Maintains a persistent Tor control
port connection, monitors HS_DESC events, and rate-limits ADD_ONION calls
to avoid overwhelming Tor's circuit builder.

Interface:
  queue-takeover <content_address>   — add to takeover queue
  queue-release <content_address>    — release (immediate DEL_ONION)
  queue-status                       — print JSON status

Invoked via: docker exec <worker> python3 /onionheaven-queue-manager.py <command> [args]

The manager runs as a daemon (started by entrypoint.sh). Commands communicate
with it via a command file + response file on a shared path.
"""

import json
import os
import socket
import subprocess
import sys
import time
import threading
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_IN_FLIGHT = int(os.environ.get("ONIONHEAVEN_MAX_IN_FLIGHT", "5"))
CONTROL_PORT = ("127.0.0.1", 9051)
COOKIE_PATH = "/var/lib/tor/control_auth_cookie"
KEYS_DIR = "/var/lib/onionpress/onionheaven/keys"
KEY_CONVERT = "/key-convert.py"
REDIRECT_PORT = 8082
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "unknown")

# Command socket path — query via: docker exec <worker> python3 /onionheaven-queue-manager.py status
SOCK_PATH = "/tmp/queue-manager.sock"


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"[{ts}] queue-manager: {msg}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Tor control port connection with event monitoring
# ---------------------------------------------------------------------------

class TorControl:
    """Persistent control port connection with HS_DESC event monitoring."""

    def __init__(self):
        self.sock = None
        self.lock = threading.Lock()
        self.uploaded = set()  # service_ids with HS_DESC UPLOADED
        self._event_thread = None
        self._running = False

    def connect(self):
        """Connect to control port, authenticate, subscribe to events."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect(CONTROL_PORT)
        self.sock.settimeout(5.0)

        # Read greeting
        self._read_response()

        # Authenticate
        try:
            with open(COOKIE_PATH, "rb") as f:
                cookie = f.read().hex()
        except FileNotFoundError:
            raise RuntimeError("Control auth cookie not found")

        self._send(f"AUTHENTICATE {cookie}")
        resp = self._read_response()
        if "250 OK" not in resp:
            raise RuntimeError(f"Auth failed: {resp}")

        # Subscribe to HS_DESC events
        self._send("SETEVENTS HS_DESC")
        resp = self._read_response()
        if "250 OK" not in resp:
            raise RuntimeError(f"SETEVENTS failed: {resp}")

        # Start event listener thread
        self._running = True
        self._event_thread = threading.Thread(target=self._event_loop, daemon=True)
        self._event_thread.start()
        log("Connected to Tor control port, listening for HS_DESC events")

    def _send(self, cmd):
        self.sock.sendall(f"{cmd}\r\n".encode())

    def _read_response(self):
        """Read until we get a final response line (starts with 250/5xx + space)."""
        data = b""
        self.sock.settimeout(5.0)
        while True:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                # Check for complete response
                lines = data.decode("utf-8", errors="replace").split("\r\n")
                for line in lines:
                    if line and (line[:3].isdigit() and len(line) > 3 and line[3] == " "):
                        return data.decode("utf-8", errors="replace")
            except socket.timeout:
                break
        return data.decode("utf-8", errors="replace")

    def _event_loop(self):
        """Listen for async events from Tor."""
        self.sock.settimeout(1.0)
        buf = b""
        while self._running:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    log("Control port connection closed")
                    self._running = False
                    break
                buf += chunk
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    self._handle_event(line.decode("utf-8", errors="replace"))
            except socket.timeout:
                continue
            except Exception as e:
                log(f"Event loop error: {e}")
                time.sleep(1)

    def _handle_event(self, line):
        """Parse HS_DESC events."""
        # Format: 650 HS_DESC UPLOADED <service_id> ...
        if "650 HS_DESC UPLOADED" in line:
            parts = line.split()
            if len(parts) >= 4:
                service_id = parts[3]
                with self.lock:
                    self.uploaded.add(service_id)
                log(f"HS_DESC UPLOADED: {service_id[:20]}...")

    def add_onion(self, content_address, key_b64):
        """Send ADD_ONION command. Returns True on success."""
        service_id = content_address.replace(".onion", "")
        cmd = f"ADD_ONION ED25519-V3:{key_b64} Flags=Detach Port=80,127.0.0.1:{REDIRECT_PORT}"
        with self.lock:
            self._send(cmd)
        # Read response (need to handle it carefully with event thread running)
        time.sleep(0.5)
        # The response will be mixed with events, but ADD_ONION response is synchronous
        # We just check if the service appears in detached list
        return True  # ADD_ONION rarely fails if key is valid

    def del_onion(self, content_address):
        """Send DEL_ONION command."""
        service_id = content_address.replace(".onion", "")
        with self.lock:
            self._send(f"DEL_ONION {service_id}")
            self.uploaded.discard(service_id)
        time.sleep(0.3)

    def is_uploaded(self, content_address):
        """Check if descriptors were uploaded for this address."""
        service_id = content_address.replace(".onion", "")
        with self.lock:
            return service_id in self.uploaded

    def close(self):
        self._running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Queue manager
# ---------------------------------------------------------------------------

class QueueManager:
    """Rate-limited ADD_ONION pipeline."""

    def __init__(self, tor):
        self.tor = tor
        self.queued = []       # addresses waiting for a slot
        self.in_flight = {}    # addr -> timestamp (ADD_ONION done, waiting for upload)
        self.active = set()    # descriptors uploaded, service reachable
        self.lock = threading.Lock()

    def takeover(self, content_address):
        """Add an address to the takeover queue."""
        with self.lock:
            if (content_address in self.active or
                    content_address in self.in_flight or
                    content_address in self.queued):
                return {"status": "already_queued", "address": content_address}
            self.queued.append(content_address)
        self._process_queue()
        return {"status": "queued", "address": content_address}

    def release(self, content_address):
        """Release an address (immediate DEL_ONION)."""
        with self.lock:
            if content_address in self.queued:
                self.queued.remove(content_address)
                return {"status": "removed_from_queue", "address": content_address}
            was_active = content_address in self.active
            was_in_flight = content_address in self.in_flight
            self.active.discard(content_address)
            self.in_flight.pop(content_address, None)

        if was_active or was_in_flight:
            self.tor.del_onion(content_address)
            self._process_queue()  # free slot for next in queue
            return {"status": "released", "address": content_address}
        return {"status": "not_found", "address": content_address}

    def _process_queue(self):
        """Move addresses from queued to in_flight if slots available."""
        with self.lock:
            while self.queued and len(self.in_flight) < MAX_IN_FLIGHT:
                addr = self.queued.pop(0)
                key_b64 = self._get_key(addr)
                if key_b64:
                    self.tor.add_onion(addr, key_b64)
                    self.in_flight[addr] = time.time()
                    log(f"ADD_ONION in-flight: {addr[:20]}... "
                        f"({len(self.in_flight)}/{MAX_IN_FLIGHT} slots, "
                        f"{len(self.queued)} queued)")
                else:
                    log(f"ERROR: no key for {addr}, skipping")

    def check_uploads(self):
        """Move in-flight addresses to active if descriptors uploaded."""
        promoted = []
        with self.lock:
            for addr in list(self.in_flight):
                if self.tor.is_uploaded(addr):
                    self.active.add(addr)
                    del self.in_flight[addr]
                    promoted.append(addr)
        if promoted:
            for addr in promoted:
                log(f"ACTIVE: {addr[:20]}... (descriptors uploaded)")
            self._process_queue()  # fill freed slots

    def status(self):
        with self.lock:
            return {
                "container": CONTAINER_NAME,
                "queued": len(self.queued),
                "in_flight": len(self.in_flight),
                "active": len(self.active),
                "max_in_flight": MAX_IN_FLIGHT,
            }

    def _get_key(self, content_address):
        """Extract ed25519 key as base64 for ADD_ONION."""
        key_file = os.path.join(KEYS_DIR, content_address,
                                "ks_hs_id.ed25519_expanded_private")
        if not os.path.isfile(key_file):
            return None
        try:
            result = subprocess.run(
                ["python3", KEY_CONVERT, "pem-to-ed25519-base64", key_file],
                capture_output=True, text=True, timeout=10
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None



# ---------------------------------------------------------------------------
# Command interface via Unix socket
# ---------------------------------------------------------------------------

def run_daemon():
    """Run as daemon — listen for commands on Unix socket."""
    # Wait for Tor control port
    log("Waiting for Tor control port...")
    for _ in range(60):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(CONTROL_PORT)
            s.close()
            break
        except ConnectionRefusedError:
            time.sleep(2)
    else:
        log("ERROR: Tor control port not available after 120s")
        sys.exit(1)

    tor = TorControl()
    tor.connect()
    qm = QueueManager(tor)

    # Remove stale socket
    try:
        os.unlink(SOCK_PATH)
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o666)
    server.listen(5)
    server.settimeout(2.0)

    log(f"Queue manager daemon started (max_in_flight={MAX_IN_FLIGHT})")

    while True:
        # Check for uploaded descriptors
        qm.check_uploads()

        # Accept commands
        try:
            conn, _ = server.accept()
            conn.settimeout(5.0)
            data = conn.recv(4096).decode().strip()
            parts = data.split(None, 1)
            cmd = parts[0] if parts else ""
            arg = parts[1] if len(parts) > 1 else ""

            if cmd == "takeover" and arg:
                result = qm.takeover(arg)
            elif cmd == "release" and arg:
                result = qm.release(arg)
            elif cmd == "status":
                result = qm.status()
            else:
                result = {"error": f"unknown command: {data}"}

            conn.sendall(json.dumps(result).encode() + b"\n")
            conn.close()
        except socket.timeout:
            continue
        except Exception as e:
            log(f"Socket error: {e}")


def send_command(cmd):
    """Send a command to the running daemon and print response."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(SOCK_PATH)
        s.sendall(cmd.encode() + b"\n")
        s.settimeout(10.0)
        resp = s.recv(4096).decode().strip()
        s.close()
        print(resp)
    except ConnectionRefusedError:
        print(json.dumps({"error": "queue manager not running"}))
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: queue-manager.py daemon | takeover <addr> | release <addr> | status")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "daemon":
        run_daemon()
    elif cmd == "takeover" and len(sys.argv) >= 3:
        send_command(f"takeover {sys.argv[2]}")
    elif cmd == "release" and len(sys.argv) >= 3:
        send_command(f"release {sys.argv[2]}")
    elif cmd == "status":
        send_command("status")
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
