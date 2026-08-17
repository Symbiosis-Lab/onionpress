"""Host-side self-healing supervisor (self-healing-design.md §3.2, Actor 2).

The tor container's watchdog (Actor 1) climbs its in-container ladder and,
when its restarts change nothing, yields by writing ``escalate_to_host:
true`` into ``/var/lib/onionpress/watchdog-state.json``. Before 2026-08-16
that file had no reader: the onion was end-to-end dead for ~9 hours while
the escalation terminated into a file nobody consumed, and the fix — a full
``launcher restart`` — took 22 seconds once a human finally ran it.

This module is the missing reader and the actor that owns that fix. It is a
pure decision engine: callers (``src/menubar.py`` on macOS,
``linux/onionpress-service.py`` on Linux) feed it evidence each status
cycle and execute the actions it decides with their own plumbing. Nothing
in here shells out except the two small evidence helpers
(:func:`read_watchdog_state`, :func:`publish_in_flight`), which take an
injected ``docker`` wrapper.

The engine acts only when **all** of these hold (§3.2):

1. The host's own independent probe agrees the onion is unreachable for
   >= ``AGREE_CYCLES`` consecutive cycles, and the failure is not a hub
   takeover (takeover means visitors ARE being served; healing is reclaim,
   not restart).
2. The watchdog has yielded — ``escalate_to_host`` set, or its state file
   is stale/unreadable, or the container is gone (watchdog dead: the host
   is the only actor left).
3. No publish is in flight, the app is not stopping/quitting, and we are
   outside the settle window that follows ANY restart action by either
   actor.

Anti-flap rules are explicit and persisted in ``app_support`` so an app
relaunch cannot reset them (the watchdog's own persistence lesson):
``H2_BUDGET`` stack restarts per rolling ``H2_BUDGET_WINDOW``, spaced
``H2_SPACING_SECONDS`` plus uniform jitter; one tunnel kick per
``H1_KICK_SPACING``; ``SETTLE_SECONDS`` of quiet after any restart.
After the budget: honest quiescence (``given_up``) until a probe-confirmed
green — visitors are meanwhile covered by the hub takeover → Wayback chain.
"""

import dataclasses
import json
import os
import random
import time
from typing import List, Optional

# Where the tor-watchdog publishes its ladder state (inside onionpress-tor).
WATCHDOG_STATE_PATH = "/var/lib/onionpress/watchdog-state.json"
# The watchdog rewrites its state at least every ~15s; 120s of silence means
# it is wedged or dead either way.
WATCHDOG_STALE_SECONDS = 120

# Gate 1: host-side agreement debounce (matches the menubar's existing
# 2-consecutive-failures yellow debounce).
AGREE_CYCLES = 2

# Settle window after ANY restart action by either actor: post-restart
# descriptor propagation takes minutes, and restarting into it is how
# self-healing becomes self-harm.
SETTLE_SECONDS = 900

# H2 (full stack restart) anti-flap: min spacing + uniform jitter between
# actions; hard budget per rolling window.
H2_SPACING_SECONDS = 45 * 60
H2_JITTER_MAX_SECONDS = 10 * 60
H2_BUDGET = 2
H2_BUDGET_WINDOW = 6 * 3600

# H1 (fork-only tunnel kick): max one per this interval.
H1_KICK_SPACING = 30 * 60

PERSIST_FILENAME = "self-heal.json"

# Recent write activity under the moss receiver's staging area counts as a
# publish in flight. Age-bounded so a crashed upload's leftovers cannot veto
# healing forever. (Receiver v1.3 may later expose an explicit busy marker;
# until then this observes the receiver's own on-disk protocol:
# <id>.tar / <id>.tmp exist only between upload arrival and the atomic
# rename, and PHP streams in-progress multipart bodies to /tmp/php*.)
GENERATIONS_DIR = "/var/www/html/site-generations"
PUBLISH_RECENT_MINUTES = 10


def _default_jitter() -> float:
    return random.uniform(0, H2_JITTER_MAX_SECONDS)


@dataclasses.dataclass
class WatchdogView:
    """Parsed watchdog-state.json as seen from the host, plus freshness."""

    readable: bool = False
    updated_at: float = 0.0
    stale: bool = True
    serving: Optional[bool] = None
    e2e_ok: Optional[bool] = None
    e2e_verdict: Optional[str] = None
    escalate_to_host: bool = False
    handoff_reason: str = ""
    degraded: bool = False
    restarts_this_outage: int = 0
    tor_restart_stamps: List[float] = dataclasses.field(default_factory=list)


