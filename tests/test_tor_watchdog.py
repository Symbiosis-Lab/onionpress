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
import socket
import sys
import tempfile
import threading
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


def _probe_ok(_addr):
    return {"ok": True, "responded": True, "code": "200", "stage": "",
            "takeover": False, "ms": 8542}


def _probe_timeout(_addr):
    return {"ok": False, "responded": False, "code": "timeout",
            "stage": "rendezvous", "takeover": False, "ms": 45000}


class TestIsServing(unittest.TestCase):
    def test_healthy_stack_with_no_probe_evidence_yet_is_serving(self):
        # Tri-state: e2e_ok=None never blocks serving before evidence exists.
        s = _serving_state()
        self.assertIsNone(s.e2e_ok)
        self.assertTrue(tw.is_serving(s, circuit_established=True))

    def test_probe_confirmed_stack_is_serving(self):
        s = _serving_state()
        s.e2e_ok = True
        self.assertTrue(tw.is_serving(s, circuit_established=True))

    def test_local_green_with_failed_probe_is_not_serving(self):
        # THE incident line: all four local signals green, onion dead.
        s = _serving_state()
        s.e2e_ok = False
        self.assertTrue(tw.local_signals_green(s, circuit_established=True))
        self.assertFalse(tw.is_serving(s, circuit_established=True))

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


class TestProbeScheduling(unittest.TestCase):
    """maybe_e2e_probe: gating, cadence, and the asymmetric declare rules
    (3 consecutive failures => down, 1 success => up)."""

    def _run(self, state, now, fetch=_probe_ok, circuits=True):
        return tw.maybe_e2e_probe(FakeSock(), state, circuits, now=now, fetch=fetch)

    def test_first_probe_runs_immediately_once_gates_pass(self):
        s = _serving_state()
        self.assertTrue(self._run(s, 1000))
        self.assertIs(s.e2e_ok, True)
        self.assertEqual(s.e2e_code, "200")
        self.assertTrue(s.e2e_confirmed_since_restart)

    def test_probes_the_wordpress_service_address(self):
        s = _serving_state()
        seen = []

        def fetch(addr):
            seen.append(addr)
            return _probe_ok(addr)

        self._run(s, 1000, fetch=fetch)
        self.assertEqual(seen, ["abc.onion"])

    def test_override_address_wins_for_the_live_drill(self):
        s = _serving_state()
        seen = []

        def fetch(addr):
            seen.append(addr)
            return _probe_ok(addr)

        os.environ["E2E_PROBE_ADDRESS_OVERRIDE"] = "fakeaddressfordrill"
        try:
            self._run(s, 1000, fetch=fetch)
        finally:
            del os.environ["E2E_PROBE_ADDRESS_OVERRIDE"]
        self.assertEqual(seen, ["fakeaddressfordrill.onion"])

    def test_gated_off_while_sleeping_inactive_or_locally_red(self):
        s = _serving_state()
        s.sleeping = True
        self.assertFalse(self._run(s, 1000))
        s = _serving_state()
        s.services_active = False
        self.assertFalse(self._run(s, 1000))
        s = _serving_state()
        self.assertFalse(self._run(s, 1000, circuits=False))
        s = tw.WatchdogState()  # SOCKS-only container: nothing to self-fetch
        s.bootstrapped = True
        self.assertFalse(self._run(s, 1000))

    def test_ok_cadence_between_successful_probes(self):
        s = _serving_state()
        self._run(s, 1000)
        self.assertFalse(self._run(s, 1000 + tw.E2E_PROBE_INTERVAL_OK - 1))
        self.assertTrue(self._run(s, 1000 + tw.E2E_PROBE_INTERVAL_OK))

    def test_failures_switch_to_the_bad_cadence(self):
        s = _serving_state()
        self._run(s, 1000, fetch=_probe_timeout)
        self.assertEqual(s.e2e_fail_streak, 1)
        self.assertFalse(self._run(s, 1000 + tw.E2E_PROBE_INTERVAL_BAD - 1,
                                   fetch=_probe_timeout))
        self.assertTrue(self._run(s, 1000 + tw.E2E_PROBE_INTERVAL_BAD,
                                  fetch=_probe_timeout))

    def test_three_consecutive_failures_declare_down_and_classify(self):
        s = _serving_state()
        t = 1000
        for i in range(tw.E2E_FAIL_THRESHOLD):
            self.assertIsNot(s.e2e_ok, False)  # not down until the third
            self._run(s, t, fetch=_probe_timeout)
            t += tw.E2E_PROBE_INTERVAL_BAD
        self.assertIs(s.e2e_ok, False)
        self.assertFalse(tw.is_serving(s, circuit_established=True))
        # the declare-down transition runs the classifier; with the control
        # onions timing out too, the attribution is network
        self.assertEqual(s.e2e_verdict, "network")

    def test_one_success_declares_up_again(self):
        s = _serving_state()
        s.e2e_ok = False
        s.e2e_fail_streak = tw.E2E_FAIL_THRESHOLD
        self._run(s, 1000)
        self.assertIs(s.e2e_ok, True)
        self.assertEqual(s.e2e_fail_streak, 0)

    def test_a_lone_failure_does_not_flip_a_serving_verdict(self):
        # Anti-flap: one slow probe must not take a green stack to red.
        s = _serving_state()
        self._run(s, 1000)
        self._run(s, 2000, fetch=_probe_timeout)
        self.assertIs(s.e2e_ok, True)
        self.assertEqual(s.e2e_fail_streak, 1)


