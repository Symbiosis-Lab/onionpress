"""Health checks and restart decisions for OnionPress.

Extracts the health monitoring logic from menubar.py into a testable module.
The MenubarApp (or CLI) creates a HealthMonitor and calls check() periodically.
"""

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .docker import Docker


class ServiceState(Enum):
    """Overall OnionPress service state."""
    STOPPED = "stopped"
    STARTING = "starting"
    AVAILABLE = "available"
    OFFLINE = "offline"
    STUCK = "stuck"


@dataclass
class HealthResult:
    """Result of a single health check cycle."""
    wp_healthy: bool = False
    tor_bootstrapped: bool = False
    tor_internally_ready: bool = False  # Checks 1-4 passed
    tor_externally_reachable: bool = False  # Check 5 passed (via tor-client)
    onion_address: str = ""
    bootstrap_pct: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """Service is fully operational."""
        return self.wp_healthy and self.tor_externally_reachable


# Patterns in Tor logs indicating the container is sick and restart will help.
SICK_PATTERNS = [
    "No usable guards",
    "Too many preemptive onion service circuits failed",
    "Rejected 60/60 as down",
    "Could not connect rendezvous circuit",
]

# Patterns indicating Tor is healthy (just waiting for propagation).
HEALTHY_PATTERNS = [
    "Sufficiently bootstrapped",
]


class HealthChecker:
    """Stateless health check operations.

    Each method performs a single check and returns a result.
    No state tracking — that's HealthMonitor's job.
    """

    def __init__(
        self,
        docker: Docker,
        log_func: Callable[[str], None] | None = None,
    ):
        self.docker = docker
        self.log_func = log_func

    def _log(self, msg: str) -> None:
        if self.log_func:
            self.log_func(msg)

    def check_wordpress_local(self, wp_port: int = 80) -> bool:
        """Check if WordPress responds locally inside the container."""
        result = self.docker.exec(
            "onionpress-wordpress",
            ["curl", "-sf", "--max-time", "3", f"http://localhost:{wp_port}/"],
            timeout=10,
        )
        if result.ok:
            # Check for database errors
            if "Error establishing a database connection" in result.stdout:
                return False
            return True
        return False

    def check_tor_bootstrap(self) -> tuple[bool, int]:
        """Check Tor bootstrap status from container logs.

        Returns:
            (bootstrapped, percentage) — bootstrapped is True if 100%.
        """
        result = self.docker.run(
            ["logs", "--tail", "100", "onionpress-tor"],
            timeout=15,
        )
        if not result.ok:
            return False, 0

        output = result.stdout
        pct = 0

        # Find highest bootstrap percentage
        for m in re.finditer(r"PROGRESS=(\d+)", output):
            p = int(m.group(1))
            if p > pct:
                pct = p

        # Also check for Arti's message
        if "Sufficiently bootstrapped" in output:
            pct = max(pct, 100)
        if "Bootstrapped 100%" in output:
            pct = 100

        return pct >= 100, pct

    def check_tor_hostname(self, expected_address: str = "") -> str:
        """Read the onion hostname from the Tor container.

        Returns the address, or "" if not available.
        """
        result = self.docker.exec(
            "onionpress-tor",
            ["cat", "/var/lib/tor/hidden_service/wordpress/hostname"],
            timeout=10,
        )
        addr = result.output.strip() if result.ok else ""
        if expected_address and addr and addr != expected_address:
            self._log(f"WARNING: hostname mismatch: expected={expected_address}, got={addr}")
        return addr

    def check_internal_connectivity(self) -> bool:
        """Check if WordPress is reachable from inside the Tor container."""
        result = self.docker.exec(
            "onionpress-tor",
            ["curl", "-sf", "--max-time", "5", "http://wordpress:80/"],
            timeout=10,
        )
        return result.ok

    def check_external_reachability(self, onion_address: str) -> bool:
        """Check if the onion service is reachable through the Tor network.

        Uses the independent tor-client container (not self-connection).
        """
        if not onion_address:
            return False
        result = self.docker.exec(
            "onionpress-tor-client",
            [
                "curl", "-sf", "--max-time", "30",
                "--socks5-hostname", "127.0.0.1:9050",
                f"http://{onion_address}/",
            ],
            timeout=45,
        )
        return result.ok

    def check_internet_connectivity(self) -> bool:
        """Check if the host has internet access."""
        result = self.docker.exec(
            "onionpress-wordpress",
            ["curl", "-sf", "--max-time", "5", "http://1.1.1.1/"],
            timeout=10,
        )
        return result.ok

    def tor_container_unhealthy(self) -> bool:
        """Check Tor container logs for signs of sickness.

        Returns True if restart is likely to help.
        """
        result = self.docker.run(
            ["logs", "--tail", "50", "onionpress-tor"],
            timeout=10,
        )
        if not result.ok:
            return True  # Can't read logs — assume unhealthy

        output = result.stdout

        for pattern in SICK_PATTERNS:
            if pattern in output:
                return True

        for pattern in HEALTHY_PATTERNS:
            if pattern in output:
                return False  # Healthy, just waiting

        # No recognizable patterns — don't restart without clear signals.
        # Aggressive auto-restart disrupted stress tests; default to waiting.
        return False

    def full_check(self, expected_address: str = "") -> HealthResult:
        """Run all five health checks.

        Returns a HealthResult with all fields populated.
        """
        hr = HealthResult()

        # Check 1: WordPress local health
        hr.wp_healthy = self.check_wordpress_local()

        # Check 2: Tor bootstrap
        hr.tor_bootstrapped, hr.bootstrap_pct = self.check_tor_bootstrap()

        # Check 3: Hostname
        hr.onion_address = self.check_tor_hostname(expected_address)
        if not hr.onion_address:
            hr.errors.append("No onion address found")

        # Check 4: Internal connectivity (Tor → WordPress)
        if hr.wp_healthy and hr.tor_bootstrapped:
            internal = self.check_internal_connectivity()
            hr.tor_internally_ready = internal
            if not internal:
                hr.errors.append("WordPress not reachable from Tor container")

        # Check 5: External reachability (via tor-client)
        if hr.tor_internally_ready and hr.onion_address:
            hr.tor_externally_reachable = self.check_external_reachability(hr.onion_address)
            if not hr.tor_externally_reachable:
                hr.errors.append("Onion service not yet reachable through Tor network")

        return hr


