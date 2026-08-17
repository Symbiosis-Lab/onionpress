"""Veee tunnel triage — FORK-ONLY module (self-healing-design.md §3.4).

Never part of the upstream PR. This deployment routes tor through an
upstream SOCKS proxy carried by a GUI VPN (Veee) via a launchd-managed
socat tunnel; upstream OnionPress has no such layer. The self-heal
supervisor's H1 rung therefore lives here, activated ONLY by config keys
in ``~/.onionpress/config``::

    TUNNEL_LAUNCHD_LABEL=com.onionpress.veee-tunnel
    TUNNEL_PROXY_ADDR=127.0.0.1:15235

Absent keys → :meth:`TunnelTriage.from_config` returns ``None`` → the
supervisor silently skips the rung, which is exactly the upstream shape.

Diagnosis-first (the 2026-08-16 lesson — the tunnel was the sick layer
while nine hours of tor restarts changed nothing):

* **host leg** — from the Mac: TCP + SOCKS5 greeting to the tunnel's
  local proxy, then a CONNECT to a bridge destination parsed from
  ``TOR_BRIDGE_LINES``. Dead ⇒ Veee itself (or its path to the bridge)
  is down; nothing automated can fix a GUI VPN, so the supervisor skips
  all restart rungs and notifies instead of thrashing.
* **container leg** — ``docker exec onionpress-tor`` running a stdlib
  probe against the *configured* ``TOR_UPSTREAM_PROXY`` value (172.19 is
  not guaranteed). Host leg OK + container leg dead ⇒ the socat/launchd
  layer is wedged; ``launchctl kickstart -k`` is the same relaunch the
  plist's KeepAlive performs on crash. The greeting requires a full
  relay round-trip, so the incident's half-alive socat (accepts TCP,
  then Broken pipe) reads as dead — which it is.
"""

import os
import re
import socket
import subprocess
from typing import Optional, Tuple

from .config import read_value

PROBE_TIMEOUT = 10
# How long the relaunched tunnel gets before the post-kick re-probe.
KICK_SETTLE_SECONDS = 20

_HOSTPORT_RE = re.compile(r"^(?:[a-z0-9+]+://)?([A-Za-z0-9_.-]+):(\d{1,5})$")
_IPV4_PORT_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})$")


def _parse_hostport(value: str) -> Optional[Tuple[str, int]]:
    """``host:port`` or ``scheme://host:port`` → (host, port), else None."""
    m = _HOSTPORT_RE.match((value or "").strip())
    if not m:
        return None
    port = int(m.group(2))
    if not 0 < port < 65536:
        return None
    return m.group(1), port


def _first_bridge_dest(bridge_lines: str) -> Optional[Tuple[str, int]]:
    """First ``ip:port`` in TOR_BRIDGE_LINES (``;``-joined entries,
    optional ``Bridge `` prefix, form ``[transport] ip:port fp [args]``)."""
    for entry in (bridge_lines or "").split(";"):
        for token in entry.strip().split():
            m = _IPV4_PORT_RE.match(token)
            if m:
                port = int(m.group(2))
                if 0 < port < 65536:
                    return m.group(1), port
    return None


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return buf
        buf += chunk
    return buf


# Runs INSIDE onionpress-tor (python3 is present — the watchdog runs on
# it). Prints TUNNEL-OK / TUNNEL-FAIL <stage>; never raises.
_CONTAINER_PROBE = r"""
import socket, struct, sys
host, port = {host!r}, {port}
dest = {dest!r}
timeout = {timeout}
try:
    s = socket.create_connection((host, port), timeout=timeout)
except OSError as e:
    print("TUNNEL-FAIL connect", e); sys.exit(0)
try:
    s.settimeout(timeout)
    s.sendall(b"\x05\x01\x00")
    r = s.recv(2)
    if r != b"\x05\x00":
        print("TUNNEL-FAIL greeting", r); sys.exit(0)
    if dest:
        ip, dport = dest
        s.sendall(b"\x05\x01\x00\x01" + socket.inet_aton(ip)
                  + struct.pack(">H", dport))
        r = s.recv(4)
        if len(r) < 2 or r[1] != 0:
            print("TUNNEL-FAIL connect-rep", r); sys.exit(0)
    print("TUNNEL-OK")
except OSError as e:
    print("TUNNEL-FAIL io", e)
finally:
    try:
        s.close()
    except OSError:
        pass
"""


