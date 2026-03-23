#!/usr/bin/env python3
"""
Multi-port HTTP server for onionheaven stress testing.

Replaces hundreds of individual socat processes with a single process.
Listens on a range of ports (2 per worker: content + healthcheck) and
serves simple HTTP responses. A control API on port 9000 allows the
stress test script to toggle individual ports on/off to simulate failures.

Usage:
    python3 worker-server.py <base_port> <num_workers>
    python3 worker-server.py 9100 50

Each worker i gets:
    content port:     base_port + i*2
    healthcheck port: base_port + i*2 + 1
"""

import asyncio
import json
import os
import subprocess
import sys

# Set of disabled ports (simulating failure)
disabled_ports = set()

# Stats
stats = {"requests": 0, "disabled_hits": 0, "healthy_hits": 0}

# Cached worker info (loaded from /worker-info.db on demand)
_worker_info = None


def _load_worker_info():
    global _worker_info
    try:
        import sqlite3
        conn = sqlite3.connect("/worker-data/worker-info.db", timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM workers").fetchall()
        conn.close()
        _worker_info = {row["local_index"]: dict(row) for row in rows}
    except Exception:
        _worker_info = {}
    return _worker_info


def _send_notify(worker_idx, endpoint, onionheaven_addr, stress_version=""):
    """Send a signed /offline or /online notification for a worker using this container's Tor."""
    import base64
    from onion_auth import sign_payload, make_timestamp

    _load_worker_info()
    w = _worker_info.get(worker_idx)
    if not w or not w.get("content_address"):
        return {"error": "no_info"}

    ca = w["content_address"]
    ha = w.get("healthcheck_address", "")
    pk = w.get("privkey_b64", "")
    pub = w.get("pubkey_b64", "")
    if not (pk and pub):
        return {"error": "no_keys"}

    privkey = base64.b64decode(pk)
    pubkey = base64.b64decode(pub)
    ts = make_timestamp()
    sig = sign_payload(privkey, pubkey, endpoint, ca, ha, ts)

    payload = {
        "content_address": ca,
        "healthcheck_address": ha,
        "timestamp": ts,
        "signature": sig,
    }
    if endpoint == "online":
        payload["arti_key_pem"] = w.get("arti_key_pem", "")
        payload["version"] = stress_version or os.environ.get("STRESS_VERSION", "stress-test")
        payload["wordpress_healthy"] = True

    global_idx = w.get("global_index", worker_idx)
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST",
             "--socks5-hostname", f"notify{global_idx}:x@127.0.0.1:9050",
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload),
             "--max-time", "30",
             f"http://{onionheaven_addr}:8083/{endpoint}"],
            capture_output=True, text=True, timeout=45,
        )
        try:
            return json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            return {"error": result.stdout[:200] if result.stdout else "empty"}
    except Exception as e:
        return {"error": str(e)}


def _ctor_control(cmd):
    """Send a command to C Tor's control port. Returns the raw response.

    Uses a Python socket instead of nc pipe so we can verify each step:
    1. AUTHENTICATE — must get 250 OK
    2. Send the actual command — must get a response
    3. QUIT
    """
    import socket as _socket

    try:
        cookie = open("/var/lib/tor/control_auth_cookie", "rb").read().hex()
    except Exception as e:
        return f"ERROR: cannot read cookie: {e}"

    try:
        s = _socket.create_connection(("127.0.0.1", 9051), timeout=10)
        s.settimeout(10)

        # Step 1: AUTHENTICATE
        s.sendall(f"AUTHENTICATE {cookie}\r\n".encode())
        auth_resp = _recv_response(s)
        if "250 OK" not in auth_resp:
            s.close()
            print(f"  CONTROL AUTH FAILED: {auth_resp.strip()}", flush=True)
            return f"AUTH_FAILED: {auth_resp}"

        # Step 2: Send the actual command
        s.sendall(f"{cmd}\r\n".encode())
        cmd_resp = _recv_response(s)

        # Step 3: QUIT
        try:
            s.sendall(b"QUIT\r\n")
            s.close()
        except Exception:
            pass

        return cmd_resp
    except Exception as e:
        return f"ERROR: {e}"


def _recv_response(sock):
    """Read a complete Tor control response (ends with 'NNN SP' line)."""
    data = b""
    while True:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            # Check if we have a final response line (3-digit code + space)
            lines = data.decode("utf-8", errors="replace").split("\r\n")
            for line in lines:
                if len(line) >= 4 and line[:3].isdigit() and line[3] == " ":
                    return data.decode("utf-8", errors="replace")
        except Exception:
            break
    return data.decode("utf-8", errors="replace")