class TestServingLedger(unittest.TestCase):
    """update_serving_ledger — the 11-restart bug's fix. A local-green verdict
    without probe confirmation may clear NOTHING."""

    def _mid_outage(self):
        s = _serving_state()
        s.not_serving_since = 800
        s.tor_restarts = [900, 950]
        s.restarts_this_outage = 2
        s.degraded = True
        s.degraded_reason = "x"
        s.escalate_to_host = True
        s.handoff_reason = "x"
        return s

    def test_unconfirmed_local_green_clears_nothing(self):
        # The exact incident mechanic: post-restart local green wiped the
        # ledger 11 times. e2e_ok=None (fresh process, no evidence) means
        # serving computes True — and still nothing may be cleared.
        s = self._mid_outage()
        s.e2e_ok = None
        self.assertTrue(tw.is_serving(s, circuit_established=True))
        tw.update_serving_ledger(s, tw.is_serving(s, True), now=1000)
        self.assertEqual(s.tor_restarts, [900, 950])
        self.assertEqual(s.restarts_this_outage, 2)
        self.assertEqual(s.not_serving_since, 800)
        self.assertTrue(s.degraded)
        self.assertTrue(s.escalate_to_host)

    def test_probe_confirmed_serving_clears_the_whole_ledger(self):
        s = self._mid_outage()
        s.e2e_ok = True
        s.e2e_confirmed_since_restart = True
        tw.update_serving_ledger(s, tw.is_serving(s, True), now=1000)
        self.assertEqual(s.tor_restarts, [])
        self.assertEqual(s.restarts_this_outage, 0)
        self.assertEqual(s.not_serving_since, 0)
        self.assertFalse(s.degraded)
        self.assertFalse(s.escalate_to_host)
        self.assertEqual(s.handoff_reason, "")

    def test_confirmation_must_postdate_the_last_restart(self):
        # e2e_ok can still read True from before the restart action; the
        # ledger clear requires a success observed SINCE it.
        s = self._mid_outage()
        s.e2e_ok = True
        s.e2e_confirmed_since_restart = False
        tw.update_serving_ledger(s, tw.is_serving(s, True), now=1000)
        self.assertEqual(s.tor_restarts, [900, 950])
        self.assertEqual(s.not_serving_since, 800)

    def test_socks_only_container_keeps_the_trust_based_clear(self):
        # No services => nothing to fetch => circuits are all the evidence
        # there is. The old behavior stays for that container class.
        s = tw.WatchdogState()
        s.bootstrapped = True
        s.not_serving_since = 800
        s.tor_restarts = [900]
        tw.update_serving_ledger(s, tw.is_serving(s, True), now=1000)
        self.assertEqual(s.tor_restarts, [])
        self.assertEqual(s.not_serving_since, 0)

    def test_not_serving_stamps_the_outage_start_once(self):
        s = _serving_state()
        s.e2e_ok = False
        tw.update_serving_ledger(s, tw.is_serving(s, True), now=1000)
        self.assertEqual(s.not_serving_since, 1000)
        tw.update_serving_ledger(s, tw.is_serving(s, True), now=2000)
        self.assertEqual(s.not_serving_since, 1000)


