"""OnionPress CLI entry point.

Usage: python -m onionpress.cli <command> [args]

Commands: start, stop, restart, status, address, logs, setup, backup, restore, reset
"""

import argparse
import json
import os
import sys
import time
from typing import Callable

from . import __version__
from .platform import OS, detect_os, resolve_paths, detect_timezone
from .config import (
    ensure_config, ensure_secrets, read_value, detect_port_offset,
)
from .docker import Docker
from .containers import ContainerManager


def _make_log_func(log_file: str | None = None) -> Callable[[str], None]:
    """Create a log function that writes to stderr and optionally a file."""
    def log(msg: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, file=sys.stderr)
        if log_file:
            try:
                with open(log_file, "a") as f:
                    f.write(line + "\n")
            except OSError:
                pass
    return log


class OnionPressCLI:
    """Wires together all OnionPress modules for CLI use."""

    def __init__(self, data_dir: str = None, app_bundle: str = None):
        self.paths = resolve_paths(data_dir=data_dir, app_bundle=app_bundle)
        os.makedirs(self.paths.data_dir, exist_ok=True)
        self.log = _make_log_func(self.paths.log_file)

        # Ensure config exists
        ensure_config(self.paths)

        # Detect ports
        self.port_config = detect_port_offset()

        # Build env for Docker
        secrets = ensure_secrets(self.paths.secrets_file)
        extra_env = secrets.as_env()
        extra_env["ONIONPRESS_WP_PORT"] = str(self.port_config.wp_port)
        extra_env["ONIONPRESS_SOCKS_PORT"] = str(self.port_config.socks_port)
        extra_env["ONIONPRESS_PROXY_PORT"] = str(self.port_config.proxy_port)
        extra_env["ONIONPRESS_PORT_OFFSET"] = str(self.port_config.offset)
        extra_env["TOR_IMPL"] = read_value(
            self.paths.config_file, "TOR_IMPL", "arti"
        )
        extra_env["TZ"] = detect_timezone()
        extra_env["ONIONPRESS_VERSION"] = __version__

        self.docker = Docker(self.paths, log_func=self.log, extra_env=extra_env)
        self.containers = ContainerManager(
            self.docker, self.paths, self.port_config, log_func=self.log,
        )

    def cmd_start(self) -> int:
        """Start OnionPress containers."""
        # Check PID lock
        if self._check_pid_lock():
            print("OnionPress is already running.", file=sys.stderr)
            return 1
        self._write_pid_lock()

        try:
            # Detect container runtime (may start Colima on macOS)
            if detect_os() == OS.MACOS:
                from .colima import detect_container_runtime
                runtime = detect_container_runtime(self.paths, log_func=self.log)
                self.log(f"Container runtime: {runtime}")

            # Pull images if configured
            if read_value(self.paths.config_file, "UPDATE_ON_LAUNCH", "yes") == "yes":
                self.containers.pull_images()

            # Start core services
            if not self.containers.start_core():
                return 1

            # Wait for WordPress
            if not self.containers.wait_for_wordpress():
                self.log("WARNING: WordPress not ready, continuing anyway")

            # Start Tor if WordPress is installed
            if self.containers.wp_is_installed():
                self.containers.start_tor()

                # Wait for Tor
                if self.containers.wait_for_tor():
                    addr = self.containers.get_onion_address()
                    if addr:
                        self.log(f"Onion address: {addr}")
                        print(f"  Onion address: {addr}")
            else:
                self.log("WordPress not installed — skipping Tor startup")
                print("  WordPress not installed. Run: onionpress setup")

            wp_url = f"http://localhost:{self.port_config.wp_port}"
            self.log(f"OnionPress is running! Local: {wp_url}")
            print(f"  Local access: {wp_url}")
            return 0

        except Exception as e:
            self.log(f"ERROR: {e}")
            return 1

    def cmd_stop(self) -> int:
        """Stop OnionPress containers."""
        self.containers.stop()
        self._remove_pid_lock()
        self.log("OnionPress stopped")
        return 0

    def cmd_restart(self) -> int:
        """Restart OnionPress containers."""
        self.containers.stop()
        return self.cmd_start()

    def cmd_status(self) -> int:
        """Print container status as JSON."""
        status = self.containers.get_status()
        output = {
            "onion_address": status.onion_address,
            "wp_ready": status.wp_ready,
            "tor_bootstrapped": status.tor_bootstrapped,
            "services": status.services,
        }
        print(json.dumps(output, indent=2))
        return 0

    def cmd_address(self) -> int:
        """Print the onion address."""
        addr = self.containers.get_onion_address()
        if addr:
            print(addr)
            return 0
        print("No onion address available", file=sys.stderr)
        return 1

    def cmd_logs(self) -> int:
        """Follow container logs."""
        result = self.docker.compose(
            ["logs", "-f"],
            compose_files=[os.path.join(self.paths.docker_dir, "docker-compose.yml")] if self.paths.docker_dir else None,
            timeout=0,  # Will be killed by user
        )
        return result.returncode

    def cmd_backup(self, password: str, output_path: str = None) -> int:
        """Create a backup."""
        if not output_path:
            output_path = os.path.expanduser(
                f"~/Downloads/onionpress-{time.strftime('%Y-%m-%d')}-{os.getpid()}.zip"
            )
        addr = self.containers.get_onion_address()
        # Delegate to backup_manager (existing module)
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
            import backup_manager
            backup_manager.create_backup(
                onion_address=addr,
                username="",  # not needed for CLI backup
                password=password,
                output_path=output_path,
                version=__version__,
                log_func=self.log,
            )
            print(f"Backup saved to: {output_path}")
            return 0
        except Exception as e:
            self.log(f"Backup failed: {e}")
            return 1

    def cmd_restore(self, password: str, backup_path: str) -> int:
        """Restore from backup."""
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
            import backup_manager
            backup_manager.restore_backup(
                backup_path=backup_path,
                password=password,
                log_func=self.log,
            )
            # Flag key reimport
            import_flag = os.path.join(self.paths.data_dir, ".import-key-pending")
            with open(import_flag, "w") as f:
                f.write("")
            # Restart
            self.containers.stop()
            self.containers.start_core()
            self.containers.wait_for_wordpress()
            self.containers.start_tor()
            self.containers.wait_for_tor()
            print("Restore complete. OnionPress restarted with restored data.")
            return 0
        except Exception as e:
            self.log(f"Restore failed: {e}")
            return 1

    def cmd_reset(self, yes: bool = False) -> int:
        """Reset OnionPress — wipe all data and start fresh."""
        if not yes:
            print()
            print("  This will ERASE everything: WordPress, database, Tor state,")
            print("  onion address keys, config, and secrets.")
            print()
            print("  To preserve your data, run 'onionpress backup' first.")
            print()
            try:
                input("  Press Enter to continue or Ctrl+C to cancel...")
            except (KeyboardInterrupt, EOFError):
                print("\n  Cancelled.")
                return 1

        self.log("Resetting OnionPress (full wipe)...")

        # Stop everything
        try:
            self.containers.stop()
        except Exception:
            pass

        # Remove Docker volumes
        result = self.docker.run(
            ["volume", "ls", "-q", "--filter", "name=onionpress-"],
            timeout=15,
        )
        if result.ok:
            for vol in result.output.splitlines():
                vol = vol.strip()
                if vol:
                    self.docker.run(["volume", "rm", vol], timeout=15)
                    self.log(f"Removed volume: {vol}")

        # Wipe data files (keep colima VM)
        for name in [
            "secrets", "onionpress.log", ".last_status_state",
            "config", "config.bak", "onionheaven-registration.json",
            "onion_address", "healthcheck-address",
        ]:
            path = os.path.join(self.paths.data_dir, name)
            if os.path.exists(path):
                os.remove(path)

        import shutil
        vanity_dir = os.path.join(self.paths.shared_dir, "vanity-keys")
        if os.path.exists(vanity_dir):
            shutil.rmtree(vanity_dir)

        self.log("Removed keys, config, secrets, and logs")
        print("\n  Reset complete. Run 'onionpress start' to start fresh.")
        self._remove_pid_lock()
        return 0

    # -- PID lock --

    def _check_pid_lock(self) -> bool:
        """Check if another instance is running."""
        if not os.path.exists(self.paths.pid_file):
            return False
        try:
            with open(self.paths.pid_file) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # Check if process exists
            return True
        except (ValueError, OSError):
            # Stale PID file
            os.remove(self.paths.pid_file)
            return False

    def _write_pid_lock(self) -> None:
        with open(self.paths.pid_file, "w") as f:
            f.write(str(os.getpid()))

    def _remove_pid_lock(self) -> None:
        try:
            os.remove(self.paths.pid_file)
        except OSError:
            pass


