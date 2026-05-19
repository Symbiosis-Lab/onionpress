"""Reachability stats + snapshot formatting for OnionPress.

In-memory counters mutated from the menubar's status loop. Emits a
grep-friendly single-line snapshot suitable for human + machine
consumption:

  snapshot: 14h12m up | reachability 851/852 ok (1 timeout, max yellow 90s) | host 12.4/16.0 GB ram, load 1.2, 8 cpu | containers 1.8 GB ram, 3.1% cpu

Counters tick every probe (silent), transitions emit their own one-line
log via the menubar's existing self.log(). Snapshots fire at sleep, at
wake, and every ~12h of continuous uptime — see issue #238.
"""

import time
from typing import Optional


class ReachabilityStats:
    """Tracks probe outcomes + yellow-state durations across one session.

    A "session" starts on construction and resets on wake. Reasonable
    fields are best-effort — a missing host or container metrics dict
    just omits that section of the snapshot.
    """

    def __init__(self, now: Optional[float] = None):
        now = now if now is not None else time.time()
        self._reset(now)

    def _reset(self, now: float) -> None:
        self.session_start_ts = now
        self.ok_count = 0
        self.fail_count = 0
        self.fail_modes: dict = {}
        self.total_yellow_seconds = 0.0
        self.longest_yellow_streak = 0.0
        self.current_yellow_start_ts: Optional[float] = None

    def reset_session(self, now: float) -> None:
        """Start a fresh session. Finalizes any in-progress yellow streak."""
        if self.current_yellow_start_ts is not None:
            streak = now - self.current_yellow_start_ts
            self.total_yellow_seconds += streak
            if streak > self.longest_yellow_streak:
                self.longest_yellow_streak = streak
        self._reset(now)

    def record_probe(self, ok: bool, code: str, duration_ms: int) -> None:
        """Record a single probe outcome. Silent — no log line emitted."""
        if ok:
            self.ok_count += 1
            return
        self.fail_count += 1
        mode = self._classify(code)
        self.fail_modes[mode] = self.fail_modes.get(mode, 0) + 1

    @staticmethod
    def _classify(code: str) -> str:
        """Bucket a check_external_reachability code into a fail mode."""
        if not code:
            return "unknown"
        if code == "000:rc=28" or code.startswith("000:rc=28"):
            return "timeout"
        if code.startswith("000:rc="):
            return "curl_" + code.split("rc=", 1)[1]
        if code.startswith("000"):
            return "no_response"
        if code == "takeover":
            return "takeover"
        if code == "302":
            return "redirector"
        if code.startswith("degraded:"):
            return "degraded"
        if code.isdigit():
            return f"http_{code}"
        return code

    def enter_yellow(self, now: float) -> None:
        """Mark the start of a yellow streak (icon flipping to not-ready)."""
        if self.current_yellow_start_ts is None:
            self.current_yellow_start_ts = now

    def exit_yellow(self, now: float) -> None:
        """Mark the end of a yellow streak (recovery to ready)."""
        if self.current_yellow_start_ts is not None:
            streak = now - self.current_yellow_start_ts
            self.total_yellow_seconds += streak
            if streak > self.longest_yellow_streak:
                self.longest_yellow_streak = streak
            self.current_yellow_start_ts = None

    def format_snapshot(
        self,
        host: Optional[dict] = None,
        containers: Optional[dict] = None,
        now: Optional[float] = None,
    ) -> str:
        """Render a single-line snapshot. Omit sections with no data."""
        now = now if now is not None else time.time()
        uptime_s = now - self.session_start_ts
        parts = [f"{_fmt_uptime(uptime_s)} up"]

        total = self.ok_count + self.fail_count
        if total > 0:
            reach = f"reachability {self.ok_count}/{total} ok"
            if self.fail_count:
                modes_sorted = sorted(self.fail_modes.items(), key=lambda kv: -kv[1])
                modes_str = ", ".join(f"{n} {m}" for m, n in modes_sorted)
                reach += f" ({modes_str}"
                # Include the currently-in-progress streak in the max if it
                # already exceeds the historical longest.
                effective_longest = self.longest_yellow_streak
                if self.current_yellow_start_ts is not None:
                    cur = now - self.current_yellow_start_ts
                    if cur > effective_longest:
                        effective_longest = cur
                if effective_longest > 0:
                    reach += f", max yellow {int(effective_longest)}s"
                reach += ")"
            parts.append(reach)

        host_part = _fmt_host(host)
        if host_part:
            parts.append(host_part)

        ctn_part = _fmt_containers(containers)
        if ctn_part:
            parts.append(ctn_part)

        return "snapshot: " + " | ".join(parts)


def _fmt_uptime(seconds: float) -> str:
    """Format seconds as '14h12m' / '3m17s' / '45s'."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m{s % 60:02d}s" if s % 60 else f"{m}m"
    h = m // 60
    return f"{h}h{m % 60:02d}m"


def _fmt_host(host) -> str:
    if not host:
        return ""
    bits = []
    used = host.get("ram_used_gb")
    total = host.get("ram_total_gb")
    if used is not None and total is not None:
        bits.append(f"{used:.1f}/{total:.1f} GB ram")
    elif total is not None:
        bits.append(f"{total:.1f} GB ram")
    load = host.get("load_1m")
    if load is not None:
        bits.append(f"load {load:.2f}")
    ncpu = host.get("cpu_count")
    if ncpu is not None:
        bits.append(f"{ncpu} cpu")
    return "host " + ", ".join(bits) if bits else ""


def _fmt_containers(containers) -> str:
    """Format the OnionPress section of the snapshot.

    Prefers onionpress_ram_mb (full host footprint, includes Colima VM on
    Mac) over total_ram_mb (in-container only). CPU% always comes from
    docker stats.
    """
    if not containers:
        return ""
    bits = []
    ram_mb = containers.get("onionpress_ram_mb")
    if ram_mb is None:
        ram_mb = containers.get("total_ram_mb")
    if ram_mb is not None:
        if ram_mb >= 1024:
            bits.append(f"{ram_mb / 1024:.1f} GB ram")
        else:
            bits.append(f"{int(ram_mb)} MB ram")
    cpu = containers.get("total_cpu_percent")
    if cpu is not None:
        bits.append(f"{cpu:.1f}% cpu")
    return "onionpress " + ", ".join(bits) if bits else ""