async def handle_http(reader, writer, port):
    """Handle an HTTP request. Close without response if port is disabled."""
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=10)
        if not request_line:
            writer.close()
            return

        # Consume headers
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if line in (b"\r\n", b"\n", b""):
                break

        stats["requests"] += 1

        if port in disabled_ports:
            # Simulate failure: close without response → curl exit code 52
            stats["disabled_hits"] += 1
            writer.close()
            return

        stats["healthy_hits"] += 1
        body = b"<html><body>OK</body></html>"
        response = (
            f"HTTP/1.0 200 OK\r\n"
            f"Content-Type: text/html\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
        ).encode() + body
        writer.write(response)
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle_control(reader, writer):
    """Control API on port 9000."""
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=10)
        if not request_line:
            writer.close()
            return

        parts = request_line.decode(errors="replace").split()
        method = parts[0] if parts else "GET"
        path = parts[1] if len(parts) > 1 else "/"

        headers = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if line in (b"\r\n", b"\n", b""):
                break
            if b":" in line:
                key, val = line.decode(errors="replace").split(":", 1)
                headers[key.strip().lower()] = val.strip()

        body = b""
        cl = int(headers.get("content-length", 0))
        if cl > 0:
            body = await asyncio.wait_for(reader.readexactly(cl), timeout=10)

        if path == "/disable" and method == "POST":
            data = json.loads(body)
            ports = data.get("ports", [])
            disabled_ports.update(ports)
            resp = json.dumps({"ok": True, "disabled": sorted(disabled_ports)}).encode()
        elif path == "/enable" and method == "POST":
            data = json.loads(body)
            ports = data.get("ports", [])
            disabled_ports.difference_update(ports)
            resp = json.dumps({"ok": True, "disabled": sorted(disabled_ports)}).encode()
        elif path == "/del_onion" and method == "POST":
            data = json.loads(body)
            worker_indices = data.get("workers", [])
            _load_worker_info()
            results = {}
            for widx in worker_indices:
                w = _worker_info.get(widx)
                if not w or not w.get("content_address"):
                    results[str(widx)] = "no_info"
                    continue
                sid = w["content_address"].replace(".onion", "")
                out = await asyncio.get_event_loop().run_in_executor(
                    None, _ctor_control, f"DEL_ONION {sid}")
                results[str(widx)] = "ok" if "250 OK" in out else f"fail:{out[:80]}"
                # Also DEL healthcheck if it exists and is a real service
                hc = w.get("healthcheck_address", "")
                if hc and hc.endswith(".onion") and not hc.startswith("hc"):
                    hc_sid = hc.replace(".onion", "")
                    await asyncio.get_event_loop().run_in_executor(
                        None, _ctor_control, f"DEL_ONION {hc_sid}")
            resp = json.dumps({"ok": True, "results": results}).encode()
        elif path == "/add_onion" and method == "POST":
            data = json.loads(body)
            worker_indices = data.get("workers", [])
            _load_worker_info()
            results = {}
            for widx in worker_indices:
                w = _worker_info.get(widx)
                if not w or not w.get("ctor_key_b64"):
                    results[str(widx)] = "no_key"
                    continue
                cp = w.get("content_port", 9100 + widx * 2)
                key = w["ctor_key_b64"]
                out = await asyncio.get_event_loop().run_in_executor(
                    None, _ctor_control,
                    f"ADD_ONION ED25519-V3:{key} Flags=Detach Port=80,127.0.0.1:{cp}")
                ok = "250 OK" in out or "250-ServiceID=" in out
                results[str(widx)] = "ok" if ok else f"fail:{out[:80]}"
                # Also re-ADD healthcheck if it was a real service
                hc = w.get("healthcheck_address", "")
                hc_key = w.get("ctor_hc_key_b64", "")
                if hc_key and hc.endswith(".onion"):
                    hp = w.get("hc_port", cp + 1)
                    await asyncio.get_event_loop().run_in_executor(
                        None, _ctor_control,
                        f"ADD_ONION ED25519-V3:{hc_key} Flags=Detach Port=80,127.0.0.1:{hp}")
            resp = json.dumps({"ok": True, "results": results}).encode()
        elif path == "/notify" and method == "POST":
            data = json.loads(body)
            worker_indices = data.get("workers", [])
            endpoint = data.get("endpoint", "offline")
            onionheaven_addr = data.get("onionheaven_addr", "")
            stress_version = data.get("stress_version", "")
            results = {}
            for widx in worker_indices:
                r = await asyncio.get_event_loop().run_in_executor(
                    None, _send_notify, widx, endpoint, onionheaven_addr, stress_version)
                results[str(widx)] = r
            resp = json.dumps({"ok": True, "results": results}).encode()
        elif path == "/status":
            resp = json.dumps({
                "disabled_count": len(disabled_ports),
                "stats": stats,
            }).encode()
        else:
            writer.write(b"HTTP/1.0 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        writer.write(
            f"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(resp)}\r\n\r\n".encode()
            + resp
        )
        await writer.drain()
    except Exception as e:
        print(f"Control error: {e}", flush=True)
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def main():
    base_port = int(sys.argv[1]) if len(sys.argv) > 1 else 9100
    num_workers = int(sys.argv[2]) if len(sys.argv) > 2 else 50

    servers = []
    total_ports = num_workers * 2

    # Start a listener on each worker port
    for i in range(total_ports):
        port = base_port + i
        srv = await asyncio.start_server(
            lambda r, w, p=port: handle_http(r, w, p),
            "127.0.0.1", port,
        )
        servers.append(srv)

    # Control API
    control = await asyncio.start_server(handle_control, "0.0.0.0", 9000)
    servers.append(control)

    print(
        f"Worker server: {total_ports} ports ({base_port}-{base_port + total_ports - 1}) "
        f"+ control on 9000",
        flush=True,
    )

    await asyncio.gather(*(srv.serve_forever() for srv in servers))


if __name__ == "__main__":
    asyncio.run(main())