def parse_watchdog_state(text: Optional[str], now: float) -> WatchdogView:
    """Parse the state file's text; unreadable/garbage → an unreadable view."""
    if not text:
        return WatchdogView()
    try:
        payload = json.loads(text)
    except ValueError:
        return WatchdogView()
    if not isinstance(payload, dict):
        return WatchdogView()
    updated_at = payload.get("updated_at") or 0
    try:
        updated_at = float(updated_at)
    except (TypeError, ValueError):
        updated_at = 0.0
    stamps = payload.get("tor_restart_stamps") or []
    if not isinstance(stamps, list):
        stamps = []
    stamps = [float(t) for t in stamps if isinstance(t, (int, float))]
    return WatchdogView(
        readable=True,
        updated_at=updated_at,
        stale=(now - updated_at) > WATCHDOG_STALE_SECONDS,
        serving=payload.get("serving"),
        e2e_ok=payload.get("e2e_ok"),
        e2e_verdict=payload.get("e2e_verdict"),
        escalate_to_host=bool(payload.get("escalate_to_host")),
        handoff_reason=str(payload.get("handoff_reason") or ""),
        degraded=bool(payload.get("degraded")),
        restarts_this_outage=int(payload.get("restarts_this_outage") or 0),
        tor_restart_stamps=stamps,
    )


def read_watchdog_state(docker, now: Optional[float] = None) -> WatchdogView:
    """One ``docker exec cat`` per cycle — the host's read of Actor 1."""
    now = now if now is not None else time.time()
    try:
        r = docker.exec("onionpress-tor", ["cat", WATCHDOG_STATE_PATH],
                        timeout=10, quiet=True)
    except Exception:
        return WatchdogView()
    return parse_watchdog_state(r.stdout if r.ok else None, now)


def publish_in_flight(docker) -> bool:
    """True when the moss receiver shows recent upload/apply activity."""
    probe = (
        "find {gen} -mindepth 1 -maxdepth 1 "
        "\\( -name '*.tar' -o -name '*.tmp' \\) -mmin -{age} 2>/dev/null "
        "| head -1; "
        "find /tmp -maxdepth 1 -name 'php*' -mmin -{age} 2>/dev/null | head -1"
    ).format(gen=GENERATIONS_DIR, age=PUBLISH_RECENT_MINUTES)
    try:
        r = docker.exec("onionpress-wordpress", ["sh", "-c", probe],
                        timeout=10, quiet=True)
    except Exception:
        return False
    return bool(r.ok and r.stdout.strip())


@dataclasses.dataclass
class Decision:
    """One evaluation's outcome. ``action`` is for the caller to execute."""

    action: Optional[str]  # None | "tunnel_kick" | "restart_stack"
    state: str             # healing state string for status.json (§3.3)
    reason: str = ""
    verdict: Optional[str] = None
    next_eligible_at: Optional[float] = None
    notify: Optional[str] = None  # one-shot loud log line, if any


def _watchdog_rung(view: Optional[WatchdogView]) -> str:
    if view is None:
        return "unknown"
    if not view.readable:
        return "dead"
    if view.escalate_to_host or view.degraded:
        return "degraded"
    if view.restarts_this_outage:
        return "restart-tor"
    if view.e2e_ok is False or view.serving is False:
        return "recovering"
    return "idle"