class TestPerOutageAccounting(unittest.TestCase):
    """The incident's second counting bug: restarts spaced ~45min apart slid
    past the 3600s window for 4.5 hours. Restarts are now also counted per
    outage, and that counter survives the restart it counts."""

    def test_do_halt_counts_the_restart_and_resets_confirmation(self):
        s = _serving_state()
        s.e2e_confirmed_since_restart = True
        tw.do_halt(FakeSock(), s, "test", now=1000)
        self.assertEqual(s.restarts_this_outage, 1)
        self.assertEqual(s.tor_restarts, [1000])
        self.assertFalse(s.e2e_confirmed_since_restart)

    def test_do_restart_pt_also_resets_confirmation(self):
        s = _serving_state()
        s.e2e_confirmed_since_restart = True
        tw.do_restart_pt(FakeSock(), s, "test", kill=lambda p, sig: None,
                         pids=[1], now=1000)
        self.assertFalse(s.e2e_confirmed_since_restart)

    def test_three_per_outage_restarts_degrade_even_outside_the_window(self):
        # Stamps an hour+ apart never accumulate 3 in the sliding window —
        # exactly how the incident ran 11 restarts. The per-outage counter
        # (persisted across the restarts it counts) must catch it.
        s = _serving_state()
        s.e2e_ok = False
        s.not_serving_since = 1_000_000
        s.restarts_this_outage = 3
        s.tor_restarts = [1_020_000]  # only one visible to the window
        self.assertEqual(
            tw.next_escalation(s, 1_021_000, has_transport=True), "degraded")

    def test_mid_outage_state_survives_the_restart(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "watchdog-state.json")
            before = _serving_state()
            before.e2e_ok = False
            before.not_serving_since = 800
            before.restarts_this_outage = 2
            before.tor_restarts = [990]
            tw.write_state_file(before, False, path=path, now=1000)

            after = tw.WatchdogState()
            tw.load_restart_history(after, path=path, now=1000)
            self.assertEqual(after.not_serving_since, 800)
            self.assertEqual(after.restarts_this_outage, 2)
            self.assertEqual(after.tor_restarts, [990])

    def test_a_serving_state_restores_no_outage(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "watchdog-state.json")
            tw.write_state_file(_serving_state(), True, path=path, now=1000)
            after = tw.WatchdogState()
            tw.load_restart_history(after, path=path, now=1000)
            self.assertEqual(after.not_serving_since, 0)
            self.assertEqual(after.restarts_this_outage, 0)


def _probe_302(_addr):
    return {"ok": False, "responded": True, "code": "302", "stage": "",
            "takeover": False, "ms": 900}


def _probe_responds(_addr):
    return {"ok": True, "responded": True, "code": "200", "stage": "",
            "takeover": False, "ms": 2000}


