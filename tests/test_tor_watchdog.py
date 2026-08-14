"""Tests for app/Resources/docker/tor/tor-watchdog.py — the escalation ladder.

The failure these exist for: after a Mac sleep on 2026-08-08 the onion went
dark and stayed dark for ~20 minutes. Tor reported bootstrapped=100% the whole
time (stale), so every rung above DROPGUARDS — all of them gated on
`not bootstrapped` — was unreachable, and the wedged snowflake-client was
never restarted. The ladder now hangs off SERVING instead.
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest

_WATCHDOG = os.path.join(
    os.path.dirname(__file__), "..", "app", "Resources", "docker", "tor", "tor-watchdog.py"
)
_spec = importlib.util.spec_from_file_location("tor_watchdog", _WATCHDOG)
tw = importlib.util.module_from_spec(_spec)
sys.modules["tor_watchdog"] = tw
_spec.loader.exec_module(tw)


class FakeSock:
    """Records commands; answers GETINFO from a canned map."""

    def __init__(self, answers=None):
        self.sent = []
        self.answers = answers or {}

    def sendall(self, data):
        self.sent.append(data.decode().strip())

    def recv(self, _n):
        cmd = self.sent[-1] if self.sent else ""
        return (self.answers.get(cmd, "250 OK") + "\r\n").encode()


def _serving_state():
    """A healthy onion-service watchdog: bootstrapped, attached, published."""
    s = tw.WatchdogState()
    s.bootstrapped = True
    s.services = [{"service_name": "wordpress", "service_id": "abc", "key_b64": "k", "ports": ["80,127.0.0.1:8080"]}]
    s.services_active = True
    s.last_recovery_time = 0
    return s


class TestIsServing(unittest.TestCase):
    def test_healthy_stack_is_serving(self):
        self.assertTrue(tw.is_serving(_serving_state(), circuit_established=True))

    def test_bootstrapped_but_no_circuits_is_not_serving(self):
        # The exact post-sleep shape: Tor still says 100%, nobody can reach us.
        self.assertFalse(tw.is_serving(_serving_state(), circuit_established=False))

    def test_circuits_but_undelivered_descriptor_is_not_serving(self):
        s = _serving_state()
        s.last_recovery_time = 1000
        s.hs_desc_uploaded_since_recovery = False
        self.assertFalse(tw.is_serving(s, circuit_established=True))

    def test_descriptor_landing_restores_serving(self):
        s = _serving_state()
        s.last_recovery_time = 1000
        s.hs_desc_uploaded_since_recovery = True
        self.assertTrue(tw.is_serving(s, circuit_established=True))

    def test_socks_only_container_serves_on_circuits_alone(self):
        s = tw.WatchdogState()
        s.bootstrapped = True
        s.services = []
        self.assertTrue(tw.is_serving(s, circuit_established=True))


class TestLadder(unittest.TestCase):
    """`next_escalation` is the whole ladder, and it is pure."""

    def _down_for(self, seconds, **kw):
        s = _serving_state()
        s.not_serving_since = 1_000_000
        for k, v in kw.items():
            setattr(s, k, v)
        return s, 1_000_000 + seconds

    def test_serving_never_escalates(self):
        s = _serving_state()
        self.assertIsNone(tw.next_escalation(s, 1_000_000, has_transport=True))

    def test_nothing_happens_before_the_transport_rung(self):
        s, now = self._down_for(tw.PT_RESTART_AFTER - 1)
        self.assertIsNone(tw.next_escalation(s, now, has_transport=True))

    def test_transport_restart_is_due_at_its_threshold(self):
        s, now = self._down_for(tw.PT_RESTART_AFTER)
        self.assertEqual(tw.next_escalation(s, now, has_transport=True), "restart-pt")

    def test_no_transport_configured_skips_that_rung(self):
        # A direct-connection Tor has no managed proxy to restart; going
        # straight to a process restart at 180s would be gratuitous.
        s, now = self._down_for(tw.PT_RESTART_AFTER)
        self.assertIsNone(tw.next_escalation(s, now, has_transport=False))

    def test_transport_restart_is_rate_limited(self):
        s, now = self._down_for(tw.PT_RESTART_AFTER + 10,
                                last_pt_restart=1_000_000 + tw.PT_RESTART_AFTER)
        self.assertIsNone(tw.next_escalation(s, now, has_transport=True))

    def test_tor_restart_takes_over_when_the_transport_restart_did_not_help(self):
        s, now = self._down_for(tw.TOR_RESTART_AFTER,
                                last_pt_restart=1_000_000 + tw.PT_RESTART_AFTER)
        self.assertEqual(tw.next_escalation(s, now, has_transport=True), "restart-tor")

    def test_restarts_that_change_nothing_end_in_degraded(self):
        base = 1_000_000
        s, now = self._down_for(2_000, tor_restarts=[base + 1, base + 2, base + 3])
        self.assertEqual(tw.next_escalation(s, now, has_transport=True), "degraded")

    def test_degraded_stops_climbing(self):
        s, now = self._down_for(99_999, degraded=True)
        self.assertIsNone(tw.next_escalation(s, now, has_transport=True))

    def test_restarts_outside_the_window_do_not_count_toward_degraded(self):
        base = 1_000_000
        s, now = self._down_for(tw.TOR_RESTART_AFTER,
                                tor_restarts=[base - tw.DEGRADED_WINDOW - 10] * 3)
        self.assertEqual(tw.next_escalation(s, now, has_transport=True), "restart-tor")

    def test_the_ladder_never_climbs_back_down_to_the_transport_rung(self):
        # Restarting Tor already re-execs the transport, so offering the
        # smaller action again while the bigger one cools off would just make
        # the loop busier than the rung it replaced.
        base = 1_000_000
        s, now = self._down_for(tw.TOR_RESTART_AFTER + 60,
                                tor_restarts=[base + tw.TOR_RESTART_AFTER])
        self.assertIsNone(tw.next_escalation(s, now, has_transport=True))


class TestRestartTransport(unittest.TestCase):
    def test_kills_every_transport_process_then_reloads(self):
        sock = FakeSock()
        state = tw.WatchdogState()
        killed = []
        tw.do_restart_pt(sock, state, "test", kill=lambda p, s: killed.append(p), pids=[41, 42])

        self.assertEqual(killed, [41, 42])
        self.assertIn("SIGNAL RELOAD", sock.sent)

    def test_sends_no_add_onion(self):
        # The address-safety property for this rung: it cannot change the
        # address because it never touches a key or an onion service.
        sock = FakeSock()
        tw.do_restart_pt(sock, tw.WatchdogState(), "test", kill=lambda p, s: None, pids=[1])
        self.assertFalse(any("ONION" in c for c in sock.sent))

    def test_missing_process_still_records_the_attempt(self):
        # Otherwise a container with no transport retries every pass.
        state = tw.WatchdogState()
        self.assertFalse(tw.do_restart_pt(FakeSock(), state, "test", pids=[]))
        self.assertGreater(state.last_pt_restart, 0)

    def test_finds_transport_pids_from_proc(self):
        with tempfile.TemporaryDirectory() as root:
            for pid, cmd in (("7", "/usr/bin/snowflake-client\0-url\0x"),
                             ("9", "/usr/sbin/apache2\0-D\0FOREGROUND")):
                os.mkdir(os.path.join(root, pid))
                with open(os.path.join(root, pid, "cmdline"), "w") as f:
                    f.write(cmd)
            os.mkdir(os.path.join(root, "self"))  # non-numeric entries ignored
            self.assertEqual(tw._pt_pids(proc_root=root), [7])


class TestVanityAddressSurvives(unittest.TestCase):
    """The catastrophic regression: recovery that mints a NEW address.

    The user has published theirs. Every rung must either reuse the on-disk
    key or do nothing.
    """

    def _service_dir(self, root, name, key=None, pubkey=None, hostname=None):
        d = os.path.join(root, name)
        os.makedirs(d)
        if key is not None:
            with open(os.path.join(d, "hs_ed25519_secret_key"), "wb") as f:
                f.write(key)
        if pubkey is not None:
            with open(os.path.join(d, "hs_ed25519_public_key"), "wb") as f:
                f.write(pubkey)
        if hostname is not None:
            with open(os.path.join(d, "hostname"), "w") as f:
                f.write(hostname + "\n")
        return d

    def _discover(self, root, defs):
        cfg = os.path.join(root, "services.json")
        with open(cfg, "w") as f:
            json.dump(defs, f)
        real_base, real_open = tw.HS_BASE_DIR, None
        tw.HS_BASE_DIR = root
        try:
            import builtins
            real_open = builtins.open

            def fake_open(path, *a, **kw):
                if path == "/etc/tor/onion-services.json":
                    return real_open(cfg, *a, **kw)
                return real_open(path, *a, **kw)

            builtins.open = fake_open
            return tw.discover_services()
        finally:
            if real_open:
                import builtins
                builtins.open = real_open
            tw.HS_BASE_DIR = real_base

    def test_unreadable_key_for_a_published_address_refuses_new_best(self):
        with tempfile.TemporaryDirectory() as root:
            self._service_dir(root, "wordpress", key=b"too-short",
                              hostname="vanityaddress.onion")
            svcs = self._discover(root, [{"name": "wordpress", "ports": ["80,127.0.0.1:8080"]}])

            self.assertEqual(len(svcs), 1)
            self.assertTrue(svcs[0]["key_unreadable"])
            # It keeps the address it already publishes...
            self.assertEqual(svcs[0]["service_id"], "vanityaddress")
            # ...and carries no key, so nothing can ADD it under a new one.
            self.assertIsNone(svcs[0]["key_b64"])

    def test_add_onion_skips_a_service_whose_key_is_unreadable(self):
        sock = FakeSock()
        added, collisions = tw.add_all_services(sock, [{
            "service_name": "wordpress", "service_id": "vanityaddress",
            "key_b64": None, "key_unreadable": True, "ports": ["80,127.0.0.1:8080"],
        }])

        self.assertEqual((added, collisions), (0, 0))
        self.assertFalse(any("NEW:BEST" in c for c in sock.sent),
                         "NEW:BEST here would replace the user's published address")

    def test_a_healthy_service_is_added_with_its_own_key(self):
        sock = FakeSock()
        tw.add_all_services(sock, [{
            "service_name": "wordpress", "service_id": "vanityaddress",
            "key_b64": "KEYBYTES", "ports": ["80,127.0.0.1:8080"],
        }])
        self.assertTrue(any("ED25519-V3:KEYBYTES" in c for c in sock.sent))
        self.assertFalse(any("NEW:BEST" in c for c in sock.sent))


class TestRestartHistorySurvivesTheRestart(unittest.TestCase):
    """Rung 3 ends the container, so the counter cannot live in memory.

    Without this, rung 4 is unreachable: every restart resets the count and
    the ladder restarts Tor every 15 minutes forever against a dead network.
    """

    def test_stamps_round_trip_through_the_state_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "watchdog-state.json")
            before = _serving_state()
            before.tor_restarts = [990, 995]
            tw.write_state_file(before, False, path=path, now=1000)

            after = tw.WatchdogState()
            tw.load_restart_history(after, path=path, now=1000)
            self.assertEqual(after.tor_restarts, [990, 995])

    def test_stale_stamps_are_dropped_on_the_way_back_in(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "watchdog-state.json")
            before = _serving_state()
            before.tor_restarts = [1000]
            tw.write_state_file(before, False, path=path, now=1000)

            after = tw.WatchdogState()
            tw.load_restart_history(after, path=path, now=1000 + tw.DEGRADED_WINDOW + 1)
            self.assertEqual(after.tor_restarts, [])

    def test_no_state_file_is_a_clean_start(self):
        after = tw.WatchdogState()
        tw.load_restart_history(after, path="/nonexistent/state.json")
        self.assertEqual(after.tor_restarts, [])


class TestStateFile(unittest.TestCase):
    def test_publishes_a_serving_verdict_moss_can_read(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "nested", "watchdog-state.json")
            tw.write_state_file(_serving_state(), circuit_established=True, path=path, now=1234)
            with open(path) as f:
                payload = json.load(f)

            self.assertTrue(payload["serving"])
            self.assertFalse(payload["degraded"])
            self.assertEqual(payload["updated_at"], 1234)

    def test_a_degraded_stack_says_so_rather_than_going_quiet(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "watchdog-state.json")
            s = _serving_state()
            s.degraded = True
            s.degraded_reason = "network unreachable"
            s.not_serving_since = 900
            tw.write_state_file(s, circuit_established=False, path=path, now=1234)
            with open(path) as f:
                payload = json.load(f)

            self.assertFalse(payload["serving"])
            self.assertTrue(payload["degraded"])
            self.assertEqual(payload["degraded_reason"], "network unreachable")

    def test_an_unwritable_path_is_never_fatal(self):
        tw.write_state_file(_serving_state(), True, path="/proc/nope/state.json")


_SPN_ID = "archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad"


class _WarmSetFixture(unittest.TestCase):
    """Isolate the warm set from the host: every source of it, not just some.

    The warm set is built from three inputs — the service directories, the
    published content address, and TOR_WARM_ONIONS — and a test that pins it
    exactly is only honest if all three are redirected. Redirecting two was
    worse than redirecting none: `/var/lib/onionpress/onion_address` does not
    exist on a dev Mac, so the exact-set assertion passed here and would have
    failed inside the container and on every real Linux install, where the
    file exists and quietly adds an address.
    """

    def setUp(self):
        self._base = tw.HS_BASE_DIR
        self._addr_file = tw.ONION_ADDRESS_FILE
        self._env = os.environ.pop("TOR_WARM_ONIONS", None)
        tw._warned_bad_addresses.clear()
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        tw.HS_BASE_DIR = os.path.join(root.name, "hidden_service")
        os.mkdir(tw.HS_BASE_DIR)
        tw.ONION_ADDRESS_FILE = os.path.join(root.name, "onion_address")

    def tearDown(self):
        tw.HS_BASE_DIR = self._base
        tw.ONION_ADDRESS_FILE = self._addr_file
        os.environ.pop("TOR_WARM_ONIONS", None)
        if self._env is not None:
            os.environ["TOR_WARM_ONIONS"] = self._env

    def _publish_our_address(self, address):
        """Stand in for the launcher copying our hostname onto the volume."""
        with open(tw.ONION_ADDRESS_FILE, "w") as f:
            f.write(address + "\n")


class TestDescriptorWarming(_WarmSetFixture):
    """The failure this exists for: SPN calls dying because a descriptor is cold.

    Measured against archive.org's onion — ~3.6s with its descriptor cached,
    25-70s without, and Tor abandons the attempt at ~70s. Every Save Page Now
    call therefore failed wholesale after a Tor restart or a laptop sleep, and
    it read as "archive.org's onion is down" for a long time.
    """

    def test_the_spn_address_loses_its_subdomain_as_well_as_its_suffix(self):
        # HSFETCH takes the bare 56-char id. Handing it "web." earns a 552,
        # which in the log is indistinguishable from a fetch that just failed —
        # so warming would have looked like it was running and not working.
        self.assertEqual(tw._hs_address(f"web.{_SPN_ID}.onion"), _SPN_ID)

    def test_anything_that_is_not_a_v3_address_stays_off_the_control_port(self):
        self.assertIsNone(tw._hs_address("web.archive.org"))
        self.assertIsNone(tw._hs_address(""))

    def test_the_spn_default_and_our_own_address_are_warmed_with_no_config(self):
        # Both halves of the out-of-the-box set: the dependency we never
        # publish, and the address we do. Ours is what a reachability check
        # would otherwise pay the cold-descriptor penalty on.
        self._publish_our_address(f"{'d' * 56}.onion")
        self.assertEqual(tw.warm_onion_addresses(), sorted([_SPN_ID, "d" * 56]))

    def test_tor_warm_onions_adds_to_the_default_rather_than_replacing_it(self):
        os.environ["TOR_WARM_ONIONS"] = f"{'b' * 56}.onion, {'c' * 56}"
        self.assertEqual(tw.warm_onion_addresses(),
                         sorted([_SPN_ID, "b" * 56, "c" * 56]))

    def test_the_warm_set_is_ours_plus_dependencies_and_ours_is_only_ours(self):
        # The two readers this pair exists to keep apart: the warmer wants
        # everything we depend on, the HS_DESC-stall path wants only what we
        # could actually republish. One list served both and they drifted.
        self._publish_our_address("d" * 56)
        self.assertEqual(tw.our_onion_addresses(), ["d" * 56])
        self.assertIn(_SPN_ID, tw.warm_onion_addresses())
        self.assertNotIn(_SPN_ID, tw.our_onion_addresses())

    def test_only_a_cold_descriptor_costs_a_fetch(self):
        cached, cold = "a" * 56, "b" * 56
        os.environ["TOR_WARM_ONIONS"] = f"{cached} {cold}"
        sock = FakeSock({
            f"GETINFO hs/client/desc/id/{cached}":
                f"250+hs/client/desc/id/{cached}=\r\nhs-descriptor 3\r\n.\r\n250 OK",
            f"GETINFO hs/client/desc/id/{cold}": "551 Not found",
        })
        tw._hsfetch_missing_descriptors(sock)

        self.assertIn(f"HSFETCH {cold}", sock.sent)
        self.assertNotIn(f"HSFETCH {cached}", sock.sent)


class TestWarmingRunsWhereTheSpnTrafficGoes(_WarmSetFixture):
    """onionheaven is the container that has to warm, and it is the SOCKS-only one.

    onionpress-wayback-archive.php points CURLOPT_PROXY at
    socks5h://onionheaven:9050, so a gate that skipped NO_ONION_SERVICE
    containers would leave the whole change a no-op for the traffic it exists
    to speed up.
    """

    def setUp(self):
        super().setUp()
        self._write = tw.write_state_file
        tw.write_state_file = lambda *a, **kw: None

    def tearDown(self):
        tw.write_state_file = self._write
        super().tearDown()

    def _check_stalls(self, state):
        sock = FakeSock({
            "GETINFO status/circuit-established":
                "250-status/circuit-established=1\r\n250 OK",
        })
        tw.check_stalls(sock, state)
        return sock

    def _socks_only_state(self):
        """onionheaven's shape: bootstrapped, circuits, and nothing published."""
        s = tw.WatchdogState()
        s.bootstrapped = True
        return s

    def test_warming_repeats_on_its_own_interval(self):
        # The gap the old call site left: descriptors expire, laptops sleep,
        # and a single warm at bootstrap goes cold long before the next SPN run.
        state = self._socks_only_state()
        self.assertIn(f"HSFETCH {_SPN_ID}", self._check_stalls(state).sent)
        self.assertNotIn(f"HSFETCH {_SPN_ID}", self._check_stalls(state).sent)

        state.last_warm_check -= tw.WARM_DESCRIPTOR_INTERVAL
        self.assertIn(f"HSFETCH {_SPN_ID}", self._check_stalls(state).sent)

    def test_circuits_coming_back_warms_now_rather_than_an_interval_later(self):
        # Waking is exactly when the cache is empty and an SPN call is most
        # likely to land on a cold descriptor.
        state = self._socks_only_state()
        state.not_serving_since = time.time() - 10
        state.last_warm_check = time.time()  # warmed recently; timer not due
        self.assertIn(f"HSFETCH {_SPN_ID}", self._check_stalls(state).sent)

    def test_a_publication_of_our_own_is_never_interrupted(self):
        # The one window where fetching and publishing compete for the same
        # Tor. Only the serving container has it; onionheaven publishes nothing.
        state = _serving_state()
        state.last_recovery_time = time.time()
        state.hs_desc_uploaded_since_recovery = False
        sock = self._check_stalls(state)
        self.assertFalse(any(c.startswith("HSFETCH") for c in sock.sent))

    def test_a_guard_purge_re_warms_without_waiting_out_the_interval(self):
        # The purge the "serving again" path cannot see. do_dropguards sends
        # SIGNAL NEWNYM, which empties the descriptor cache — but it also fires
        # for "Failed to find node" and guard exhaustion, which happen while
        # circuit-established=1. In a SOCKS-only container is_serving() stays
        # true through both, not_serving_since is never set, and the cache
        # would sit cold for a full WARM_DESCRIPTOR_INTERVAL.
        state = self._socks_only_state()
        self.assertIn(f"HSFETCH {_SPN_ID}", self._check_stalls(state).sent)
        self.assertNotIn(f"HSFETCH {_SPN_ID}", self._check_stalls(state).sent)

        tw.process_event("No usable guards", FakeSock(), state)
        self.assertIn(f"HSFETCH {_SPN_ID}", self._check_stalls(state).sent)

    def test_a_stall_with_nothing_of_our_own_touches_nothing(self):
        # onionheaven's shape again, HS_DESC_UPLOAD_TIMEOUT after a recovery.
        # It publishes no descriptor, so a stalled upload of ours is not a
        # thing that can have happened here. The branch used to answer it by
        # NEWNYM-ing — purging the cache the SPN calls live on — and then
        # HSFETCHing archive.org, the one address it could never republish.
        state = self._socks_only_state()
        state.last_recovery_time = time.time() - tw.HS_DESC_UPLOAD_TIMEOUT - 1
        state.last_warm_check = time.time()  # isolate the branch from the warmer

        sock = self._check_stalls(state)
        self.assertNotIn("SIGNAL NEWNYM", sock.sent)
        self.assertFalse(any(c.startswith("HSFETCH") for c in sock.sent))

    def test_a_stall_that_purges_the_cache_hands_the_warm_set_back(self):
        # With an address of our own there is something to refresh, and the
        # NEWNYM that refreshes it costs every other descriptor too — so the
        # warmer has to run again on this same pass rather than 300s later.
        ours = "d" * 56
        self._publish_our_address(ours)
        state = self._socks_only_state()
        state.last_recovery_time = time.time() - tw.HS_DESC_UPLOAD_TIMEOUT - 1
        state.last_warm_check = time.time()  # not due, and about to be voided

        sock = self._check_stalls(state)
        self.assertIn("SIGNAL NEWNYM", sock.sent)
        self.assertIn(f"HSFETCH {ours}", sock.sent)
        self.assertIn(f"HSFETCH {_SPN_ID}", sock.sent)


class TestConfiguredTransports(unittest.TestCase):
    def test_reads_the_transport_tor_was_told_to_launch(self):
        with tempfile.NamedTemporaryFile("w", suffix=".torrc", delete=False) as f:
            f.write("UseBridges 1\n"
                    "ClientTransportPlugin snowflake exec /usr/bin/snowflake-client\n"
                    "Bridge snowflake 192.0.2.3:1\n")
            path = f.name
        try:
            self.assertEqual(tw.configured_transports(path), ["snowflake"])
        finally:
            os.unlink(path)

    def test_no_torrc_is_not_an_error(self):
        self.assertEqual(tw.configured_transports("/nonexistent/torrc"), [])


if __name__ == "__main__":
    unittest.main()
