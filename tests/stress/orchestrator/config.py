"""Stress test configuration — replaces 60 lines of bash defaults + arg parsing."""

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class StressConfig:
    """All stress test parameters, computed once."""
    mode: str = "worker"
    total: int = 5
    healthy: int = 0  # computed in __post_init__
    failing: int = 0  # computed in __post_init__
    per_ctr: int = 20
    onionheaven_addr: str = ""
    output_dir: str = "./onionheaven-stress-results"
    batch_size: int = 0
    no_healthcheck: bool = False
    no_timeout: bool = False
    cleanup: bool = False
    cleanup_stale: bool = False
    stale_hours: int = 2
    base_port: int = 9100

    # Computed fields
    num_containers: int = field(init=False)
    num_poll_clients: int = field(init=False)
    stress_version: str = field(init=False)
    bootstrap_timeout: int = field(init=False)
    takeover_timeout: int = field(init=False)
    recovery_timeout: int = field(init=False)
    healthy_timeout: int = field(init=False)
    redirect_verify_timeout: int = field(init=False)

    # Set during preflight
    data_dir: str = field(init=False)
    script_dir: str = field(init=False)

    # Explicit healthy/failing from user (for __post_init__ logic)
    _healthy_set: bool = field(default=False, init=False, repr=False)
    _failing_set: bool = field(default=False, init=False, repr=False)

    def __post_init__(self):
        self.data_dir = os.path.join(os.path.expanduser("~"), ".onionpress")
        from pathlib import Path
        self.script_dir = str(Path(__file__).resolve().parents[2])

        # Compute healthy/failing split
        if not self._healthy_set and not self._failing_set:
            self.failing = self.total // 2
            self.healthy = self.total - self.failing
        elif not self._healthy_set:
            self.healthy = self.total - self.failing
        elif not self._failing_set:
            self.failing = self.total - self.healthy
        else:
            self.total = self.healthy + self.failing

        # Containers
        self.num_containers = (self.total + self.per_ctr - 1) // self.per_ctr

        # Poll clients: 1 per 3 failing sites, clamped [1, 20]
        self.num_poll_clients = max(1, min(20, (self.total // 2 + 2) // 3))

        # Version stamp
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.stress_version = f"stress-test-{ts}-{os.getpid()}"

        # Timeouts
        if self.no_timeout:
            self.bootstrap_timeout = 86400
            self.takeover_timeout = 86400
            self.recovery_timeout = 86400
            self.healthy_timeout = 86400
            self.redirect_verify_timeout = 86400
        else:
            self.bootstrap_timeout = 900
            self.takeover_timeout = 600
            self.recovery_timeout = 600
            self.healthy_timeout = 600
            self.redirect_verify_timeout = 300

    @property
    def fail_start(self) -> int:
        """Global index where failing sites begin."""
        return self.total - self.failing

    @classmethod
    def from_args(cls, argv: list[str] | None = None) -> "StressConfig":
        """Parse command-line arguments into a StressConfig."""
        parser = argparse.ArgumentParser(
            description="OnionHeaven Stress Test",
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        parser.add_argument("--mode", default="worker", choices=["worker"])
        parser.add_argument("--total", type=int, default=5)
        parser.add_argument("--healthy", type=int, default=None)
        parser.add_argument("--failing", type=int, default=None)
        parser.add_argument("--per-ctr", type=int, default=20)
        parser.add_argument("--onionheaven-addr", default="")
        parser.add_argument("--output-dir", default="./onionheaven-stress-results")
        parser.add_argument("--batch-size", type=int, default=0)
        parser.add_argument("--no-healthcheck", action="store_true")
        parser.add_argument("--no-timeout", action="store_true")
        parser.add_argument("--cleanup", action="store_true")
        parser.add_argument("--cleanup-stale", action="store_true")
        parser.add_argument("--stale-hours", type=int, default=2)

        args = parser.parse_args(argv)

        if args.total < 1:
            parser.error("--total must be at least 1")

        config = cls(
            mode=args.mode,
            total=args.total,
            per_ctr=args.per_ctr,
            onionheaven_addr=args.onionheaven_addr,
            output_dir=args.output_dir,
            batch_size=args.batch_size,
            no_healthcheck=args.no_healthcheck,
            no_timeout=args.no_timeout,
            cleanup=args.cleanup,
            cleanup_stale=args.cleanup_stale,
            stale_hours=args.stale_hours,
        )
        # Set healthy/failing with tracking
        if args.healthy is not None:
            config.healthy = args.healthy
            config._healthy_set = True
        if args.failing is not None:
            config.failing = args.failing
            config._failing_set = True
        # Recompute after setting flags
        config.__post_init__()
        return config

    def workers_in_container(self, ctr_idx: int) -> int:
        """Number of workers in the given container index."""
        if ctr_idx == self.num_containers - 1:
            return self.total - ctr_idx * self.per_ctr
        return self.per_ctr

    def container_name(self, idx: int) -> str:
        return f"stress-worker-{idx}"

    # Docker volume + in-container path for the shared worker-info DB
    db_volume: str = "stress-worker-info"
    db_container_path: str = "/worker-data/worker-info.db"

    def poll_client_name(self, idx: int) -> str:
        return f"stress-poll-client-{idx}"
