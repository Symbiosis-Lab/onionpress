"""Tests for src/onionpress/health.py and the src/onionpress/self_heal.py
host-side supervisor decision table (self-healing-design.md §3.2 Actor 2)."""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.docker import DockerResult
from onionpress.health import (
    HealthChecker, HealthResult, HealthMonitor, HealthState,
    ServiceState, SICK_PATTERNS, HEALTHY_PATTERNS, WedgeSignals,
    YELLOW_TO_STUCK_SECONDS, YELLOW_TO_RESTART_SECONDS,
    RESTART_COOLDOWN_SECONDS, RECLAIM_RETRY_SECONDS,
    POLL_READY_SECONDS, POLL_STARTING_SECONDS, POLL_OFFLINE_SECONDS,
    WEDGE_LOAD_WARN, WEDGE_LOAD_ALARM, WEDGE_FAILING_STREAK_ALARM,
    decode_curl_reason,
)
from onionpress import self_heal
from onionpress.self_heal import (
    SelfHealSupervisor, WatchdogView, parse_watchdog_state,
    AGREE_CYCLES, WATCHDOG_STALE_SECONDS, SETTLE_SECONDS,
    H2_SPACING_SECONDS, H2_JITTER_MAX_SECONDS, H2_BUDGET,
    H2_BUDGET_WINDOW, H1_KICK_SPACING,
)


def _ok(stdout="", stderr=""):
    return DockerResult(returncode=0, stdout=stdout, stderr=stderr)


def _fail(stderr="error", code=1):
    return DockerResult(returncode=code, stdout="", stderr=stderr)


class TestHealthResult(unittest.TestCase):
    def test_ready_when_all_checks_pass(self):
        hr = HealthResult(wp_healthy=True, tor_externally_reachable=True)
        self.assertTrue(hr.ready)

    def test_not_ready_missing_wp(self):
        hr = HealthResult(wp_healthy=False, tor_externally_reachable=True)
        self.assertFalse(hr.ready)

    def test_not_ready_missing_tor(self):
        hr = HealthResult(wp_healthy=True, tor_externally_reachable=False)
        self.assertFalse(hr.ready)

    def test_defaults(self):
        hr = HealthResult()
        self.assertFalse(hr.ready)
        self.assertEqual(hr.errors, [])

    def test_unknown_reachability_defaults_to_none_not_false(self):
        # moss#917: a HealthResult that never ran Check 5 must be
        # distinguishable from one that ran it and got a negative answer —
        # status.json (and moss's onion_reachable) treats None as "unknown",
        # never as "confirmed unreachable".
        hr = HealthResult()
        self.assertIsNone(hr.tor_externally_reachable)
        self.assertIsNone(hr.external_http_code)
        self.assertFalse(hr.ready)  # None must not satisfy `ready`

    def test_not_ready_when_reachability_unknown(self):
        hr = HealthResult(wp_healthy=True, tor_externally_reachable=None)
        self.assertFalse(hr.ready)