class TunnelTriage:
    """H1 rung: probe both tunnel legs; kick the launchd tunnel service."""

    def __init__(self, *, label, proxy_host, proxy_port, bridge_dest,
                 docker, upstream_proxy, log_func=None,
                 run_func=subprocess.run, probe_timeout=PROBE_TIMEOUT):
        self.label = label
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.bridge_dest = bridge_dest          # (ip, port) or None
        self.upstream_proxy = upstream_proxy    # raw TOR_UPSTREAM_PROXY value
        self._docker = docker
        self._log = log_func or (lambda msg: None)
        self._run = run_func
        self._timeout = probe_timeout

    @classmethod
    def from_config(cls, config_file, docker, log_func=None):
        """Build from config; None when the fork keys are absent/invalid."""
        label = read_value(config_file, "TUNNEL_LAUNCHD_LABEL", "").strip()
        addr = read_value(config_file, "TUNNEL_PROXY_ADDR", "").strip()
        if not label or not addr:
            return None
        parsed = _parse_hostport(addr)
        if parsed is None:
            if log_func:
                log_func(f"tunnel triage: unparseable TUNNEL_PROXY_ADDR "
                         f"{addr!r} — rung disabled")
            return None
        host, port = parsed
        return cls(
            label=label, proxy_host=host, proxy_port=port,
            bridge_dest=_first_bridge_dest(
                read_value(config_file, "TOR_BRIDGE_LINES", "")),
            docker=docker,
            upstream_proxy=read_value(config_file, "TOR_UPSTREAM_PROXY", ""),
            log_func=log_func,
        )

    # -- probes ------------------------------------------------------------

    def probe_host_leg(self) -> bool:
        """Mac → tunnel proxy: TCP + SOCKS5 greeting, then CONNECT to a
        bridge when one is configured. Requires a full relay round-trip."""
        try:
            sock = socket.create_connection(
                (self.proxy_host, self.proxy_port), timeout=self._timeout)
        except OSError as e:
            self._log(f"tunnel triage: host leg connect failed: {e}")
            return False
        try:
            sock.settimeout(self._timeout)
            sock.sendall(b"\x05\x01\x00")
            if _recv_exact(sock, 2) != b"\x05\x00":
                self._log("tunnel triage: host leg SOCKS greeting failed")
                return False
            if self.bridge_dest:
                ip, port = self.bridge_dest
                sock.sendall(b"\x05\x01\x00\x01" + socket.inet_aton(ip)
                             + port.to_bytes(2, "big"))
                reply = _recv_exact(sock, 4)
                if len(reply) < 2 or reply[1] != 0:
                    self._log("tunnel triage: host leg CONNECT to bridge "
                              f"refused (rep={reply[1:2]!r})")
                    return False
            return True
        except OSError as e:
            self._log(f"tunnel triage: host leg probe error: {e}")
            return False
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def probe_container_leg(self) -> bool:
        """onionpress-tor → the configured TOR_UPSTREAM_PROXY value."""
        parsed = _parse_hostport(self.upstream_proxy)
        if parsed is None:
            self._log("tunnel triage: no parseable TOR_UPSTREAM_PROXY — "
                      "container leg unknown")
            return False
        host, port = parsed
        script = _CONTAINER_PROBE.format(
            host=host, port=port, dest=self.bridge_dest,
            timeout=self._timeout)
        try:
            r = self._docker.exec(
                "onionpress-tor", ["python3", "-c", script],
                timeout=self._timeout + 15, quiet=True)
        except Exception as e:
            self._log(f"tunnel triage: container leg exec failed: {e}")
            return False
        if not r.ok:
            return False
        out = (r.stdout or "").strip()
        if "TUNNEL-OK" in out:
            return True
        if out:
            self._log(f"tunnel triage: container leg dead: {out}")
        return False

    # -- action ------------------------------------------------------------

    def kick(self) -> bool:
        """Relaunch the launchd tunnel service (same as KeepAlive on crash)."""
        target = f"gui/{os.getuid()}/{self.label}"
        try:
            r = self._run(["launchctl", "kickstart", "-k", target],
                          capture_output=True, timeout=30)
        except Exception as e:
            self._log(f"tunnel triage: kickstart failed: {e}")
            return False
        rc = getattr(r, "returncode", 1)
        if rc != 0:
            self._log(f"tunnel triage: kickstart {target} exited {rc}")
        return rc == 0