# -- Thresholds (from menubar.py) --

YELLOW_TO_STUCK_SECONDS = 300      # 5 min in yellow → display "stuck"
YELLOW_TO_RESTART_SECONDS = 120    # 2 min in yellow → eligible for restart
RESTART_COOLDOWN_SECONDS = 300     # 5 min between auto-restarts
RECLAIM_RETRY_SECONDS = 60         # Retry OnionHeaven reclaim every 60s
POLL_READY_SECONDS = 30            # Poll interval when ready
POLL_OFFLINE_SECONDS = 10          # Poll interval when offline
POLL_STARTING_SECONDS = 5          # Poll interval when starting/stuck


@dataclass
class HealthState:
    """Mutable state tracked across health check cycles."""
    yellow_since: float | None = None  # Timestamp when entered yellow
    was_ready: bool = False            # Were we ever ready this session?
    wordpress_confirmed: bool = False  # WP responded at least once
    last_bootstrap_pct: int = 0
    bootstrap_stall_count: int = 0
    last_auto_restart: float = 0
    has_internet: bool = True
    tor_internally_ready: bool = False
    # OnionHeaven reclaim
    reclaim_succeeded: bool = False
    reclaim_in_flight: bool = False
    reclaim_last_attempt: float = 0


class HealthMonitor:
    """Stateful health monitor that tracks changes across check cycles.

    Call evaluate() with a HealthResult to get the current state and
    any restart decisions.
    """

    def __init__(self, log_func: Callable[[str], None] | None = None):
        self.state = HealthState()
        self.log_func = log_func

    def _log(self, msg: str) -> None:
        if self.log_func:
            self.log_func(msg)

    def evaluate(self, result: HealthResult, is_running: bool = True) -> ServiceState:
        """Evaluate a health check result and update state.

        Args:
            result: The latest HealthResult from HealthChecker.full_check().
            is_running: Whether the container stack is supposed to be running.

        Returns:
            The current ServiceState.
        """
        now = time.time()

        if not is_running:
            return ServiceState.STOPPED

        # Track WordPress confirmation
        if result.wp_healthy:
            self.state.wordpress_confirmed = True

        # Track internal readiness
        self.state.tor_internally_ready = result.tor_internally_ready

        if result.ready:
            # Fully operational
            if not self.state.was_ready:
                self._log("Service is now available")
            self.state.was_ready = True
            self.state.yellow_since = None
            self.state.bootstrap_stall_count = 0
            self.state.reclaim_succeeded = False
            self.state.reclaim_in_flight = False
            return ServiceState.AVAILABLE

        if self.state.was_ready and not result.ready:
            # Was ready, now degraded
            if self.state.yellow_since is None:
                self.state.yellow_since = now
                self.state.bootstrap_stall_count = 0
                self.state.last_auto_restart = 0  # Allow immediate restart
                self._log("Service became unreachable — reconnecting")

        # Track bootstrap progress
        if result.bootstrap_pct > self.state.last_bootstrap_pct:
            self.state.bootstrap_stall_count = 0
        elif result.bootstrap_pct == self.state.last_bootstrap_pct and result.bootstrap_pct > 0:
            self.state.bootstrap_stall_count += 1
        self.state.last_bootstrap_pct = result.bootstrap_pct

        if self.state.yellow_since is None:
            self.state.yellow_since = now

        # Determine display state
        if not self.state.has_internet:
            return ServiceState.OFFLINE

        yellow_duration = now - self.state.yellow_since if self.state.yellow_since else 0
        if yellow_duration > YELLOW_TO_STUCK_SECONDS:
            return ServiceState.STUCK

        return ServiceState.STARTING

    def should_restart_tor(self, tor_unhealthy: bool) -> bool:
        """Decide whether to auto-restart the Tor container.

        Args:
            tor_unhealthy: Result of HealthChecker.tor_container_unhealthy().

        Returns:
            True if Tor should be restarted.
        """
        now = time.time()

        if self.state.yellow_since is None:
            return False

        yellow_duration = now - self.state.yellow_since
        cooldown = now - self.state.last_auto_restart

        if (
            yellow_duration > YELLOW_TO_RESTART_SECONDS
            and cooldown > RESTART_COOLDOWN_SECONDS
            and tor_unhealthy
        ):
            self.state.last_auto_restart = now
            self._log("Tor container unhealthy after 2min — restarting")
            return True

        return False

    def should_reclaim(self) -> bool:
        """Decide whether to send OnionHeaven /online reclaim.

        Returns True if internally ready but self-check failing,
        and enough time has passed since last attempt.
        """
        now = time.time()

        if not self.state.tor_internally_ready:
            return False
        if self.state.reclaim_succeeded:
            return False
        if self.state.reclaim_in_flight:
            return False
        if (now - self.state.reclaim_last_attempt) < RECLAIM_RETRY_SECONDS:
            return False

        self.state.reclaim_in_flight = True
        self.state.reclaim_last_attempt = now
        return True

    def poll_interval(self, state: ServiceState) -> int:
        """Return the recommended poll interval in seconds."""
        if state == ServiceState.AVAILABLE:
            return POLL_READY_SECONDS
        if state == ServiceState.OFFLINE:
            return POLL_OFFLINE_SECONDS
        return POLL_STARTING_SECONDS
