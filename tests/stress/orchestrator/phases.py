"""Phase execution framework — replaces bash phase_start/phase_result/WAIT_RESULT."""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, TypeVar

T = TypeVar("T")


def fmt_duration(secs: int) -> str:
    """Format seconds as 'Xm:XXs' (e.g., 135 -> '2m:15s')."""
    return f"{secs // 60}m:{secs % 60:02d}s"


def run_parallel(
    items: list[T],
    func: Callable[[T], bool],
    logger: "StressLogger",
    max_workers: int = 5,
) -> tuple[int, int]:
    """Run func on each item with ThreadPoolExecutor (like gnu-parallel -j5).

    Shows progress dots as items complete.

    Args:
        items: List of items to process.
        func: Callable that takes one item, returns True on success.
        logger: StressLogger for progress dots.
        max_workers: Concurrency limit (default 5).

    Returns:
        (succeeded, failed) counts.
    """
    succeeded = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(func, item): item for item in items}
        for future in as_completed(futures):
            try:
                if future.result():
                    succeeded += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
            logger.progress_dot()

    logger.progress_end(f"{succeeded}/{len(items)}")
    return succeeded, failed


@dataclass
class PhaseResult:
    """Result of a phase execution."""
    success: bool
    message: str
    elapsed: int = 0  # seconds

    def __str__(self):
        return self.message


class StressLogger:
    """Logging to stdout, log file, and phase log with progress dots."""

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.log_file: str | None = None
        self.phase_log: str | None = None
        self._metrics_path: str | None = None

    def setup(self, log_file: str, phase_log: str):
        """Set log file paths after output_dir is finalized."""
        self.log_file = log_file
        self.phase_log = phase_log
        self._metrics_path = os.path.join(self.output_dir, "metrics.jsonl")
        # Start fresh phase log
        with open(self.phase_log, "w"):
            pass
        self._mid_progress = False

    @staticmethod
    def _local_ts(fmt: str = "%H:%M:%S %Z") -> str:
        """Format current local time with timezone abbreviation."""
        return datetime.now().astimezone().strftime(fmt)

    def log(self, msg: str):
        """Log a message to stdout + log file + phase log."""
        ts = self._local_ts()
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        if self.log_file:
            with open(self.log_file, "a") as f:
                f.write(line + "\n")
        if self.phase_log:
            with open(self.phase_log, "a") as f:
                if self._mid_progress:
                    f.write("\n")
                    self._mid_progress = False
                f.write(line + "\n")

    def log_json(self, fields: str):
        """Append a JSON metrics line."""
        if self._metrics_path:
            ts = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
            with open(self._metrics_path, "a") as f:
                f.write(f'{{"ts":"{ts}",{fields}}}\n')

    def progress_dot(self, count: int = 1):
        """Write progress dots to phase log only."""
        if self.phase_log and count > 0:
            with open(self.phase_log, "a") as f:
                f.write("." * count)
            self._mid_progress = True

    def progress_end(self, msg: str):
        """End a progress line in the phase log."""
        if self.phase_log:
            with open(self.phase_log, "a") as f:
                f.write(f" {msg}\n")

    def phase_start(self, phase: str, desc: str):
        """Write phase start to phase log."""
        if self.phase_log:
            ts = self._local_ts()
            with open(self.phase_log, "a") as f:
                f.write(f"[{ts}] PHASE {phase}: {desc}\n")

    def phase_result(self, phase: str, result: str):
        """Write phase result to phase log."""
        if self.phase_log:
            ts = self._local_ts()
            with open(self.phase_log, "a") as f:
                f.write(f"[{ts}]   -> {result}\n")

    def write_phase_header(self, config, oh_version: str = "unknown"):
        """Write the phase.log header with test parameters."""
        if not self.phase_log:
            return
        with open(self.phase_log, "a") as f:
            f.write(f"""====================================================================
  OnionHeaven Stress Test
  {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}
--------------------------------------------------------------------
  Sites: {config.total} total ({config.healthy} healthy, {config.failing} failing)
  Containers: {config.num_containers} x {config.per_ctr} sites/container
  OnionHeaven: {config.onionheaven_addr}
  OnionHeaven server version: {oh_version}
  Stress test version: {config.stress_version}
====================================================================

""")

    def write_summary(self, config, results: dict, run_start: float):
        """Write the summary table to phase log."""
        if not self.phase_log:
            return
        total_elapsed = int(time.time() - run_start)
        with open(self.phase_log, "a") as f:
            f.write(f"""
====================================================================
  SUMMARY — {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')} — total {fmt_duration(total_elapsed)}
--------------------------------------------------------------------
  Phase 2 (bootstrap):   {results.get('phase2', '(not run)')}
  Phase 3 (healthy):     {results.get('phase3', '(not run)')}
""")
            if config.failing > 0:
                f.write(f"""  ---
  A. Graceful (/offline + /online):
     A.1  Takeover:       {results.get('a1', '(not run)')}
     A.1v Verify 302s:    {results.get('a1v', '(not run)')}
     A.2  Recovery:       {results.get('a2', '(not run)')}
     => {results.get('a_sum', '(incomplete)')}
  ---
  B. Silent (heartbeat-only, no notifications):
     B.1  Takeover:       {results.get('b1', '(not run)')}
     B.1v Verify 302s:    {results.get('b1v', '(not run)')}
     => {results.get('b_sum', '(incomplete)')}
""")
            f.write("====================================================================\n")
