#!/usr/bin/env python3
"""
Event-driven onion service verification worker.

Runs inside a poll client container. Subscribes to HS_DESC events via the
Tor control port, tracks per-address descriptor state, and verifies
reachability with targeted curl checks (only when a descriptor is received).

Replaces the bash polling loop (parallel_check_addrs every 5s on all
addresses) with an event-driven approach that generates far less traffic.

Usage:
    python3 verify-worker.py <expected_code> <address1> <address2> ...

    expected_code: "200" or "302"

Output:
    Writes JSON results to /tmp/verify-results.json, updated continuously.
    The orchestrator reads this file to check progress.

    {"verified": 3, "total": 5, "pending": ["addr1", "addr2"],
     "results": {"addr1": {"code": 200, "time": 12.3}, ...}}
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time

CONTROL_PORT = 9051
SOCKS_PORT = 9050
RESULTS_FILE = "/tmp/verify-results.json"
MAX_CURL_CONCURRENT = 5


def read_cookie():
    """Read Tor control port cookie."""
    try:
        with open("/var/lib/tor/control_auth_cookie", "rb") as f:
            return f.read().hex()
    except Exception:
        return None


def control_cmd(cmd):
    """Send a command to Tor control port, return response lines."""
    cookie = read_cookie()
    if not cookie:
        return []
    try:
        s = socket.create_connection(("127.0.0.1", CONTROL_PORT), timeout=10)
        s.sendall(f"AUTHENTICATE {cookie}\r\n{cmd}\r\nQUIT\r\n".encode())
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        return data.decode(errors="replace").splitlines()
    except Exception:
        return []


def hsfetch(service_id):
    """Issue HSFETCH for a service."""
    control_cmd(f"HSFETCH {service_id}")


def newnym():
    """Issue SIGNAL NEWNYM to clear descriptor cache."""
    control_cmd("SIGNAL NEWNYM")


_addr_generation: dict[str, tuple[int, float]] = {}  # addr -> (generation, last_rotate_time)
_ROTATE_INTERVAL = 120  # seconds between SOCKS credential rotations per address

def curl_check(addr, expected_code, timeout=15):
    """Check if an address returns the expected HTTP code.

    Rotates SOCKS credentials per-address every 2 minutes so Tor builds
    a fresh circuit periodically — prevents stale descriptor cache from
    causing permanent 000s without overwhelming the SOCKS proxy.
    """
    import time
    now = time.time()
    # Offset rotation per-address so they don't all rotate at once
    offset = (hash(addr) % 60)
    gen, last_rotate = _addr_generation.get(addr, (0, now + offset))
    if now - last_rotate >= _ROTATE_INTERVAL:
        gen += 1
        last_rotate = now
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"  [{ts}] ROTATE {addr} gen={gen} (stale circuit after {_ROTATE_INTERVAL}s)", flush=True)
    _addr_generation[addr] = (gen, last_rotate)
    try:
        result = subprocess.run(
            ["curl", "-s", "--http1.0",
             "--socks5-hostname", f"v_{os.getpid()}_{id(addr)}_{gen}:x@127.0.0.1:{SOCKS_PORT}",
             "--max-time", str(timeout),
             "-o", "/dev/null", "-w", "%{http_code}",
             f"http://{addr}/"],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        return result.stdout.strip()
    except Exception:
        return "000"


class VerifyWorker:
    def __init__(self, expected_code, addresses):
        self.expected_code = expected_code
        self.addresses = addresses
        self.service_ids = {a.replace(".onion", ""): a for a in addresses}
        self.results = {}  # addr -> {"code": str, "time": float, "verified": bool}
        self.pending = set(addresses)
        self.desc_received = set()  # service_ids that have received descriptors
        self.lock = threading.Lock()
        self.start_time = time.time()

    def write_results(self):
        """Write current state to results file."""
        with self.lock:
            verified = sum(1 for r in self.results.values() if r.get("verified"))
            data = {
                "verified": verified,
                "total": len(self.addresses),
                "pending": sorted(self.pending),
                "elapsed": round(time.time() - self.start_time, 1),
                "results": dict(self.results),
            }
        with open(RESULTS_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def subscribe_hsdesc(self):
        """Subscribe to HS_DESC events in a background thread."""
        cookie = read_cookie()
        if not cookie:
            print("No control cookie, skipping HS_DESC subscription", flush=True)
            return

        def listener():
            try:
                s = socket.create_connection(("127.0.0.1", CONTROL_PORT), timeout=10)
                s.sendall(f"AUTHENTICATE {cookie}\r\nSETEVENTS HS_DESC\r\n".encode())
                s.settimeout(None)  # block forever
                buf = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\r\n" in buf:
                        line, buf = buf.split(b"\r\n", 1)
                        self.handle_event(line.decode(errors="replace"))
            except Exception as e:
                print(f"HS_DESC listener error: {e}", flush=True)

        t = threading.Thread(target=listener, daemon=True)
        t.start()

    def handle_event(self, line):
        """Handle a Tor control port event line."""
        # 650 HS_DESC RECEIVED <service_id> ...
        # 650 HS_DESC FAILED <service_id> ...
        if not line.startswith("650 HS_DESC"):
            return
        parts = line.split()
        if len(parts) < 4:
            return
        action = parts[2]
        service_id = parts[3]

        if service_id in self.service_ids:
            addr = self.service_ids[service_id]
            if action == "RECEIVED":
                with self.lock:
                    self.desc_received.add(service_id)
                # Descriptor available — trigger verification
                self.verify_address(addr)
            elif action == "FAILED":
                print(f"  HS_DESC FAILED for {addr}", flush=True)

    def verify_address(self, addr):
        """Verify a single address (called when descriptor is received)."""
        with self.lock:
            if addr not in self.pending:
                return

        code = curl_check(addr, self.expected_code)

        with self.lock:
            # Never overwrite a verified result — another thread may have
            # verified this address while our curl was in flight.
            existing = self.results.get(addr, {})
            if existing.get("verified"):
                return
            if code == self.expected_code:
                elapsed = round(time.time() - self.start_time, 1)
                self.results[addr] = {"code": code, "time": elapsed, "verified": True}
                self.pending.discard(addr)
                verified = sum(1 for r in self.results.values() if r.get("verified"))
                print(f"  VERIFIED {addr} → {code} ({elapsed}s) [{verified}/{len(self.addresses)}]", flush=True)
            else:
                self.results[addr] = {"code": code, "time": round(time.time() - self.start_time, 1), "verified": False}

        self.write_results()

    def run(self, timeout=600):
        """Main verification loop."""
        print(f"Verify worker: {len(self.addresses)} addresses, expect {self.expected_code}, timeout {timeout}s", flush=True)

        # Subscribe to HS_DESC events
        self.subscribe_hsdesc()
        time.sleep(1)

        # Initial NEWNYM + HSFETCH for all addresses
        newnym()
        time.sleep(3)
        for sid in self.service_ids:
            hsfetch(sid)

        deadline = time.time() + timeout
        last_hsfetch = time.time()

        while time.time() < deadline:
            # Check if all verified
            with self.lock:
                if not self.pending:
                    print(f"All {len(self.addresses)} addresses verified!", flush=True)
                    self.write_results()
                    return 0

            # Periodic HSFETCH for remaining addresses (every 30s)
            # No NEWNYM here — it kills in-flight circuits and prevents
            # descriptor fetches from completing. The initial NEWNYM at
            # startup is sufficient to clear stale cache.
            now = time.time()
            if now - last_hsfetch >= 30:
                with self.lock:
                    remaining_sids = [sid for sid, addr in self.service_ids.items() if addr in self.pending]
                for sid in remaining_sids:
                    hsfetch(sid)
                last_hsfetch = now

            # Also try direct curl for pending addresses that have descriptors
            with self.lock:
                to_check = [self.service_ids[sid] for sid in self.desc_received
                           if self.service_ids[sid] in self.pending]

            for addr in to_check[:MAX_CURL_CONCURRENT]:
                self.verify_address(addr)

            self.write_results()
            time.sleep(5)

        # Timed out
        with self.lock:
            verified = sum(1 for r in self.results.values() if r.get("verified"))
        print(f"Timed out: {verified}/{len(self.addresses)} verified", flush=True)
        self.write_results()
        return 1


def main():
    if len(sys.argv) < 3:
        print("Usage: verify-worker.py <expected_code> [--timeout N] <addr1> [addr2] ...", file=sys.stderr)
        sys.exit(1)

    args = sys.argv[1:]
    expected_code = args.pop(0)

    timeout = 600
    if args and args[0] == "--timeout":
        args.pop(0)
        timeout = int(args.pop(0))

    addresses = args

    worker = VerifyWorker(expected_code, addresses)
    sys.exit(worker.run(timeout=timeout))


if __name__ == "__main__":
    main()