class TestVerdictTaxonomy(unittest.TestCase):
    """classify_down — §3.1's failure attribution. Wrong attribution is wasted
    rungs: HS rebuilds against a dead tunnel, restarts against a takeover."""

    def _down_state(self):
        s = _serving_state()
        s.e2e_ok = False
        s.e2e_fail_streak = tw.E2E_FAIL_THRESHOLD
        return s

    def test_takeover_marker_wins_without_touching_the_network(self):
        s = self._down_state()
        s.last_probe_takeover = True
        calls = []

        def fetch(addr):
            calls.append(addr)
            return _probe_responds(addr)

        v = tw.classify_down(FakeSock(), s, fetch=fetch)
        self.assertEqual(v, "takeover")
        self.assertEqual(s.e2e_verdict, "takeover")
        self.assertEqual(calls, [])  # no control fetch needed — we HAVE a response

    def test_no_reachable_control_onion_means_network(self):
        s = self._down_state()
        calls = []

        def fetch(addr):
            calls.append(addr)
            return _probe_timeout(addr)

        v = tw.classify_down(FakeSock(), s, fetch=fetch)
        self.assertEqual(v, "network")
        # both the hub and the fallback were given their chance
        self.assertEqual(len(calls), len(tw.CONTROL_ONIONS))

    def test_the_fallback_control_onion_can_save_the_attribution(self):
        # Hub down must not read as 'our transport is dead'.
        s = self._down_state()
        responses = [_probe_timeout, _probe_responds]

        def fetch(addr):
            return responses.pop(0)(addr)

        sock = FakeSock()
        v = tw.classify_down(sock, s, fetch=fetch)
        self.assertEqual(v, "")  # control OK -> HSFETCH now in flight
        self.assertEqual(s.hsfetch_pending, "abc")
        self.assertTrue(any(c == "HSFETCH abc" for c in sock.sent))

    def test_control_ok_and_hsfetch_failed_means_descriptor(self):
        s = self._down_state()
        s.hsfetch_failed_reason = "NOT_FOUND"
        v = tw.classify_down(FakeSock(), s, fetch=_probe_responds)
        self.assertEqual(v, "descriptor")

    def test_control_ok_and_hsfetch_received_means_intro_wedge(self):
        # The tor#19522/#8864 class: descriptor fine, intro circuits dead.
        s = self._down_state()
        s.hsfetch_received = True
        v = tw.classify_down(FakeSock(), s, fetch=_probe_responds)
        self.assertEqual(v, "intro-wedge")

    def test_hsfetch_is_not_reissued_while_pending(self):
        s = self._down_state()
        s.hsfetch_pending = "abc"
        s.hsfetch_sent_at = 1000
        sock = FakeSock()
        v = tw.classify_down(sock, s, fetch=_probe_responds, now=1010)
        self.assertEqual(v, "")
        self.assertFalse(any("HSFETCH" in c for c in sock.sent))

    def test_a_lost_hsfetch_answer_is_reissued_after_the_window(self):
        # Control-connection churn can eat the HS_DESC answer; a pending
        # query must not wedge the verdict at 'undetermined' forever.
        s = self._down_state()
        s.hsfetch_pending = "abc"
        s.hsfetch_sent_at = 1000
        sock = FakeSock()
        v = tw.classify_down(sock, s, fetch=_probe_responds,
                             now=1000 + tw.HSFETCH_RETRY_AFTER + 1)
        self.assertEqual(v, "")
        self.assertTrue(any(c == "HSFETCH abc" for c in sock.sent))

    def test_takeover_from_302_shape(self):
        s = _serving_state()
        t = 1000
        for _ in range(tw.E2E_FAIL_THRESHOLD):
            tw.maybe_e2e_probe(FakeSock(), s, True, now=t, fetch=_probe_302)
            t += tw.E2E_PROBE_INTERVAL_BAD
        self.assertEqual(s.e2e_verdict, "takeover")


class TestHsDescFetchEvents(unittest.TestCase):
    """HSFETCH answers arrive as async HS_DESC events; the parser must split
    fetch results for OUR pending query from upload failures."""

    def test_received_for_the_pending_fetch(self):
        s = tw.WatchdogState()
        s.hsfetch_pending = "abc"
        tw.process_event("650 HS_DESC RECEIVED abc NO_AUTH $D1 descid",
                         FakeSock(), s)
        self.assertTrue(s.hsfetch_received)
        self.assertEqual(s.hsfetch_pending, "")

    def test_failed_for_the_pending_fetch_records_the_reason(self):
        s = tw.WatchdogState()
        s.hsfetch_pending = "abc"
        tw.process_event("650 HS_DESC FAILED abc NO_AUTH $D1 REASON=NOT_FOUND",
                         FakeSock(), s)
        self.assertEqual(s.hsfetch_failed_reason, "NOT_FOUND")
        self.assertEqual(s.hsfetch_pending, "")
        # and it is NOT misread as a descriptor-upload failure
        self.assertFalse(s.hs_desc_upload_failed_since_recovery)

    def test_failed_for_another_address_still_counts_as_upload_failure(self):
        s = tw.WatchdogState()
        s.hsfetch_pending = "abc"
        tw.process_event("650 HS_DESC FAILED other NO_AUTH $D1 REASON=UPLOAD_REJECTED",
                         FakeSock(), s)
        self.assertTrue(s.hs_desc_upload_failed_since_recovery)
        self.assertEqual(s.hs_desc_last_failed_reason, "UPLOAD_REJECTED")
        self.assertEqual(s.hsfetch_pending, "abc")  # still waiting for ours

    def test_received_for_another_address_is_ignored(self):
        s = tw.WatchdogState()
        s.hsfetch_pending = "abc"
        tw.process_event("650 HS_DESC RECEIVED other NO_AUTH $D1 descid",
                         FakeSock(), s)
        self.assertFalse(s.hsfetch_received)
        self.assertEqual(s.hsfetch_pending, "abc")


