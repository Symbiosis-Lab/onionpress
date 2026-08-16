"""Tests for src/onionpress/tunnel_fork.py — FORK-ONLY tunnel triage.

This module (and this test file) is excluded from any upstream PR
(self-healing-design.md §3.4). The rung activates only when the
TUNNEL_LAUNCHD_LABEL / TUNNEL_PROXY_ADDR config keys are present.
"""

import os
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.tunnel_fork import (
    TunnelTriage, _first_bridge_dest, _parse_hostport,
)


def _write_config(**keys):
    f = tempfile.NamedTemporaryFile(
        "w", suffix=".config", delete=False, encoding="utf-8")
    for k, v in keys.items():
        f.write(f"{k}={v}\n")
    f.close()
    return f.name


class FakeSocksProxy(threading.Thread):
    """Minimal SOCKS5 endpoint: scripted greeting + CONNECT replies.

    modes: "ok" (greeting + CONNECT succeed), "greeting-dead" (accepts
    TCP, never answers — the half-alive socat of the incident),
    "connect-refused" (greeting OK, CONNECT rep=5).
    """

    def __init__(self, mode="ok"):
        super().__init__(daemon=True)
        self.mode = mode
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(2)
        self.port = self.sock.getsockname()[1]

    def run(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        try:
            conn.settimeout(5)
            greeting = conn.recv(3)
            if self.mode == "greeting-dead" or not greeting:
                # Half-alive relay: TCP accepted, no bytes ever come back.
                import time
                time.sleep(3)
                return
            conn.sendall(b"\x05\x00")
            req = conn.recv(64)
            if not req:
                return
            rep = b"\x00" if self.mode == "ok" else b"\x05"
            conn.sendall(b"\x05" + rep + b"\x00\x01"
                         + socket.inet_aton("0.0.0.0") + b"\x00\x00")
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self.sock.close()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class TestParsing(unittest.TestCase):
    def test_parse_hostport(self):
        self.assertEqual(_parse_hostport("127.0.0.1:15235"),
                         ("127.0.0.1", 15235))

    def test_parse_hostport_with_scheme(self):
        self.assertEqual(_parse_hostport("socks5://172.19.0.1:15235"),
                         ("172.19.0.1", 15235))

    def test_parse_hostport_invalid(self):
        self.assertIsNone(_parse_hostport("not-an-addr"))
        self.assertIsNone(_parse_hostport(""))

    def test_first_bridge_dest(self):
        lines = ("obfs4 1.2.3.4:443 AAAA cert=zz iat-mode=0;"
                 "obfs4 5.6.7.8:80 BBBB")
        self.assertEqual(_first_bridge_dest(lines), ("1.2.3.4", 443))

    def test_first_bridge_dest_strips_bridge_prefix(self):
        self.assertEqual(_first_bridge_dest("Bridge obfs4 9.9.9.9:9001 CC"),
                         ("9.9.9.9", 9001))

    def test_first_bridge_dest_none_when_absent(self):
        self.assertIsNone(_first_bridge_dest(""))
        self.assertIsNone(_first_bridge_dest("obfs4 hostname-only FING"))


class TestFromConfig(unittest.TestCase):
    def test_absent_keys_deactivate_the_rung(self):
        path = _write_config(TOR_BRIDGE_LINES="obfs4 1.2.3.4:443 AA")
        self.addCleanup(os.unlink, path)
        self.assertIsNone(TunnelTriage.from_config(path, mock.Mock()))

    def test_one_key_alone_is_not_enough(self):
        path = _write_config(TUNNEL_LAUNCHD_LABEL="com.onionpress.veee-tunnel")
        self.addCleanup(os.unlink, path)
        self.assertIsNone(TunnelTriage.from_config(path, mock.Mock()))

    def test_both_keys_activate(self):
        path = _write_config(
            TUNNEL_LAUNCHD_LABEL="com.onionpress.veee-tunnel",
            TUNNEL_PROXY_ADDR="127.0.0.1:15235",
            TOR_BRIDGE_LINES="obfs4 1.2.3.4:443 AA cert=zz",
            TOR_UPSTREAM_PROXY="socks5://172.19.0.1:15235",
        )
        self.addCleanup(os.unlink, path)
        t = TunnelTriage.from_config(path, mock.Mock())
        self.assertIsNotNone(t)
        self.assertEqual(t.label, "com.onionpress.veee-tunnel")
        self.assertEqual(t.proxy_host, "127.0.0.1")
        self.assertEqual(t.proxy_port, 15235)
        self.assertEqual(t.bridge_dest, ("1.2.3.4", 443))

    def test_bad_proxy_addr_deactivates(self):
        path = _write_config(
            TUNNEL_LAUNCHD_LABEL="x", TUNNEL_PROXY_ADDR="garbage")
        self.addCleanup(os.unlink, path)
        self.assertIsNone(TunnelTriage.from_config(path, mock.Mock()))


class TestHostLegProbe(unittest.TestCase):
    def _triage(self, port, bridge=("1.2.3.4", 443)):
        return TunnelTriage(
            label="com.onionpress.veee-tunnel",
            proxy_host="127.0.0.1", proxy_port=port,
            bridge_dest=bridge, docker=mock.Mock(),
            upstream_proxy="socks5://172.19.0.1:15235",
            probe_timeout=2,
        )

    def test_healthy_proxy_passes(self):
        srv = FakeSocksProxy("ok")
        srv.start()
        self.addCleanup(srv.close)
        self.assertTrue(self._triage(srv.port).probe_host_leg())

    def test_connection_refused_fails(self):
        srv = FakeSocksProxy("ok")  # never started; port is bound+closed
        port = srv.port
        srv.close()
        self.assertFalse(self._triage(port).probe_host_leg())

    def test_half_alive_relay_fails(self):
        # The incident signature: socat accepts TCP but relays nothing.
        srv = FakeSocksProxy("greeting-dead")
        srv.start()
        self.addCleanup(srv.close)
        self.assertFalse(self._triage(srv.port).probe_host_leg())

    def test_connect_refusal_fails(self):
        # Greeting OK but the proxy cannot carry traffic to the bridge —
        # Veee (or its path out) is sick; restarts would be blind.
        srv = FakeSocksProxy("connect-refused")
        srv.start()
        self.addCleanup(srv.close)
        self.assertFalse(self._triage(srv.port).probe_host_leg())

    def test_no_bridge_dest_greeting_alone_passes(self):
        srv = FakeSocksProxy("ok")
        srv.start()
        self.addCleanup(srv.close)
        self.assertTrue(self._triage(srv.port, bridge=None).probe_host_leg())


class TestContainerLegProbe(unittest.TestCase):
    def _triage(self, docker):
        return TunnelTriage(
            label="l", proxy_host="127.0.0.1", proxy_port=15235,
            bridge_dest=("1.2.3.4", 443), docker=docker,
            upstream_proxy="socks5://172.19.0.1:15235",
        )

    def test_probe_runs_stdlib_python_inside_tor_container(self):
        docker = mock.Mock()
        docker.exec.return_value = mock.Mock(ok=True, stdout="TUNNEL-OK\n")
        t = self._triage(docker)
        self.assertTrue(t.probe_container_leg())
        container, cmd = docker.exec.call_args.args[:2]
        self.assertEqual(container, "onionpress-tor")
        self.assertEqual(cmd[0], "python3")
        script = cmd[2]
        # The probe must target the CONFIGURED upstream value — 172.19 is
        # not guaranteed, so the address must come from config, verbatim.
        self.assertIn("172.19.0.1", script)
        self.assertIn("15235", script)

    def test_dead_leg_reports_false(self):
        docker = mock.Mock()
        docker.exec.return_value = mock.Mock(ok=True, stdout="TUNNEL-FAIL connect\n")
        self.assertFalse(self._triage(docker).probe_container_leg())

    def test_exec_failure_reports_false(self):
        docker = mock.Mock()
        docker.exec.return_value = mock.Mock(ok=False, stdout="")
        self.assertFalse(self._triage(docker).probe_container_leg())

    def test_no_upstream_proxy_config_reports_false(self):
        docker = mock.Mock()
        t = TunnelTriage(label="l", proxy_host="127.0.0.1", proxy_port=1,
                         bridge_dest=None, docker=docker, upstream_proxy="")
        self.assertFalse(t.probe_container_leg())
        docker.exec.assert_not_called()


class TestKick(unittest.TestCase):
    def test_kickstart_invocation(self):
        calls = []

        def run_func(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0)

        t = TunnelTriage(label="com.onionpress.veee-tunnel",
                         proxy_host="127.0.0.1", proxy_port=15235,
                         bridge_dest=None, docker=mock.Mock(),
                         upstream_proxy="", run_func=run_func)
        self.assertTrue(t.kick())
        self.assertEqual(calls, [[
            "launchctl", "kickstart", "-k",
            f"gui/{os.getuid()}/com.onionpress.veee-tunnel",
        ]])

    def test_kick_failure_returns_false(self):
        t = TunnelTriage(label="x", proxy_host="127.0.0.1", proxy_port=1,
                         bridge_dest=None, docker=mock.Mock(),
                         upstream_proxy="",
                         run_func=mock.Mock(side_effect=OSError("no launchctl")))
        self.assertFalse(t.kick())


if __name__ == "__main__":
    unittest.main()
