"""Cross-platform sleep prevention.

Mac:   CaffeineManager wraps `caffeinate -s | -i`.
Linux: SystemdInhibitor wraps `systemd-inhibit --what=idle:sleep`.

Both classes share the same interface (`start()` / `stop()` /
`is_running()`) so the menubar / service can swap them freely. Both
read the `PREVENT_SLEEP` key from `~/.onionpress/config`:

    normal      — no inhibit; system sleeps normally
    on-battery  — Mac: caffeinate -s (AC only); Linux: idle inhibit
                  (best-effort; Linux can't easily condition on AC)
    never       — Mac: caffeinate -i; Linux: idle+sleep inhibit

Both reap orphaned inhibitor processes from prior crashes via the
shared PID file in app_support / data_dir.
"""

import os
import shutil
import subprocess
from typing import Callable, Optional


class CaffeineManager:
    def __init__(
        self,
        app_support: str,
        log_func: Callable[[str], None],
        read_config: Callable[[str, str], str],
    ):
        self.app_support = app_support
        self._log = log_func
        self._read_config = read_config
        self._process: Optional[subprocess.Popen] = None

    @property
    def pid_file(self) -> str:
        return os.path.join(self.app_support, "caffeinate.pid")

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _cleanup_stale(self) -> None:
        if not os.path.exists(self.pid_file):
            return
        try:
            with open(self.pid_file) as f:
                old_pid = int(f.read().strip())
            # Confirm it's actually caffeinate before killing — PIDs recycle.
            result = subprocess.run(
                ["ps", "-p", str(old_pid), "-o", "comm="],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=5,
            )
            if result.returncode == 0 and "caffeinate" in result.stdout:
                os.kill(old_pid, 15)  # SIGTERM
                self._log(f"Cleaned up orphaned caffeinate (PID {old_pid}) from previous run")
            os.remove(self.pid_file)
        except (ValueError, OSError, subprocess.TimeoutExpired):
            try:
                os.remove(self.pid_file)
            except OSError:
                pass

    def start(self) -> None:
        """Start caffeinate per configured mode. No-op if already running or mode=normal."""
        if self.is_running():
            return

        self._cleanup_stale()

        mode = self._read_config("PREVENT_SLEEP", "normal").lower()
        # Backward compat for older configs.
        if mode == "yes":
            mode = "on-battery"
        elif mode == "no":
            mode = "normal"

        if mode == "on-battery":
            caff_args = ["caffeinate", "-s"]
            caff_msg = "staying awake on AC power"
        elif mode == "never":
            caff_args = ["caffeinate", "-i"]
            caff_msg = "never sleeping while OnionPress runs"
        else:
            return  # "normal" or unknown — Mac sleeps normally

        try:
            self._process = subprocess.Popen(
                caff_args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                with open(self.pid_file, "w") as f:
                    f.write(str(self._process.pid))
            except OSError:
                pass
            self._log(f"Started caffeinate (PID {self._process.pid}) - {caff_msg}")
        except Exception as e:
            self._log(f"Failed to start caffeinate: {e}")

    def stop(self) -> None:
        """Terminate caffeinate and remove the PID file."""
        if self._process is None:
            return
        try:
            self._process.terminate()
            self._process.wait(timeout=2)
            self._log("Stopped caffeinate - Mac can sleep normally")
        except Exception:
            try:
                self._process.kill()
                self._log("Force killed caffeinate process")
            except Exception:
                pass
        finally:
            self._process = None
            try:
                os.remove(self.pid_file)
            except OSError:
                pass


class SystemdInhibitor:
    """Linux equivalent of CaffeineManager — wraps systemd-inhibit.

    `systemd-inhibit --what=<locks> sleep infinity` holds an inhibitor
    lock as long as its child sleep process is alive. We Popen() that
    command and store the PID; on stop() we terminate it and the lock
    releases.

    `what` modes:
      idle       — prevents idle suspend (screensaver / auto-suspend)
      sleep      — prevents systemd-initiated suspend / hibernate
      idle:sleep — both

    Manual sleep (lid close, `systemctl suspend`) is governed by
    different locks (`handle-lid-switch`, etc.) which we deliberately
    do NOT block — the user must always be able to put the machine
    to sleep deliberately.
    """

    def __init__(
        self,
        data_dir: str,
        log_func: Callable[[str], None],
        read_config: Callable[[str, str], str],
    ):
        self.data_dir = data_dir
        self._log = log_func
        self._read_config = read_config
        self._process: Optional[subprocess.Popen] = None

    @property
    def pid_file(self) -> str:
        return os.path.join(self.data_dir, "systemd-inhibit.pid")

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _cleanup_stale(self) -> None:
        if not os.path.exists(self.pid_file):
            return
        try:
            with open(self.pid_file) as f:
                old_pid = int(f.read().strip())
            # Confirm it's actually systemd-inhibit before killing — PIDs recycle.
            try:
                with open(f"/proc/{old_pid}/comm") as f:
                    comm = f.read().strip()
            except OSError:
                comm = ""
            if "systemd-inhibit" in comm or comm == "systemd-inhibi":
                os.kill(old_pid, 15)
                self._log(f"Cleaned up orphaned systemd-inhibit (PID {old_pid})")
            os.remove(self.pid_file)
        except (ValueError, OSError):
            try:
                os.remove(self.pid_file)
            except OSError:
                pass

    def start(self) -> None:
        """Start systemd-inhibit per configured mode. No-op if already running."""
        if self.is_running():
            return

        if not shutil.which("systemd-inhibit"):
            return  # No systemd on this host — silently skip.

        self._cleanup_stale()

        mode = self._read_config("PREVENT_SLEEP", "normal").lower()
        if mode == "yes":
            mode = "on-battery"
        elif mode == "no":
            mode = "normal"

        if mode == "on-battery":
            what = "idle"
            msg = "blocking idle suspend while OnionPress runs"
        elif mode == "never":
            what = "idle:sleep"
            msg = "blocking idle and auto-suspend while OnionPress runs"
        else:
            return  # "normal" — Linux sleeps normally

        try:
            self._process = subprocess.Popen(
                ["systemd-inhibit",
                 f"--what={what}",
                 "--who=OnionPress",
                 "--why=Serving onion site",
                 "--mode=block",
                 "sleep", "infinity"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            try:
                with open(self.pid_file, "w") as f:
                    f.write(str(self._process.pid))
            except OSError:
                pass
            self._log(f"Started systemd-inhibit (PID {self._process.pid}) - {msg}")
        except Exception as e:
            self._log(f"Failed to start systemd-inhibit: {e}")

    def stop(self) -> None:
        """Terminate systemd-inhibit and remove the PID file."""
        if self._process is None:
            return
        try:
            self._process.terminate()
            self._process.wait(timeout=2)
            self._log("Stopped systemd-inhibit - system can sleep normally")
        except Exception:
            try:
                self._process.kill()
                self._log("Force killed systemd-inhibit process")
            except Exception:
                pass
        finally:
            self._process = None
            try:
                os.remove(self.pid_file)
            except OSError:
                pass