class TestVerdictDirectedRungs(unittest.TestCase):
    """§3.2: the verdict chooses the rung. takeover => republish once, never
    restart; descriptor/intro-wedge => DEL+ADD rebuild before the heavy
    rungs; network => skip the rebuild, it cannot help."""

    def _down(self, verdict, seconds=None, **kw):
        s = _serving_state()
        s.e2e_ok = False
        s.e2e_verdict = verdict
        s.not_serving_since = 1_000_000
        for k, v in kw.items():
            setattr(s, k, v)
        return s, 1_000_000 + (seconds if seconds is not None else tw.TOR_RESTART_AFTER)

    def test_takeover_republishes_once_and_never_restarts(self):
        s, now = self._down("takeover")
        self.assertEqual(tw.next_escalation(s, now, has_transport=True), "reclaim")
        s.reclaim_republished = True
        # even hours later: reclaim is the heartbeat's job, not a restart's
        self.assertIsNone(tw.next_escalation(s, now + 99_999, has_transport=True))

    def test_descriptor_verdict_rebuilds_before_the_transport_rung(self):
        s, now = self._down("descriptor", seconds=tw.PT_RESTART_AFTER)
        self.assertEqual(tw.next_escalation(s, now, has_transport=True), "rebuild-hs")

    def test_intro_wedge_verdict_also_rebuilds_first(self):
        s, now = self._down("intro-wedge", seconds=10)
        self.assertEqual(tw.next_escalation(s, now, has_transport=True), "rebuild-hs")

    def test_rebuild_happens_once_then_the_ladder_continues(self):
        s, now = self._down("intro-wedge", hs_rebuilt_this_outage=True)
        self.assertEqual(tw.next_escalation(s, now, has_transport=True), "restart-tor")

    def test_network_verdict_skips_the_pointless_rebuild(self):
        s, now = self._down("network", seconds=tw.PT_RESTART_AFTER)
        self.assertEqual(tw.next_escalation(s, now, has_transport=True), "restart-pt")

    def test_takeover_still_yields_to_degraded_never(self):
        # A takeover is the hub covering our readers; handing off to the host
        # for a restart would fight the reclaim.
        s, now = self._down("takeover", restarts_this_outage=3)
        self.assertEqual(tw.next_escalation(s, now, has_transport=True), "reclaim")

    def test_rebuild_services_del_add_newnym_in_order_and_rearms(self):
        sock = FakeSock()
        s = _serving_state()
        s.hsfetch_received = True
        s.hsfetch_pending = "abc"
        count = tw.rebuild_services(sock, s, "verdict-intro-wedge", now=5000)
        self.assertEqual(count, 1)
        joined = "\n".join(sock.sent)
        self.assertLess(joined.index("DEL_ONION"), joined.index("ADD_ONION"))
        self.assertLess(joined.index("ADD_ONION"), joined.index("SIGNAL NEWNYM"))
        # publish monitor re-armed for the new window
        self.assertEqual(s.last_recovery_time, 5000)
        self.assertEqual(s.last_recovery_trigger, "verdict-intro-wedge")
        self.assertFalse(s.hs_desc_uploaded_since_recovery)
        # stale HSFETCH evidence dropped — the descriptor it judged is gone
        self.assertFalse(s.hsfetch_received)
        self.assertEqual(s.hsfetch_pending, "")