def main(argv: list[str] = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="onionpress",
        description="OnionPress — WordPress over Tor",
    )
    parser.add_argument("--version", action="version", version=f"OnionPress {__version__}")
    parser.add_argument("--data-dir", help="Override data directory (default: ~/.onionpress/)")

    sub = parser.add_subparsers(dest="command", help="Command to run")

    sub.add_parser("start", help="Start OnionPress")
    sub.add_parser("stop", help="Stop OnionPress")
    sub.add_parser("restart", help="Restart OnionPress")
    sub.add_parser("status", help="Show container status (JSON)")
    sub.add_parser("address", help="Print onion address")
    sub.add_parser("logs", help="Follow container logs")

    p_backup = sub.add_parser("backup", help="Create a backup")
    p_backup.add_argument("password", help="Backup encryption password")
    p_backup.add_argument("output", nargs="?", help="Output path (default: ~/Downloads/)")

    p_restore = sub.add_parser("restore", help="Restore from backup")
    p_restore.add_argument("password", help="Backup encryption password")
    p_restore.add_argument("backup_file", help="Path to backup .zip file")

    p_reset = sub.add_parser("reset", help="Wipe all data and start fresh")
    p_reset.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")

    args = parser.parse_args(argv)

    if not args.command:
        args.command = "start"

    cli = OnionPressCLI(data_dir=args.data_dir)

    commands = {
        "start": cli.cmd_start,
        "stop": cli.cmd_stop,
        "restart": cli.cmd_restart,
        "status": cli.cmd_status,
        "address": cli.cmd_address,
        "logs": cli.cmd_logs,
    }

    if args.command in commands:
        return commands[args.command]()
    elif args.command == "backup":
        return cli.cmd_backup(args.password, args.output)
    elif args.command == "restore":
        return cli.cmd_restore(args.password, args.backup_file)
    elif args.command == "reset":
        return cli.cmd_reset(yes=args.yes)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
