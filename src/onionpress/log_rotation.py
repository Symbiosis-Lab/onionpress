"""Rotating log files for OnionPress.

Handles daily rotation with size limits, home-directory scrubbing,
total disk usage caps for the logs directory, and gzip compression
of rolled (closed) files so older history takes ~10–20x less disk
and bandwidth during analytics upload.
"""

import glob
import gzip
import os
import shutil
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

        prev_path = None  # if we rolled, gzip-compress the just-closed file

        with self._lock:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            rolled = False

            if today != self._current_date:
                prev_path = self._path
                self._current_date = today
                self._seq = self._find_next_seq(today)
                self._path = self._make_path(today, self._seq)
                rolled = True
            elif os.path.exists(self._path):
                try:
                    if os.path.getsize(self._path) >= self._max_size:
                        prev_path = self._path
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
            # Compress the file we just rolled off of, in a background
            # thread so we don't block the write path. Enforcement runs
            # after so size accounting includes the compressed artifact.
            if prev_path and os.path.exists(prev_path) and not prev_path.endswith(".gz"):
                threading.Thread(
                    target=self._gzip_file, args=(prev_path,), daemon=True,
                ).start()
            self._enforce_total_size()

    def current_path(self):
        """Return the path of the currently active log file."""
        return self._path

    def completed_files(self):
        """Return metadata for all files that are NOT the current active file.

        Each entry is ``{"name": str, "size": int, "path": str}``.
        Includes both plain ``.log`` files (still-compressing or pre-gzip
        era) and ``.log.gz`` files (rolled + compressed).
        """
        current = os.path.basename(self._path)
        result = []
        patterns = [
            os.path.join(self._base_dir, f"{self._log_type}-*.log"),
            os.path.join(self._base_dir, f"{self._log_type}-*.log.gz"),
        ]
        seen = set()
        for pattern in patterns:
            for p in sorted(glob.glob(pattern)):
                name = os.path.basename(p)
                if name == current or name in seen:
                    continue
                seen.add(name)
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
        """Find the highest existing sequence number for *date_str*, or start at 1.

        Considers both ``.log`` (still-active or not-yet-compressed) and
        ``.log.gz`` (rolled + compressed) files so we don't reuse a seq
        that's been compressed.
        """
        patterns = [
            os.path.join(self._base_dir, f"{self._log_type}-{date_str}-*.log"),
            os.path.join(self._base_dir, f"{self._log_type}-{date_str}-*.log.gz"),
        ]
        matches = []
        for pattern in patterns:
            matches.extend(glob.glob(pattern))
        if not matches:
            return 1
        best = 0
        for m in matches:
            base = os.path.basename(m)
            # "<type>-YYYY-MM-DD-NNN.log" or "<type>-YYYY-MM-DD-NNN.log.gz"
            # Strip .gz then .log, then take the last -<NNN> segment.
            stem = base
            if stem.endswith(".gz"):
                stem = stem[:-3]
            if stem.endswith(".log"):
                stem = stem[:-4]
            parts = stem.rsplit("-", 1)
            if len(parts) == 2:
                try:
                    seq = int(parts[1])
                    if seq > best:
                        best = seq
                except ValueError:
                    pass
        return best if best > 0 else 1

    def _gzip_file(self, path):
        """Gzip *path* in place (writes ``path + .gz``, removes original).

        Runs in a background thread off the write path. Safe to call
        after a rotation because the original file is no longer being
        appended to.
        """
        gz_path = path + ".gz"
        try:
            with open(path, "rb") as src, gzip.open(gz_path, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst, length=1 << 16)
            os.chmod(gz_path, 0o600)
            os.remove(path)
        except OSError:
            # If compression or delete failed, leave the .log in place
            # and clean up any partial .gz — next rotation retries.
            try:
                os.remove(gz_path)
            except OSError:
                pass

    def _enforce_total_size(self):
        """Delete oldest log files across all types until under the cap.

        Skips any file modified in the last 60 seconds to protect active
        files from all RotatingLog instances sharing this directory.
        Considers both ``.log`` and ``.log.gz`` files.
        """
        import time as _time
        now = _time.time()

        try:
            all_logs = sorted(
                glob.glob(os.path.join(self._base_dir, "*.log"))
                + glob.glob(os.path.join(self._base_dir, "*.log.gz")),
                key=lambda p: os.path.getmtime(p),
            )
        except OSError:
            return

        # Background gzip thread may remove a .log file between the glob
        # above and sizing here — tolerate OSError per file.
        total = 0
        for p in all_logs:
            try:
                total += os.path.getsize(p)
            except OSError:
                pass

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