class SelfHealSupervisor:
    """Decision engine + persisted anti-flap ledger for Actor 2."""

    def __init__(self, state_dir, log_func=None, now_func=time.time,
                 jitter_func=_default_jitter):
        self._path = os.path.join(state_dir, PERSIST_FILENAME)
        self._log = log_func or (lambda msg: None)
        self._now = now_func
        self._jitter = jitter_func

        self._restart_stamps: List[float] = []
        self._kick_stamps: List[float] = []
        self._given_up = False
        self._given_up_notified = False
        self._tunnel_down_notified = False
        self._last_action: Optional[str] = None
        self._last_action_at: Optional[float] = None
        self._next_eligible_at: Optional[float] = None
        self._last_verdict: Optional[str] = None
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return

        def _stamps(key):
            raw = data.get(key) or []
            if not isinstance(raw, list):
                return []
            return [float(t) for t in raw if isinstance(t, (int, float))]

        self._restart_stamps = _stamps("restart_stamps")
        self._kick_stamps = _stamps("kick_stamps")
        self._given_up = bool(data.get("given_up"))
        self._given_up_notified = bool(data.get("given_up_notified"))
        self._tunnel_down_notified = bool(data.get("tunnel_down_notified"))
        self._last_action = data.get("last_action")
        self._last_action_at = data.get("last_action_at")
        self._next_eligible_at = data.get("next_eligible_at")
        self._last_verdict = data.get("last_verdict")

    def _save(self):
        payload = {
            "restart_stamps": self._restart_stamps,
            "kick_stamps": self._kick_stamps,
            "given_up": self._given_up,
            "given_up_notified": self._given_up_notified,
            "tunnel_down_notified": self._tunnel_down_notified,
            "last_action": self._last_action,
            "last_action_at": self._last_action_at,
            "next_eligible_at": self._next_eligible_at,
            "last_verdict": self._last_verdict,
        }
        tmp = self._path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, self._path)
        except OSError as e:
            self._log(f"self-heal: could not persist state: {e}")

    # -- action ledger (callers invoke these after executing an action) ----

    def record_restart(self, verdict: Optional[str] = None):
        now = self._now()
        self._restart_stamps = [t for t in self._restart_stamps
                                if now - t < H2_BUDGET_WINDOW * 2]
        self._restart_stamps.append(now)
        self._last_action = "restart_stack"
        self._last_action_at = now
        self._next_eligible_at = now + H2_SPACING_SECONDS + self._jitter()
        if verdict:
            self._last_verdict = verdict
        self._save()

    def record_kick(self):
        now = self._now()
        self._kick_stamps = [t for t in self._kick_stamps
                             if now - t < H2_BUDGET_WINDOW]
        self._kick_stamps.append(now)
        self._last_action = "tunnel_kick"
        self._last_action_at = now
        self._save()

    # -- evidence helpers --------------------------------------------------

    def _watchdog_yielded(self, view: WatchdogView):
        if not view.readable:
            return True, "watchdog state unreadable or container not running"
        if view.stale:
            return True, "watchdog state stale"
        if view.escalate_to_host:
            return True, view.handoff_reason or "watchdog requested escalation"
        return False, ""

    def _settle_source(self, view: WatchdogView, now: float):
        """Latest restart-class action by either actor: (stamp, kind)."""
        candidates = []
        if view.tor_restart_stamps:
            candidates.append((max(view.tor_restart_stamps), "watchdog"))
        if self._restart_stamps:
            candidates.append((max(self._restart_stamps), "restart_stack"))
        if self._kick_stamps:
            candidates.append((max(self._kick_stamps), "tunnel_kick"))
        if not candidates:
            return None, None
        return max(candidates)

    def _clear_outage(self):
        changed = (self._given_up or self._given_up_notified
                   or self._tunnel_down_notified or self._last_verdict)
        if changed:
            self._log("self-heal: onion confirmed reachable — clearing "
                      "give-up state (restart budget keeps rolling)")
        self._given_up = False
        self._given_up_notified = False
        self._tunnel_down_notified = False
        self._last_verdict = None
        if changed:
            self._save()

    # -- the decision table (§3.2) -----------------------------------------

    def evaluate(self, *, reachable, unreachable_streak, http_code=None,
                 stack_running=True, stopping=False, publish_in_flight=False,
                 watchdog=None, tunnel=None) -> Decision:
        now = self._now()
        view = watchdog if watchdog is not None else WatchdogView()

        # Takeover: someone (our hub) is serving our address. Never restart
        # into it — reclaim (heartbeat + republish) is the cure.
        if http_code == "takeover":
            return Decision(None, "reclaiming", verdict="takeover",
                            reason="hub takeover active — reclaim, not restart")

        # Probe-confirmed green clears the outage; unknown clears nothing.
        if reachable is True:
            self._clear_outage()
            return Decision(None, "ok")
        if unreachable_streak <= 0 or reachable is None:
            return Decision(None, "ok", reason="no confirmed-down evidence")

        if stopping or not stack_running:
            return Decision(None, "ok",
                            reason="stack stopping or not running")

        verdict = view.e2e_verdict

        # Gate 1: agreement debounce.
        if unreachable_streak < AGREE_CYCLES:
            state = ("awaiting_host" if view.escalate_to_host
                     else "watchdog_recovering")
            return Decision(None, state, verdict=verdict,
                            reason="host debounce (1 of "
                                   f"{AGREE_CYCLES} cycles)")

        # Gate 2: the watchdog must have yielded (or be dead).
        yielded, why = self._watchdog_yielded(view)
        if not yielded:
            return Decision(None, "watchdog_recovering", verdict=verdict,
                            reason="watchdog still escalating in-container")

        # Budget already exhausted: honest quiescence until confirmed green.
        if self._given_up:
            return Decision(None, "given_up",
                            verdict=self._last_verdict or verdict,
                            reason="budget exhausted earlier this outage")

        # Gate 3 vetoes.
        if publish_in_flight:
            return Decision(None, "awaiting_host", verdict=verdict,
                            reason="publish in flight")

        settle_stamp, settle_kind = self._settle_source(view, now)
        if settle_stamp is not None:
            settle_until = settle_stamp + SETTLE_SECONDS
            if settle_until > now:
                state = {"restart_stack": "host_restarting",
                         "tunnel_kick": "tunnel_kicked"}.get(
                             settle_kind, "awaiting_host")
                return Decision(None, state, verdict=verdict,
                                next_eligible_at=settle_until,
                                reason=f"settle window after {settle_kind} "
                                       "restart action")

        # H1 — tunnel triage (fork-only; tunnel is None upstream).
        if tunnel is not None:
            if not tunnel.probe_host_leg():
                notify = None
                if not self._tunnel_down_notified:
                    self._tunnel_down_notified = True
                    self._save()
                    notify = (
                        "SELF-HEAL: the tunnel upstream proxy is not "
                        "answering on the host leg — the VPN app itself "
                        "looks down. Nothing automated can fix a GUI VPN; "
                        "skipping stack restarts "
                        "(verdict=tunnel-upstream-down)."
                    )
                return Decision(None, "awaiting_host",
                                verdict="tunnel-upstream-down",
                                reason="tunnel upstream down — restarts "
                                       "would be blind",
                                notify=notify)
            if not tunnel.probe_container_leg():
                last_kick = max(self._kick_stamps) if self._kick_stamps else 0
                if now - last_kick >= H1_KICK_SPACING:
                    self._last_verdict = verdict or "tunnel-container-leg"
                    return Decision("tunnel_kick", "tunnel_kicked",
                                    verdict=verdict,
                                    reason="host leg OK, container leg dead "
                                           "— kicking the tunnel daemon")
                # Kick already spent — fall through to H2.

        # H2 — full stack restart, budgeted.
        recent = [t for t in self._restart_stamps
                  if now - t < H2_BUDGET_WINDOW]
        if len(recent) >= H2_BUDGET:
            notify = None
            if not self._given_up_notified:
                self._given_up_notified = True
                notify = (
                    f"SELF-HEAL GIVING UP: {H2_BUDGET} stack restarts in "
                    f"the last {H2_BUDGET_WINDOW // 3600}h did not restore "
                    f"the onion (verdict={self._last_verdict or verdict}). "
                    "Standing down until a probe confirms recovery — "
                    "visitors are covered by the hub takeover chain. "
                    "Manual recovery: menubar Restart."
                )
            self._given_up = True
            self._save()
            return Decision(None, "given_up",
                            verdict=self._last_verdict or verdict,
                            reason="restart budget exhausted", notify=notify)

        if self._next_eligible_at and now < self._next_eligible_at:
            return Decision(None, "awaiting_host", verdict=verdict,
                            next_eligible_at=self._next_eligible_at,
                            reason="restart spacing + jitter not yet elapsed")

        return Decision("restart_stack", "host_restarting", verdict=verdict,
                        reason=view.handoff_reason
                               or "watchdog yielded; budget available")

    # -- observability (§3.3) ---------------------------------------------

    def healing_status(self, decision: Decision,
                       watchdog: Optional[WatchdogView] = None) -> dict:
        now = self._now()
        recent = [t for t in self._restart_stamps
                  if now - t < H2_BUDGET_WINDOW]
        return {
            "state": decision.state,
            "verdict": decision.verdict,
            "watchdog_rung": _watchdog_rung(watchdog),
            "host_attempts_6h": len(recent),
            "last_action": self._last_action,
            "last_action_at": (int(self._last_action_at)
                               if self._last_action_at else None),
            "next_eligible_at": (int(decision.next_eligible_at)
                                 if decision.next_eligible_at else None),
        }