class TestHandoff(unittest.TestCase):
    """Rung 4 is no longer a dead end: it writes the escalation request the
    host supervisor (step 3) acts on, then goes quiescent."""

    def test_do_degrade_requests_host_escalation(self):
        s = _serving_state()
        s.e2e_verdict = "network"
        tw.do_degrade(s, "3 restarts this outage changed nothing")
        self.assertTrue(s.degraded)
        self.assertTrue(s.escalate_to_host)
        self.assertEqual(s.handoff_reason, "3 restarts this outage changed nothing")

    def test_degraded_means_quiescent_until_evidence(self):
        s = _serving_state()
        s.e2e_ok = False
        s.not_serving_since = 1000
        tw.do_degrade(s, "x")
        self.assertIsNone(tw.next_escalation(s, 999_999, has_transport=True))


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

    def test_publishes_the_e2e_evidence_and_handoff_fields(self):
        # §3.3 additions — what the host-side supervisor (step 3) will read.
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "watchdog-state.json")
            s = _serving_state()
            s.e2e_ok = False
            s.e2e_code = "timeout"
            s.e2e_verdict = "network"
            s.e2e_checked_at = 1200
            s.e2e_fail_streak = 3
            s.not_serving_since = 900
            s.restarts_this_outage = 2
            s.escalate_to_host = True
            s.handoff_reason = "3 restarts this outage changed nothing"
            tw.write_state_file(s, circuit_established=True, path=path, now=1234)
            with open(path) as f:
                payload = json.load(f)

        self.assertIs(payload["e2e_ok"], False)
        self.assertEqual(payload["e2e_code"], "timeout")
        self.assertEqual(payload["e2e_verdict"], "network")
        self.assertEqual(payload["e2e_checked_at"], 1200)
        self.assertEqual(payload["e2e_fail_streak"], 3)
        self.assertEqual(payload["restarts_this_outage"], 2)
        self.assertTrue(payload["escalate_to_host"])
        self.assertEqual(payload["handoff_reason"],
                         "3 restarts this outage changed nothing")
        # and serving now carries the e2e verdict: local green + e2e False
        self.assertFalse(payload["serving"])

    def test_e2e_ok_is_null_until_the_first_probe(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "watchdog-state.json")
            tw.write_state_file(_serving_state(), True, path=path, now=1234)
            with open(path) as f:
                payload = json.load(f)
        self.assertIsNone(payload["e2e_ok"])
        self.assertIsNone(payload["e2e_checked_at"])


class FakeSocks5Server:
    """In-process SOCKS5 server with scripted behavior, one connection at a time.

    Modes: "ok" (handshake, then send http_response), "hang" (accept the
    CONNECT and never reply — the rendezvous-timeout shape), "unreachable"
    (SOCKS reply 0x04 host unreachable — what tor sends when it cannot
    build the circuit).
    """

    def __init__(self, mode="ok",
                 http_response=b"HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\nhello"):
        self.mode = mode
        self.http_response = http_response
        self.requested_host = None
        self.request = b""
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _read_exact(self, conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return buf
            buf += chunk
        return buf

    def _serve(self):
        conn = None
        try:
            conn, _ = self.listener.accept()
            conn.settimeout(5)
            greeting = self._read_exact(conn, 2)
            if len(greeting) < 2:
                return
            self._read_exact(conn, greeting[1])  # auth methods offered
            conn.sendall(b"\x05\x00")            # no-auth accepted
            self._read_exact(conn, 4)            # VER CMD RSV ATYP (domain expected)
            alen = self._read_exact(conn, 1)[0]
            self.requested_host = self._read_exact(conn, alen).decode()
            self._read_exact(conn, 2)            # destination port
            if self.mode == "hang":
                time.sleep(3)
                return
            if self.mode == "unreachable":
                conn.sendall(b"\x05\x04\x00\x01" + b"\x00" * 6)
                return
            conn.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
            self.request = conn.recv(65536)
            conn.sendall(self.http_response)
        except OSError:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self):
        try:
            self.listener.close()
        except OSError:
            pass


