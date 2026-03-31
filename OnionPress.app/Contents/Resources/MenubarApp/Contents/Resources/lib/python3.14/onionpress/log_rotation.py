"""Rotating log files for OnionPress.

Handles daily rotation with size limits, home-directory scrubbing,
and total disk usage caps for the logs directory.
"""

import glob
import os
import threading
from datetime import datetime, timezone


class RotatingLog:
    """Thread-safe rotating log writer.

    Files are named ``{log_type}-YYYY-MM-DD-NNN.log`` under *base_dir*.
    A new file is started when the UTC date changes or the current file
    exceeds *max_size* bytes.  After each roll the total size of
    *base_dir* is checked and oldest files are pruned to stay under
    *max_total_size*.
    """

    def __init__(self, base_dir, log_type, max_size=5_242_880,
                 max_total_size=104_857_600):
        self._base_dir = base_dir
        self._log_type = log_type
        self._max_size = max_size
        self._max_total_size = max_total_size

        # Pre-compute the home directory string to scrub
        self._home = os.path.expanduser("~")

        self._lock = threading.Lock()

        os.makedirs(base_dir, exist_ok=True)

        # Determine today's date and pick up any existing sequence
        self._current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._seq = self._find_next_seq(self._current_date)
        self._path = self._make_path(self._current_date, self._seq)

    # -- public API --------------------------------------------------------

    def write(self, message):
        """Append *message* to the current log file (thread-safe).

        Scrubs the user's home directory path before writing.
        Rolls to a new file if the UTC date changed or size exceeded.
        """
        message = message.replace(self._home, "~")

        with self._lock:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            rolled = False

            if today != self._current_date:
                self._current_date = today
                self._seq = self._find_next_seq(today)
                self._path = self._make_path(today, self._seq)
                rolled = True
            elif os.path.exists(self._path):
                try:
                    if os.path.getsize(self._path) >= self._max_size:
                        self._seq += 1
                        self._path = self._make_path(self._current_date, self._seq)
                        rolled = True
                except OSError:
                    pass

        # Write outside the lock — O_APPEND is atomic on POSIX
        try:
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(message)
        except Exception:
            pass

        if rolled:
            self._enforce_total_size()

    def current_path(self):
        """Return the path of the currently active log file."""
        return self._path

    def completed_files(self):
        """Return metadata for all files that are NOT the current active file.

        Each entry is ``{"name": str, "size": int, "path": str}``.
        """
        current = os.path.basename(self._path)
        result = []
        pattern = os.path.join(self._base_dir, f"{self._log_type}-*.log")
        for p in sorted(glob.glob(pattern)):
            name = os.path.basename(p)
            if name == current:
                continue
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            result.append({"name": name, "size": size, "path": p})
        return result

    # -- internals ---------------------------------------------------------

    def _make_path(self, date_str, seq):
        return os.path.join(self._base_dir, f"{self._log_type}-{date_str}-{seq:03d}.log")

    def _find_next_seq(self, date_str):
        """Find the highest existing sequence number for *date_str*, or start at 1."""
        pattern = os.path.join(self._base_dir, f"{self._log_type}-{date_str}-*.log")
        matches = glob.glob(pattern)
        if not matches:
            return 1
        best = 0
        for m in matches:
            base = os.path.basename(m)
            # e.g. "onionpress-2026-03-31-002.log"
            parts = base.rsplit("-", 1)
            if len(parts) == 2:
                try:
                    seq = int(parts[1].replace(".log", ""))
                    if seq > best:
                        best = seq
                except ValueError:
                    pass
        return best if best > 0 else 1

    def _enforce_total_size(self):
        """Delete oldest log files across all types until under the cap.

        Skips any file modified in the last 60 seconds to protect active
        files from all RotatingLog instances sharing this directory.
        """
        import time as _time
        now = _time.time()

        try:
            all_logs = sorted(
                glob.glob(os.path.join(self._base_dir, "*.log")),
                key=lambda p: os.path.getmtime(p),
            )
        except OSError:
            return

        total = sum(os.path.getsize(p) for p in all_logs if os.path.exists(p))

        for p in all_logs:
            if total <= self._max_total_size:
                break
            try:
                # Protect any recently-written file (likely still active)
                if now - os.path.getmtime(p) < 60:
                    continue
                size = os.path.getsize(p)
                os.remove(p)
                total -= size
            except OSError:
                pass
