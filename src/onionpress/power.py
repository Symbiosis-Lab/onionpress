"""Mac sleep prevention via the `caffeinate` utility.

Wraps a single caffeinate subprocess whose lifetime follows the OnionPress
service. Config mode (PREVENT_SLEEP) selects the flavor:

    normal      — no caffeinate; Mac sleeps normally
    on-battery  — `caffeinate -s`; stays awake only on AC power
    never       — `caffeinate -i`; never sleeps while OnionPress runs

Also reaps orphaned caffeinate processes left behind by a previous crash
or force-quit (PID file in app_support).
"""

import os
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