class TestSocks5hProbe(unittest.TestCase):
    """socks5h_fetch is the measurement the 2026-08-16 incident proved missing:
    a genuine Tor-routed self-fetch, pure stdlib, hostname-mode CONNECT."""

    def _fetch(self, srv, **kw):
        kw.setdefault("timeout", 2)
        return tw.socks5h_fetch("selftest.onion", socks_host="127.0.0.1",
                                socks_port=srv.port, **kw)

    def test_a_200_response_is_ok_and_hostname_goes_to_the_proxy(self):
        srv = FakeSocks5Server()
        try:
            res = self._fetch(srv)
        finally:
            srv.close()
        self.assertTrue(res["ok"])
        self.assertEqual(res["code"], "200")
        self.assertTrue(res["responded"])
        self.assertFalse(res["takeover"])
        self.assertIsInstance(res["ms"], int)
        # hostname-mode: the proxy resolves, never us — .onion has no DNS
        self.assertEqual(srv.requested_host, "selftest.onion")
        self.assertIn(b"GET / HTTP/1.0", srv.request)

    def test_301_counts_as_ok_like_the_host_side_probe(self):
        srv = FakeSocks5Server(
            http_response=b"HTTP/1.0 301 Moved Permanently\r\nLocation: x\r\n\r\n")
        try:
            res = self._fetch(srv)
        finally:
            srv.close()
        self.assertTrue(res["ok"])
        self.assertEqual(res["code"], "301")

    def test_a_302_is_a_response_but_not_serving(self):
        # 302 is the OnionHeaven takeover redirector's shape (health.py parity).
        srv = FakeSocks5Server(
            http_response=b"HTTP/1.0 302 Found\r\nLocation: elsewhere\r\n\r\n")
        try:
            res = self._fetch(srv)
        finally:
            srv.close()
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "302")
        self.assertTrue(res["responded"])

    def test_the_takeover_header_marks_takeover_regardless_of_status(self):
        srv = FakeSocks5Server(
            http_response=b"HTTP/1.0 200 OK\r\nX-OnionHeaven-Takeover: 1\r\n\r\nwayback")
        try:
            res = self._fetch(srv)
        finally:
            srv.close()
        self.assertFalse(res["ok"])
        self.assertTrue(res["takeover"])
        self.assertEqual(res["code"], "200")

    def test_connection_refused_fails_at_the_connect_stage(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        res = tw.socks5h_fetch("x.onion", socks_host="127.0.0.1",
                               socks_port=port, timeout=2)
        self.assertFalse(res["ok"])
        self.assertFalse(res["responded"])
        self.assertEqual(res["stage"], "connect")
        self.assertEqual(res["code"], "refused")

    def test_a_rendezvous_that_never_completes_times_out(self):
        # The incident's signature: the CONNECT is accepted, the reply never
        # comes (rc=28 on the host side). Stage must say rendezvous, not http.
        srv = FakeSocks5Server(mode="hang")
        try:
            res = self._fetch(srv, timeout=0.3)
        finally:
            srv.close()
        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "rendezvous")
        self.assertEqual(res["code"], "timeout")

    def test_a_socks_error_reply_reports_its_code(self):
        srv = FakeSocks5Server(mode="unreachable")
        try:
            res = self._fetch(srv)
        finally:
            srv.close()
        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "rendezvous")
        self.assertEqual(res["code"], "socks-4")

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