class TestCheckWordpressLocal(unittest.TestCase):
    def test_healthy(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("<html>WordPress</html>")
        hc = HealthChecker(docker)
        self.assertTrue(hc.check_wordpress_local())

    def test_database_error(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("Error establishing a database connection")
        hc = HealthChecker(docker)
        self.assertFalse(hc.check_wordpress_local())

    def test_unreachable(self):
        docker = mock.Mock()
        docker.exec.return_value = _fail()
        hc = HealthChecker(docker)
        self.assertFalse(hc.check_wordpress_local())


class TestCheckWordpressExternal(unittest.TestCase):
    """check_wordpress_external shells out to host curl — patch subprocess.run."""

    def _result(self, returncode=0, stdout=""):
        r = mock.Mock()
        r.returncode = returncode
        r.stdout = stdout
        return r

    def test_healthy(self):
        hc = HealthChecker(mock.Mock())
        with mock.patch("onionpress.health.subprocess.run",
                        return_value=self._result(0, "<html>WordPress</html>")):
            self.assertTrue(hc.check_wordpress_external(8080, log=False))

    def test_database_error_in_body(self):
        """A 200 OK with a DB-error body is NOT healthy — this is the onionheaven bug fix."""
        hc = HealthChecker(mock.Mock())
        with mock.patch("onionpress.health.subprocess.run",
                        return_value=self._result(0, "Error establishing a database connection")):
            self.assertFalse(hc.check_wordpress_external(8080, log=False))

    def test_alternate_database_error_in_body(self):
        hc = HealthChecker(mock.Mock())
        with mock.patch("onionpress.health.subprocess.run",
                        return_value=self._result(0, "Database connection error")):
            self.assertFalse(hc.check_wordpress_external(8080, log=False))

    def test_curl_exit_code_nonzero(self):
        hc = HealthChecker(mock.Mock())
        with mock.patch("onionpress.health.subprocess.run",
                        return_value=self._result(7, "")):
            self.assertFalse(hc.check_wordpress_external(8080, log=False))

    def test_subprocess_exception(self):
        hc = HealthChecker(mock.Mock())
        with mock.patch("onionpress.health.subprocess.run",
                        side_effect=OSError("boom")):
            self.assertFalse(hc.check_wordpress_external(8080, log=False))

    def test_logs_on_success(self):
        logs = []
        hc = HealthChecker(mock.Mock(), log_func=logs.append)
        with mock.patch("onionpress.health.subprocess.run",
                        return_value=self._result(0, "<html>")):
            hc.check_wordpress_external(8080, log=True)
        self.assertTrue(any("Checking local access" in l for l in logs))
        self.assertTrue(any("WordPress responding" in l for l in logs))

    def test_silent_when_log_false(self):
        logs = []
        hc = HealthChecker(mock.Mock(), log_func=logs.append)
        with mock.patch("onionpress.health.subprocess.run",
                        return_value=self._result(0, "<html>")):
            hc.check_wordpress_external(8080, log=False)
        self.assertEqual(logs, [])


class TestCheckTorBootstrap(unittest.TestCase):
    # check_tor_bootstrap first tries the control-port via docker.exec; if
    # that doesn't return a parseable PROGRESS line it falls back to
    # docker.run logs. These tests exercise the log fallback, so exec is
    # stubbed to fail.

    def test_100_percent(self):
        docker = mock.Mock()
        docker.exec.return_value = _fail()
        docker.run.return_value = _ok("Bootstrapped 100% (done): Done")
        hc = HealthChecker(docker)
        bootstrapped, pct = hc.check_tor_bootstrap()
        self.assertTrue(bootstrapped)
        self.assertEqual(pct, 100)

    def test_partial(self):
        docker = mock.Mock()
        docker.exec.return_value = _fail()
        docker.run.return_value = _ok("PROGRESS=50 TAG=loading")
        hc = HealthChecker(docker)
        bootstrapped, pct = hc.check_tor_bootstrap()
        self.assertFalse(bootstrapped)
        self.assertEqual(pct, 50)

    def test_arti_sufficiently_bootstrapped(self):
        docker = mock.Mock()
        docker.exec.return_value = _fail()
        docker.run.return_value = _ok("Sufficiently bootstrapped to build circuits")
        hc = HealthChecker(docker)
        bootstrapped, pct = hc.check_tor_bootstrap()
        self.assertTrue(bootstrapped)

    def test_no_logs(self):
        docker = mock.Mock()
        docker.exec.return_value = _fail()
        docker.run.return_value = _fail()
        hc = HealthChecker(docker)
        bootstrapped, pct = hc.check_tor_bootstrap()
        self.assertFalse(bootstrapped)
        self.assertEqual(pct, 0)

    def test_control_port_primary(self):
        """Control-port probe short-circuits the log-based fallback."""
        docker = mock.Mock()
        docker.exec.return_value = _ok("250-status/bootstrap-phase=NOTICE BOOTSTRAP PROGRESS=100 TAG=done")
        docker.run.side_effect = AssertionError("should not fall through to logs")
        hc = HealthChecker(docker)
        bootstrapped, pct = hc.check_tor_bootstrap()
        self.assertTrue(bootstrapped)
        self.assertEqual(pct, 100)


class TestCheckTorHostname(unittest.TestCase):
    def test_returns_address(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("op2abc.onion\n")
        hc = HealthChecker(docker)
        self.assertEqual(hc.check_tor_hostname(), "op2abc.onion")

    def test_mismatch_logs_warning(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("different.onion\n")
        logs = []
        hc = HealthChecker(docker, log_func=logs.append)
        addr = hc.check_tor_hostname(expected_address="expected.onion")
        self.assertEqual(addr, "different.onion")
        self.assertTrue(any("mismatch" in l for l in logs))


class TestCheckInternalConnectivity(unittest.TestCase):
    def test_reachable(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok()
        hc = HealthChecker(docker)
        self.assertTrue(hc.check_internal_connectivity())

    def test_unreachable(self):
        docker = mock.Mock()
        docker.exec.return_value = _fail()
        hc = HealthChecker(docker)
        self.assertFalse(hc.check_internal_connectivity())


class TestCheckInternetConnectivity(unittest.TestCase):
    """Interface-scan based check — no TCC-gated API calls."""

    def _run(self, stdout, returncode=0):
        return mock.Mock(returncode=returncode, stdout=stdout)

    def test_real_en0_has_internet(self):
        ifconfig = (
            "lo0: flags=8049<UP,LOOPBACK> mtu 16384\n"
            "\tinet 127.0.0.1 netmask 0xff000000\n"
            "en0: flags=8863<UP,BROADCAST> mtu 1500\n"
            "\tinet 192.168.1.42 netmask 0xffffff00\n"
        )
        with mock.patch("onionpress.health.subprocess.run",
                        return_value=self._run(ifconfig)):
            self.assertTrue(HealthChecker.check_internet_connectivity())

    def test_only_loopback_means_offline(self):
        ifconfig = (
            "lo0: flags=8049<UP,LOOPBACK> mtu 16384\n"
            "\tinet 127.0.0.1 netmask 0xff000000\n"
            "en0: flags=8863<UP,BROADCAST> mtu 1500\n"
            "\tether aa:bb:cc:dd:ee:ff\n"
        )
        with mock.patch("onionpress.health.subprocess.run",
                        return_value=self._run(ifconfig)):
            self.assertFalse(HealthChecker.check_internet_connectivity())

    def test_ifconfig_failure_assumes_connected(self):
        """Don't block the app if ifconfig can't run for some reason."""
        with mock.patch("onionpress.health.subprocess.run",
                        return_value=self._run("", returncode=1)):
            self.assertTrue(HealthChecker.check_internet_connectivity())

    def test_subprocess_exception_assumes_connected(self):
        with mock.patch("onionpress.health.subprocess.run",
                        side_effect=OSError("boom")):
            self.assertTrue(HealthChecker.check_internet_connectivity())


class TestCheckExternalReachability(unittest.TestCase):
    def test_reachable_external_succeeds_fast_path(self):
        # Hot path: onionheaven returns 200, we short-circuit without probing self.
        docker = mock.Mock()
        docker.exec.return_value = _ok("200")
        hc = HealthChecker(docker)
        reachable, code = hc.check_external_reachability("op2abc.onion")
        self.assertTrue(reachable)
        self.assertEqual(code, "200")
        # Exactly one docker.exec — we didn't waste probes.
        self.assertEqual(docker.exec.call_count, 1)

    def test_unreachable_both_fail(self):
        # External fails, self fails → genuinely unreachable.
        docker = mock.Mock()
        docker.exec.side_effect = [
            _fail(),  # external probe via onionheaven
            _fail(),  # self probe via onionpress-tor
        ]
        hc = HealthChecker(docker)
        reachable, _code = hc.check_external_reachability("op2abc.onion")
        self.assertFalse(reachable)

    def test_reachable_self_ok_onionheaven_sick(self):
        # Today's yellow-state bug: onionheaven's tor stuck, our tor fine.
        # External probe fails, self probe succeeds, onionheaven can't
        # reach the hub either → trust self, report reachable.
        docker = mock.Mock()
        docker.exec.side_effect = [
            _ok("000"),   # external probe: curl "ok" but HTTP 000 (timeout)
            _ok("301"),   # self probe: success
            _fail(),      # onionheaven hub probe: fails (probe is sick)
        ]
        hc = HealthChecker(docker)
        reachable, code = hc.check_external_reachability("op2abc.onion")
        self.assertTrue(reachable)
        self.assertTrue(code.startswith("degraded:"))

    def test_unreachable_self_ok_onionheaven_healthy(self):
        # Descriptor-publish failure case: our tor reaches its own HS,
        # onionheaven's tor is working fine (can reach the hub), yet
        # onionheaven can't reach us → we're dark to outside visitors.
        docker = mock.Mock()
        docker.exec.side_effect = [
            _ok("000"),    # external probe to us: fails
            _ok("200"),    # self probe: success
            _ok("200"),    # onionheaven hub probe: healthy
        ]
        hc = HealthChecker(docker)
        reachable, _code = hc.check_external_reachability("op2abc.onion")
        self.assertFalse(reachable)

    def test_empty_address(self):
        docker = mock.Mock()
        hc = HealthChecker(docker)
        reachable, code = hc.check_external_reachability("")
        self.assertFalse(reachable)
        docker.exec.assert_not_called()

    def test_onionheaven_health_is_cached(self):
        # Caching: two disagreement cycles within 60s should hit the
        # onionheaven-hub probe exactly once.
        docker = mock.Mock()
        docker.exec.side_effect = [
            _ok("000"), _ok("301"), _fail(),  # cycle 1: ext, self, hub (fails → sick)
            _ok("000"), _ok("301"),           # cycle 2: ext, self — hub cached
        ]
        hc = HealthChecker(docker)
        r1, _ = hc.check_external_reachability("op2abc.onion")
        r2, _ = hc.check_external_reachability("op2abc.onion")
        self.assertTrue(r1)
        self.assertTrue(r2)
        self.assertEqual(docker.exec.call_count, 5)


class TestTorContainerUnhealthy(unittest.TestCase):
    def test_sick_patterns(self):
        for pattern in SICK_PATTERNS:
            docker = mock.Mock()
            docker.run.return_value = _ok(f"some log\n{pattern}\nmore log")
            hc = HealthChecker(docker)
            self.assertTrue(hc.tor_container_unhealthy(), f"Should be unhealthy for: {pattern}")

    def test_healthy_pattern(self):
        docker = mock.Mock()
        docker.run.return_value = _ok("Sufficiently bootstrapped to build circuits")
        hc = HealthChecker(docker)
        self.assertFalse(hc.tor_container_unhealthy())

    def test_no_patterns_returns_healthy(self):
        docker = mock.Mock()
        docker.run.return_value = _ok("some random log output")
        hc = HealthChecker(docker)
        # No clear signals → don't restart (conservative approach)
        self.assertFalse(hc.tor_container_unhealthy())

    def test_log_failure_returns_unhealthy(self):
        docker = mock.Mock()
        docker.run.return_value = _fail()
        hc = HealthChecker(docker)
        self.assertTrue(hc.tor_container_unhealthy())


class TestFullCheck(unittest.TestCase):
    def test_all_healthy(self):
        docker = mock.Mock()
        docker.exec.side_effect = [
            _ok("<html>"),           # check_wordpress_local
            _fail(),                 # check_tor_bootstrap control-port probe → falls back to docker.run
            _ok("op2abc.onion\n"),   # check_tor_hostname
            _ok(),                   # check_internal_connectivity
            _ok("200"),              # check_external_reachability
        ]
        docker.run.return_value = _ok("Bootstrapped 100% (done)")
        hc = HealthChecker(docker)
        hr = hc.full_check()
        self.assertTrue(hr.ready)
        self.assertTrue(hr.wp_healthy)
        self.assertTrue(hr.tor_bootstrapped)
        self.assertTrue(hr.tor_internally_ready)
        self.assertTrue(hr.tor_externally_reachable)
        self.assertEqual(hr.onion_address, "op2abc.onion")

    def test_wp_down_skips_later_checks(self):
        docker = mock.Mock()
        docker.exec.side_effect = [
            _fail(),               # check_wordpress_local
            _fail(),               # check_tor_bootstrap control-port probe
            _ok("op2abc.onion\n"), # check_tor_hostname
        ]
        docker.run.return_value = _ok("Bootstrapped 100%")
        hc = HealthChecker(docker)
        hr = hc.full_check()
        self.assertFalse(hr.ready)
        self.assertFalse(hr.tor_internally_ready)
        # moss#917: Check 5 never ran (gated on tor_internally_ready) — the
        # regression this guards is write_status() reading this as a
        # confirmed-unreachable `false` instead of "never asked".
        self.assertIsNone(hr.tor_externally_reachable)
        self.assertIsNone(hr.external_http_code)

    def test_missing_onion_address_skips_check_5_leaves_reachability_unknown(self):
        # Checks 1-4 can all pass with no onion_address yet (e.g. hostname
        # file present but check_tor_hostname returned "" for some other
        # reason) — full_check's Check 5 gate requires BOTH
        # tor_internally_ready and onion_address, so this exercises the
        # other half of that gate.
        docker = mock.Mock()
        docker.exec.side_effect = [
            _ok("<html>"),   # check_wordpress_local
            _fail(),         # check_tor_bootstrap control-port probe
            _ok(""),         # check_tor_hostname → empty address
            _ok(),           # check_internal_connectivity
        ]
        docker.run.return_value = _ok("Bootstrapped 100% (done)")
        hc = HealthChecker(docker)
        hr = hc.full_check()
        self.assertTrue(hr.tor_internally_ready)
        self.assertEqual(hr.onion_address, "")
        self.assertIsNone(hr.tor_externally_reachable)
        self.assertIsNone(hr.external_http_code)
        self.assertFalse(hr.ready)


class TestHealthMonitorEvaluate(unittest.TestCase):
    def test_stopped(self):
        hm = HealthMonitor()
        state = hm.evaluate(HealthResult(), is_running=False)
        self.assertEqual(state, ServiceState.STOPPED)

    def test_available(self):
        hm = HealthMonitor()
        hr = HealthResult(wp_healthy=True, tor_externally_reachable=True)
        state = hm.evaluate(hr)
        self.assertEqual(state, ServiceState.AVAILABLE)
        self.assertTrue(hm.state.was_ready)
        self.assertIsNone(hm.state.yellow_since)

    def test_starting(self):
        hm = HealthMonitor()
        hr = HealthResult(wp_healthy=True, tor_externally_reachable=False, bootstrap_pct=50)
        state = hm.evaluate(hr)
        self.assertEqual(state, ServiceState.STARTING)
        self.assertIsNotNone(hm.state.yellow_since)

    def test_degraded_from_ready(self):
        hm = HealthMonitor()
        # First: available
        hm.evaluate(HealthResult(wp_healthy=True, tor_externally_reachable=True))
        # Then: degraded
        state = hm.evaluate(HealthResult(wp_healthy=True, tor_externally_reachable=False))
        self.assertEqual(state, ServiceState.STARTING)
        self.assertIsNotNone(hm.state.yellow_since)

    def test_stuck_after_timeout(self):
        hm = HealthMonitor()
        hm.state.yellow_since = time.time() - YELLOW_TO_STUCK_SECONDS - 1
        hr = HealthResult(wp_healthy=True, tor_externally_reachable=False, bootstrap_pct=50)
        state = hm.evaluate(hr)
        self.assertEqual(state, ServiceState.STUCK)

    def test_offline(self):
        hm = HealthMonitor()
        hm.state.has_internet = False
        hm.state.yellow_since = time.time()
        hr = HealthResult()
        state = hm.evaluate(hr)
        self.assertEqual(state, ServiceState.OFFLINE)

    def test_wordpress_confirmed_persists(self):
        hm = HealthMonitor()
        hm.evaluate(HealthResult(wp_healthy=True))
        self.assertTrue(hm.state.wordpress_confirmed)
        hm.evaluate(HealthResult(wp_healthy=False))
        self.assertTrue(hm.state.wordpress_confirmed)  # still True

    def test_bootstrap_stall_tracking(self):
        hm = HealthMonitor()
        hm.evaluate(HealthResult(bootstrap_pct=50))
        self.assertEqual(hm.state.last_bootstrap_pct, 50)
        self.assertEqual(hm.state.bootstrap_stall_count, 0)
        # Same percentage → stall
        hm.evaluate(HealthResult(bootstrap_pct=50))
        self.assertEqual(hm.state.bootstrap_stall_count, 1)
        # Progress → reset
        hm.evaluate(HealthResult(bootstrap_pct=75))
        self.assertEqual(hm.state.bootstrap_stall_count, 0)


class TestShouldRestartTor(unittest.TestCase):
    def test_no_restart_when_not_yellow(self):
        hm = HealthMonitor()
        self.assertFalse(hm.should_restart_tor(tor_unhealthy=True))

    def test_restart_after_thresholds(self):
        hm = HealthMonitor()
        hm.state.yellow_since = time.time() - YELLOW_TO_RESTART_SECONDS - 1
        hm.state.last_auto_restart = time.time() - RESTART_COOLDOWN_SECONDS - 1
        self.assertTrue(hm.should_restart_tor(tor_unhealthy=True))
        # Should update last_auto_restart
        self.assertGreater(hm.state.last_auto_restart, 0)

    def test_no_restart_during_cooldown(self):
        hm = HealthMonitor()
        hm.state.yellow_since = time.time() - YELLOW_TO_RESTART_SECONDS - 1
        hm.state.last_auto_restart = time.time()  # Just restarted
        self.assertFalse(hm.should_restart_tor(tor_unhealthy=True))

    def test_no_restart_if_healthy(self):
        hm = HealthMonitor()
        hm.state.yellow_since = time.time() - YELLOW_TO_RESTART_SECONDS - 1
        hm.state.last_auto_restart = time.time() - RESTART_COOLDOWN_SECONDS - 1
        self.assertFalse(hm.should_restart_tor(tor_unhealthy=False))


class TestShouldReclaim(unittest.TestCase):
    def test_reclaim_when_internally_ready(self):
        hm = HealthMonitor()
        hm.state.tor_internally_ready = True
        hm.state.reclaim_last_attempt = 0
        self.assertTrue(hm.should_reclaim())
        self.assertTrue(hm.state.reclaim_in_flight)

    def test_no_reclaim_not_internally_ready(self):
        hm = HealthMonitor()
        hm.state.tor_internally_ready = False
        self.assertFalse(hm.should_reclaim())

    def test_no_reclaim_already_succeeded(self):
        hm = HealthMonitor()
        hm.state.tor_internally_ready = True
        hm.state.reclaim_succeeded = True
        self.assertFalse(hm.should_reclaim())

    def test_no_reclaim_in_flight(self):
        hm = HealthMonitor()
        hm.state.tor_internally_ready = True
        hm.state.reclaim_in_flight = True
        self.assertFalse(hm.should_reclaim())

    def test_no_reclaim_too_soon(self):
        hm = HealthMonitor()
        hm.state.tor_internally_ready = True
        hm.state.reclaim_last_attempt = time.time()
        self.assertFalse(hm.should_reclaim())


class TestPollInterval(unittest.TestCase):
    def test_ready(self):
        hm = HealthMonitor()
        self.assertEqual(hm.poll_interval(ServiceState.AVAILABLE), POLL_READY_SECONDS)

    def test_offline(self):
        hm = HealthMonitor()
        self.assertEqual(hm.poll_interval(ServiceState.OFFLINE), POLL_OFFLINE_SECONDS)

    def test_starting(self):
        hm = HealthMonitor()
        self.assertEqual(hm.poll_interval(ServiceState.STARTING), POLL_STARTING_SECONDS)

    def test_stuck(self):
        hm = HealthMonitor()
        self.assertEqual(hm.poll_interval(ServiceState.STUCK), POLL_STARTING_SECONDS)


class TestCheckVMWedge(unittest.TestCase):
    """Validates the wedge probe against the Apr 2026 onionpress.org snapshot.

    Ground truth (captured live from the hung VM):
      - /proc/loadavg on tor container: "150.14 150.07 149.73 1/455 1832876"
      - docker inspect wordpress State.Health: Status="unhealthy", FailingStreak=32

    These cases ensure the probe would have detected that incident and
    that a healthy machine stays silent.
    """

    def _make_checker(self, loadavg_output, inspect_output):
        docker = mock.MagicMock()
        # `exec("onionpress-tor", ["cat", "/proc/loadavg"], ...)`
        docker.exec.return_value = _ok(stdout=loadavg_output) if loadavg_output else _fail()
        # `run(["inspect", ...])`
        docker.run.return_value = _ok(stdout=inspect_output) if inspect_output else _fail()
        return HealthChecker(docker)

    def test_wedged_machine_returns_alarm_signals(self):
        # Snapshot: load=150.14, wp unhealthy, streak=32
        checker = self._make_checker(
            "150.14 150.07 149.73 1/455 1832876\n",
            '{"Status":"unhealthy","FailingStreak":32,"Log":[]}',
        )
        s = checker.check_vm_wedge()
        self.assertAlmostEqual(s.loadavg_1min, 150.14, places=2)
        self.assertEqual(s.wp_health_status, "unhealthy")
        self.assertEqual(s.wp_failing_streak, 32)
        # Both thresholds should classify this as "alarm"
        self.assertGreaterEqual(s.loadavg_1min, WEDGE_LOAD_ALARM)
        self.assertGreaterEqual(s.wp_failing_streak, WEDGE_FAILING_STREAK_ALARM)

    def test_healthy_machine_is_below_thresholds(self):
        checker = self._make_checker(
            "0.10 0.05 0.01 1/200 12345\n",
            '{"Status":"healthy","FailingStreak":0,"Log":[]}',
        )
        s = checker.check_vm_wedge()
        self.assertLess(s.loadavg_1min, WEDGE_LOAD_WARN)
        self.assertEqual(s.wp_health_status, "healthy")
        self.assertEqual(s.wp_failing_streak, 0)

    def test_no_health_block_handled(self):
        # A container without a HEALTHCHECK directive returns "null"
        checker = self._make_checker("0.50 0.40 0.30 1/200 12345\n", "null")
        s = checker.check_vm_wedge()
        self.assertAlmostEqual(s.loadavg_1min, 0.50, places=2)
        self.assertIsNone(s.wp_health_status)
        self.assertIsNone(s.wp_failing_streak)

    def test_both_probes_fail_returns_none(self):
        docker = mock.MagicMock()
        docker.exec.return_value = _fail()
        docker.run.return_value = _fail()
        checker = HealthChecker(docker)
        self.assertIsNone(checker.check_vm_wedge())

    def test_malformed_loadavg_doesnt_crash(self):
        checker = self._make_checker(
            "garbage\n",
            '{"Status":"healthy","FailingStreak":0,"Log":[]}',
        )
        s = checker.check_vm_wedge()
        self.assertIsNone(s.loadavg_1min)
        self.assertEqual(s.wp_health_status, "healthy")

    def test_malformed_json_doesnt_crash(self):
        checker = self._make_checker("0.20 0.15 0.10 1/200 12345\n", "not json")
        s = checker.check_vm_wedge()
        self.assertAlmostEqual(s.loadavg_1min, 0.20, places=2)
        self.assertIsNone(s.wp_health_status)

    def test_probe_never_execs_into_wordpress(self):
        # Critical invariant: docker exec into a wedged WP container would
        # itself hang on fuse_lock_inode. The probe must only use
        # exec(onionpress-tor) and run(inspect).
        docker = mock.MagicMock()
        docker.exec.return_value = _ok(stdout="1.0 1.0 1.0 1/200 12345\n")
        docker.run.return_value = _ok(stdout='{"Status":"healthy","FailingStreak":0}')
        checker = HealthChecker(docker)
        checker.check_vm_wedge()
        for call in docker.exec.call_args_list:
            self.assertNotEqual(
                call.args[0], "onionpress-wordpress",
                "wedge probe must never exec into wordpress (would hang)",
            )


class TestDecodeCurlReason(unittest.TestCase):
    """decode_curl_reason maps '000rc=N' http_codes to human strings."""

    def test_known_codes(self):
        self.assertEqual(decode_curl_reason("000rc=6"),
                         "DNS resolution failed")
        self.assertEqual(decode_curl_reason("000rc=28"), "timeout (30s)")
        self.assertEqual(decode_curl_reason("000rc=97"),
                         "SOCKS handshake failed (descriptor not yet available)")

    def test_unknown_rc_falls_back_to_raw(self):
        self.assertEqual(decode_curl_reason("000rc=999"), "curl rc=999")

    def test_no_rc_suffix(self):
        # "000" with no rc= means curl gave us nothing to decode.
        self.assertEqual(decode_curl_reason("000"), "unknown")

    def test_empty_rc(self):
        # Pathological "rc=" with no number — treat as unknown rather
        # than crashing or returning a confusing "curl rc=".
        self.assertEqual(decode_curl_reason("000rc="), "unknown")


# ---------------------------------------------------------------------------
# Host-side self-healing supervisor (self-healing-design.md §3.2 Actor 2)
# ---------------------------------------------------------------------------

T0 = 1_700_000_000.0


def _fresh_view(now=T0, **overrides):
    """A readable, fresh watchdog-state view; overrides applied on top."""
    fields = dict(
        readable=True, updated_at=now - 5, stale=False, serving=False,
        e2e_ok=False, e2e_verdict="network", escalate_to_host=False,
        handoff_reason="", degraded=False, restarts_this_outage=0,
        tor_restart_stamps=[],
    )
    fields.update(overrides)
    return WatchdogView(**fields)


def _yielded_view(now=T0, **overrides):
    base = dict(
        escalate_to_host=True, degraded=True,
        handoff_reason="3 restarts this outage changed nothing",
        restarts_this_outage=3,
    )
    base.update(overrides)
    return _fresh_view(now, **base)


class _FakeTunnel:
    """Tunnel triage stub — no subprocess anywhere near the decision table."""

    def __init__(self, host_ok=True, container_ok=True):
        self.host_ok = host_ok
        self.container_ok = container_ok
        self.host_probes = 0
        self.container_probes = 0

    def probe_host_leg(self):
        self.host_probes += 1
        return self.host_ok

    def probe_container_leg(self):
        self.container_probes += 1
        return self.container_ok


class SelfHealBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="selfheal-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.now = T0
        self.jitter = 0.0
        self.logs = []

    def make(self):
        return SelfHealSupervisor(
            self.dir, log_func=self.logs.append,
            now_func=lambda: self.now, jitter_func=lambda: self.jitter,
        )

    def down(self, sup, *, streak=AGREE_CYCLES, code="000rc=28",
             watchdog="yielded", **kw):
        """Evaluate a confirmed-down cycle with sensible defaults."""
        if watchdog == "yielded":
            watchdog = _yielded_view(self.now)
        elif watchdog == "fresh":
            watchdog = _fresh_view(self.now)
        return sup.evaluate(
            reachable=False, unreachable_streak=streak, http_code=code,
            watchdog=watchdog, **kw)


class TestParseWatchdogState(unittest.TestCase):
    def test_missing_text_is_unreadable(self):
        v = parse_watchdog_state(None, T0)
        self.assertFalse(v.readable)
        self.assertTrue(v.stale)

    def test_garbage_is_unreadable(self):
        v = parse_watchdog_state("not json{", T0)
        self.assertFalse(v.readable)

    def test_fresh_state_parses(self):
        payload = {
            "updated_at": int(T0) - 10, "serving": False, "degraded": True,
            "e2e_ok": False, "e2e_verdict": "network",
            "escalate_to_host": True, "handoff_reason": "3 restarts",
            "restarts_this_outage": 3,
            "tor_restart_stamps": [int(T0) - 3000, int(T0) - 1200],
        }
        v = parse_watchdog_state(json.dumps(payload), T0)
        self.assertTrue(v.readable)
        self.assertFalse(v.stale)
        self.assertTrue(v.escalate_to_host)
        self.assertEqual(v.e2e_verdict, "network")
        self.assertEqual(v.restarts_this_outage, 3)
        self.assertEqual(v.tor_restart_stamps, [int(T0) - 3000, int(T0) - 1200])

    def test_stale_state_flagged(self):
        payload = {"updated_at": int(T0) - WATCHDOG_STALE_SECONDS - 1}
        v = parse_watchdog_state(json.dumps(payload), T0)
        self.assertTrue(v.readable)
        self.assertTrue(v.stale)


class TestAgreementGate(SelfHealBase):
    def test_single_fail_cycle_never_acts(self):
        sup = self.make()
        d = self.down(sup, streak=1)
        self.assertIsNone(d.action)

    def test_two_fail_cycles_with_yielded_watchdog_acts(self):
        sup = self.make()
        d = self.down(sup, streak=2)
        self.assertEqual(d.action, "restart_stack")
        self.assertEqual(d.state, "host_restarting")

    def test_reachable_none_is_not_agreement(self):
        # Unknown is not evidence (moss#917 tri-state convention).
        sup = self.make()
        d = sup.evaluate(reachable=None, unreachable_streak=0,
                         http_code=None, watchdog=_yielded_view(self.now))
        self.assertIsNone(d.action)
        self.assertEqual(d.state, "ok")


class TestWatchdogYieldGate(SelfHealBase):
    def test_fresh_unyielded_watchdog_blocks_host(self):
        sup = self.make()
        d = self.down(sup, watchdog="fresh")
        self.assertIsNone(d.action)
        self.assertEqual(d.state, "watchdog_recovering")
        self.assertEqual(d.verdict, "network")

    def test_escalate_flag_yields(self):
        sup = self.make()
        d = self.down(sup, watchdog=_yielded_view(self.now))
        self.assertEqual(d.action, "restart_stack")

    def test_stale_state_yields(self):
        sup = self.make()
        stale = _fresh_view(self.now, updated_at=self.now - 300, stale=True)
        d = self.down(sup, watchdog=stale)
        self.assertEqual(d.action, "restart_stack")

    def test_unreadable_state_yields(self):
        # Container gone / exec failed — the host is the only actor left.
        sup = self.make()
        d = self.down(sup, watchdog=None)
        self.assertEqual(d.action, "restart_stack")


class TestVetoes(SelfHealBase):
    def test_takeover_never_restarts(self):
        # Visitors are being served by the hub — healing is reclaim.
        sup = self.make()
        d = self.down(sup, code="takeover")
        self.assertIsNone(d.action)
        self.assertEqual(d.state, "reclaiming")
        self.assertEqual(d.verdict, "takeover")

    def test_publish_in_flight_vetoes(self):
        sup = self.make()
        d = self.down(sup, publish_in_flight=True)
        self.assertIsNone(d.action)
        self.assertEqual(d.state, "awaiting_host")

    def test_stopping_vetoes(self):
        sup = self.make()
        d = self.down(sup, stopping=True)
        self.assertIsNone(d.action)

    def test_stack_not_running_vetoes(self):
        sup = self.make()
        d = self.down(sup, stack_running=False)
        self.assertIsNone(d.action)


class TestSettleWindow(SelfHealBase):
    def test_watchdog_restart_opens_settle_window(self):
        sup = self.make()
        view = _yielded_view(self.now,
                             tor_restart_stamps=[self.now - 100])
        d = self.down(sup, watchdog=view)
        self.assertIsNone(d.action)
        self.assertEqual(d.next_eligible_at, self.now - 100 + SETTLE_SECONDS)

    def test_own_restart_opens_settle_window(self):
        sup = self.make()
        sup.record_restart(verdict="network")
        self.now += 60
        d = self.down(sup)
        self.assertIsNone(d.action)
        self.assertEqual(d.state, "host_restarting")

    def test_settle_window_expiry_allows_action(self):
        sup = self.make()
        view = _yielded_view(self.now,
                             tor_restart_stamps=[self.now - SETTLE_SECONDS - 1])
        d = self.down(sup, watchdog=view)
        self.assertEqual(d.action, "restart_stack")


class TestBudgetSpacingJitter(SelfHealBase):
    def test_spacing_plus_jitter_blocks_second_restart(self):
        self.jitter = 300.0
        sup = self.make()
        sup.record_restart(verdict="network")
        expected = self.now + H2_SPACING_SECONDS + 300.0
        self.now += H2_SPACING_SECONDS + 200  # past spacing, inside jitter
        d = self.down(sup)
        self.assertIsNone(d.action)
        self.assertEqual(d.next_eligible_at, expected)

    def test_restart_allowed_after_spacing_and_jitter(self):
        self.jitter = 300.0
        sup = self.make()
        sup.record_restart(verdict="network")
        self.now += H2_SPACING_SECONDS + 301
        d = self.down(sup)
        self.assertEqual(d.action, "restart_stack")

    def test_budget_two_per_six_hours_then_give_up(self):
        sup = self.make()
        sup.record_restart()
        self.now += H2_SPACING_SECONDS + 1
        sup.record_restart()
        self.now += H2_SPACING_SECONDS + 1
        d = self.down(sup)
        self.assertIsNone(d.action)
        self.assertEqual(d.state, "given_up")
        self.assertIsNotNone(d.notify)

    def test_give_up_notification_is_one_shot(self):
        sup = self.make()
        sup.record_restart()
        sup.record_restart()
        self.now += SETTLE_SECONDS + 1
        d1 = self.down(sup)
        d2 = self.down(sup)
        self.assertIsNotNone(d1.notify)
        self.assertIsNone(d2.notify)
        self.assertEqual(d2.state, "given_up")

    def test_budget_survives_supervisor_reload(self):
        # The watchdog's own persistence lesson: an app relaunch must not
        # hand out a fresh budget mid-outage.
        sup = self.make()
        sup.record_restart()
        sup.record_restart()
        self.now += SETTLE_SECONDS + 1
        self.down(sup)  # latches given_up
        sup2 = self.make()
        d = self.down(sup2)
        self.assertIsNone(d.action)
        self.assertEqual(d.state, "given_up")

    def test_stamps_outside_window_do_not_count(self):
        sup = self.make()
        sup.record_restart()
        self.now += H2_BUDGET_WINDOW + 1
        d = self.down(sup)
        self.assertEqual(d.action, "restart_stack")

    def test_default_jitter_within_bounds(self):
        for _ in range(200):
            j = self_heal._default_jitter()
            self.assertGreaterEqual(j, 0)
            self.assertLessEqual(j, H2_JITTER_MAX_SECONDS)


class TestGiveUpAndRecovery(SelfHealBase):
    def _exhaust(self, sup):
        sup.record_restart()
        sup.record_restart()
        self.now += SETTLE_SECONDS + 1
        return self.down(sup)

    def test_given_up_is_quiescent(self):
        sup = self.make()
        self._exhaust(sup)
        tunnel = _FakeTunnel(container_ok=False)
        d = self.down(sup, tunnel=tunnel)
        self.assertIsNone(d.action)
        self.assertEqual(tunnel.host_probes, 0)  # no triage while given up

    def test_confirmed_green_clears_given_up(self):
        sup = self.make()
        self._exhaust(sup)
        d = sup.evaluate(reachable=True, unreachable_streak=0, http_code="200",
                         watchdog=_fresh_view(self.now, e2e_ok=True,
                                              serving=True, e2e_verdict=None))
        self.assertEqual(d.state, "ok")
        # A later outage may act again only within the still-rolling budget.
        d = self.down(sup)
        self.assertEqual(d.state, "given_up")  # stamps survived the green

    def test_green_after_budget_window_allows_new_outage_healing(self):
        sup = self.make()
        self._exhaust(sup)
        self.now += H2_BUDGET_WINDOW + 1
        sup.evaluate(reachable=True, unreachable_streak=0, http_code="200",
                     watchdog=None)
        d = self.down(sup)
        self.assertEqual(d.action, "restart_stack")


class TestTunnelTriage(SelfHealBase):
    def test_host_leg_dead_skips_all_restarts_and_notifies_once(self):
        # Veee itself is down — nothing automated can fix a GUI VPN.
        sup = self.make()
        tunnel = _FakeTunnel(host_ok=False)
        d1 = self.down(sup, tunnel=tunnel)
        self.assertIsNone(d1.action)
        self.assertEqual(d1.verdict, "tunnel-upstream-down")
        self.assertIsNotNone(d1.notify)
        d2 = self.down(sup, tunnel=tunnel)
        self.assertIsNone(d2.notify)
        self.assertEqual(tunnel.container_probes, 0)

    def test_container_leg_dead_kicks_tunnel(self):
        sup = self.make()
        tunnel = _FakeTunnel(host_ok=True, container_ok=False)
        d = self.down(sup, tunnel=tunnel)
        self.assertEqual(d.action, "tunnel_kick")
        self.assertEqual(d.state, "tunnel_kicked")

    def test_kick_rate_limited_then_falls_through_to_restart(self):
        sup = self.make()
        tunnel = _FakeTunnel(host_ok=True, container_ok=False)
        d = self.down(sup, tunnel=tunnel)
        self.assertEqual(d.action, "tunnel_kick")
        sup.record_kick()
        # Settle window after the kick first…
        self.now += SETTLE_SECONDS + 1
        # …then, kick still inside its 30-min spacing → H2 instead.
        self.assertLess(self.now - T0, H1_KICK_SPACING)
        d = self.down(sup, tunnel=tunnel)
        self.assertEqual(d.action, "restart_stack")

    def test_kick_opens_settle_window(self):
        sup = self.make()
        sup.record_kick()
        self.now += 60
        d = self.down(sup, tunnel=_FakeTunnel(host_ok=True, container_ok=False))
        self.assertIsNone(d.action)
        self.assertEqual(d.state, "tunnel_kicked")

    def test_healthy_tunnel_falls_through_to_restart(self):
        sup = self.make()
        tunnel = _FakeTunnel(host_ok=True, container_ok=True)
        d = self.down(sup, tunnel=tunnel)
        self.assertEqual(d.action, "restart_stack")

    def test_no_tunnel_module_skips_rung_silently(self):
        # Upstream shape: absent fork config = no tunnel object = no rung.
        sup = self.make()
        d = self.down(sup, tunnel=None)
        self.assertEqual(d.action, "restart_stack")


class TestHealingStatus(SelfHealBase):
    def test_status_object_shape(self):
        sup = self.make()
        d = self.down(sup, watchdog="fresh")
        h = sup.healing_status(d, _fresh_view(self.now))
        for key in ("state", "verdict", "watchdog_rung", "host_attempts_6h",
                    "last_action", "last_action_at", "next_eligible_at"):
            self.assertIn(key, h)
        self.assertEqual(h["state"], "watchdog_recovering")
        self.assertEqual(h["host_attempts_6h"], 0)

    def test_status_counts_recent_restarts(self):
        sup = self.make()
        sup.record_restart(verdict="network")
        d = self.down(sup)
        h = sup.healing_status(d, _yielded_view(self.now))
        self.assertEqual(h["host_attempts_6h"], 1)
        self.assertEqual(h["last_action"], "restart_stack")
        self.assertEqual(h["watchdog_rung"], "degraded")

    def test_ok_state_when_reachable(self):
        sup = self.make()
        d = sup.evaluate(reachable=True, unreachable_streak=0,
                         http_code="200", watchdog=None)
        h = sup.healing_status(d, None)
        self.assertEqual(h["state"], "ok")


class TestEvidenceHelpers(unittest.TestCase):
    def test_read_watchdog_state_unreachable_container(self):
        docker = mock.Mock()
        docker.exec.return_value = _fail()
        v = self_heal.read_watchdog_state(docker, now=T0)
        self.assertFalse(v.readable)

    def test_read_watchdog_state_parses_payload(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok(json.dumps(
            {"updated_at": int(T0) - 5, "escalate_to_host": True}))
        v = self_heal.read_watchdog_state(docker, now=T0)
        self.assertTrue(v.readable)
        self.assertTrue(v.escalate_to_host)
        self.assertFalse(v.stale)

    def test_publish_in_flight_when_receiver_staging_active(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok(
            "/var/www/html/site-generations/gen-abc.tar\n")
        self.assertTrue(self_heal.publish_in_flight(docker))

    def test_publish_idle_when_no_recent_staging(self):
        docker = mock.Mock()
        docker.exec.return_value = _ok("\n")
        self.assertFalse(self_heal.publish_in_flight(docker))

    def test_publish_check_fails_open_to_idle(self):
        # An unreachable wordpress container must not deadlock healing —
        # if we can't ask, there is no publish to protect.
        docker = mock.Mock()
        docker.exec.return_value = _fail()
        self.assertFalse(self_heal.publish_in_flight(docker))


class TestIncidentHostSide(SelfHealBase):
    """2026-08-16 as the host would now live it: watchdog hands off after 3
    restarts, host kicks the tunnel, then lands the launcher restart —
    instead of observing rc=28 for nine hours."""

    def test_handoff_to_restart_within_budget(self):
        sup = self.make()
        tunnel = _FakeTunnel(host_ok=True, container_ok=False)
        view = _yielded_view(self.now,
                             tor_restart_stamps=[self.now - 200],
                             e2e_verdict="network")
        # Watchdog's third restart just happened → settle window holds.
        d = self.down(sup, watchdog=view)
        self.assertIsNone(d.action)
        # 15 min later: tunnel triage finds the container leg dead → kick.
        self.now += SETTLE_SECONDS + 1
        view = _yielded_view(self.now,
                             tor_restart_stamps=[T0 - 200])
        d = self.down(sup, watchdog=view, tunnel=tunnel)
        self.assertEqual(d.action, "tunnel_kick")
        sup.record_kick()
        # Kick did not cure it; settle passes; kick spaced out → H2.
        self.now += SETTLE_SECONDS + 1
        d = self.down(sup, watchdog=view, tunnel=tunnel)
        self.assertEqual(d.action, "restart_stack")
        sup.record_restart(verdict="network")
        # The restart fixes it (22 seconds in real life): green clears.
        self.now += 120
        d = sup.evaluate(reachable=True, unreachable_streak=0,
                         http_code="200",
                         watchdog=_fresh_view(self.now, e2e_ok=True,
                                              serving=True, e2e_verdict=None))
        self.assertEqual(d.state, "ok")
        h = sup.healing_status(d, None)
        self.assertEqual(h["host_attempts_6h"], 1)


if __name__ == "__main__":
    unittest.main()