class TestTheIncidentRegression(unittest.TestCase):
    """2026-08-16: end-to-end dead ~9 hours, serving=True through 11 restarts,
    fixed in 22 seconds by a launcher restart nothing ever requested. This is
    that day replayed against the new code — if any step regresses, the
    watchdog is lying to the reader again."""

    def test_the_incident_replayed(self):
        s = _serving_state()
        sock = FakeSock()

        # Phase 1 — all four local signals green, the probe fails through the
        # dead tunnel: serving flips False and the verdict says network,
        # because the control onion is unreachable too.
        t = 1_000_000
        for _ in range(tw.E2E_FAIL_THRESHOLD):
            self.assertTrue(tw.maybe_e2e_probe(sock, s, True, now=t,
                                               fetch=_probe_timeout))
            tw.update_serving_ledger(s, tw.is_serving(s, True), now=t)
            t += tw.E2E_PROBE_INTERVAL_BAD
        self.assertTrue(tw.local_signals_green(s, True))
        self.assertIs(s.e2e_ok, False)
        self.assertFalse(tw.is_serving(s, True))
        self.assertEqual(s.e2e_verdict, "network")
        tw.update_serving_ledger(s, tw.is_serving(s, True), now=t)
        outage = s.not_serving_since
        self.assertGreater(outage, 0)

        # Phase 2 — the ladder climbs to a Tor restart three times. Between
        # restarts the local signals come back green (the false green that
        # wiped the old ledger 11 times); with e2e unconfirmed it clears
        # nothing, and the per-outage count survives.
        for i in range(3):
            now = outage + tw.TOR_RESTART_AFTER + i * tw.TOR_RESTART_COOLDOWN
            # network verdict: no rebuild-hs offered, straight up the ladder
            self.assertEqual(tw.next_escalation(s, now, has_transport=False),
                             "restart-tor")
            tw.do_halt(sock, s, "not serving", now=now)
            self.assertEqual(s.restarts_this_outage, i + 1)
            # ...container restarts; fresh probe evidence not in yet:
            s.e2e_ok = None
            tw.update_serving_ledger(s, tw.is_serving(s, True), now=now + 300)
            self.assertEqual(s.not_serving_since, outage)   # outage not closed
            self.assertEqual(len(s.tor_restarts), i + 1)    # ledger intact
            s.e2e_ok = False  # the probe re-confirms down

        # Phase 3 — the third useless restart hands off to the host instead
        # of climbing forever.
        now += tw.TOR_RESTART_COOLDOWN
        self.assertEqual(tw.next_escalation(s, now, has_transport=False),
                         "degraded")
        tw.do_degrade(s, "3 restarts this outage changed nothing")
        self.assertTrue(s.escalate_to_host)
        self.assertIsNone(tw.next_escalation(s, now + 99_999,
                                             has_transport=True))  # quiescent

        # Phase 4 — the handoff is on disk for the host supervisor.
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "watchdog-state.json")
            tw.write_state_file(s, True, path=path, now=now)
            with open(path) as f:
                payload = json.load(f)
        self.assertFalse(payload["serving"])
        self.assertTrue(payload["escalate_to_host"])
        self.assertEqual(payload["restarts_this_outage"], 3)
        self.assertEqual(payload["e2e_verdict"], "network")

        # Phase 5 — the host fixes the tunnel; one confirmed probe clears
        # the whole ledger and ends the quiescence.
        self.assertTrue(tw.maybe_e2e_probe(sock, s, True,
                                           now=now + tw.E2E_PROBE_INTERVAL_BAD,
                                           fetch=_probe_ok))
        tw.update_serving_ledger(s, tw.is_serving(s, True), now=now + 400)
        self.assertTrue(tw.is_serving(s, True))
        self.assertEqual(s.not_serving_since, 0)
        self.assertEqual(s.tor_restarts, [])
        self.assertEqual(s.restarts_this_outage, 0)
        self.assertFalse(s.degraded)
        self.assertFalse(s.escalate_to_host)


class TestCheckStallsIntegration(unittest.TestCase):
    """One real check_stalls pass with the e2e verdict wired in: local
    signals green over the control socket, probe already declared down."""

    def test_a_pass_with_failed_probe_climbs_and_persists(self):
        answers = {
            "GETINFO status/circuit-established":
                "250-status/circuit-established=1\r\n250 OK",
        }
        sock = FakeSock(answers)
        s = _serving_state()
        s.e2e_ok = False
        s.e2e_verdict = "network"
        s.last_e2e_probe = time.time()  # not due — no real fetch in a unit test
        s.not_serving_since = time.time() - tw.TOR_RESTART_AFTER - 5

        with tempfile.TemporaryDirectory() as root:
            real_state_file = tw.STATE_FILE
            tw.STATE_FILE = os.path.join(root, "watchdog-state.json")
            try:
                tw.check_stalls(sock, s)
                with open(tw.STATE_FILE) as f:
                    payload = json.load(f)
            finally:
                tw.STATE_FILE = real_state_file

        self.assertIn("SIGNAL HALT", sock.sent)
        self.assertEqual(s.restarts_this_outage, 1)
        self.assertFalse(payload["serving"])
        self.assertEqual(payload["restarts_this_outage"], 1)
        self.assertEqual(payload["tor_restart_stamps"],
                         [int(s.tor_restarts[0])])


if __name__ == "__main__":
    unittest.main()
