"""OS/arch detection and path resolution for OnionPress.

No side effects — no mkdir, no env var mutation.
"""

import enum
import os
import subprocess
from dataclasses import dataclass


class OS(enum.Enum):
    MACOS = "macos"
    LINUX = "linux"


class Arch(enum.Enum):
    ARM64 = "aarch64"
    X86_64 = "x86_64"


def detect_os() -> OS:
    """Detect the current operating system."""
    import platform as _platform
    system = _platform.system()
    if system == "Darwin":
        return OS.MACOS
    if system == "Linux":
        return OS.LINUX
    raise RuntimeError(f"Unsupported OS: {system}")


def detect_arch() -> Arch:
    """Detect the hardware architecture.

    On macOS, uses sysctl to get the real hardware arch (uname lies under
    Rosetta). On Linux, uses uname -m.
    """
    import platform as _platform
    system = _platform.system()
    if system == "Darwin":
        try:
            result = subprocess.run(
                ["sysctl", "-n", "hw.optional.arm64"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip() == "1":
                return Arch.ARM64
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        # Fallback to uname
        if _platform.machine() == "x86_64":
            return Arch.X86_64
        return Arch.ARM64
    else:
        machine = _platform.machine()
        if machine in ("aarch64", "arm64"):
            return Arch.ARM64
        return Arch.X86_64


def detect_timezone() -> str:
    """Detect the system timezone.

    On macOS: reads /etc/localtime symlink.
    On Linux: reads /etc/timezone, falls back to /etc/localtime symlink.
    Returns 'UTC' if detection fails.
    """
    # Try /etc/localtime symlink (macOS and some Linux)
    try:
        target = os.readlink("/etc/localtime")
        # Extract timezone from path like .../zoneinfo/America/New_York
        idx = target.find("zoneinfo/")
        if idx != -1:
            return target[idx + len("zoneinfo/"):]
    except OSError:
        pass

    # Try /etc/timezone (Debian/Ubuntu)
    try:
        with open("/etc/timezone", "r") as f:
            tz = f.read().strip()
            if tz:
                return tz
    except OSError:
        pass

    return "UTC"


def default_documents_dir() -> str:
    """Return the user-visible documents directory for backups and Creations.

    Both platforms: ~/OnionPress/ (top of home, Finder-visible).

    Previously was ~/Documents/OnionPress/, which on macOS is TCC-protected:
    the Apple Virtualization framework requires a Files-and-Folders grant
    to mount it into the Colima VM, and that grant is tied to the bundle
    identity. Every rebuild-menubar.sh modified the bundle and silently
    revoked the grant, causing the next launch to fail with VZErrorDomain
    Code=2 "directory sharing device configuration is invalid". Moving to
    a non-TCC location removes this footgun permanently. See issue #239.

    The one-time host-side migration from the old path runs in the
    launchers (app/MacOS/onionpress, linux/onionpress) before docker
    compose up, gated by the PATH_MIGRATION_2026_05 marker in the config
    file. The marker also protects against re-creating the old dir on
    machines that have already been migrated.
    """
    return os.path.join(os.path.expanduser("~"), "OnionPress")


@dataclass(frozen=True)
class OnionPressPaths:
    """All OnionPress filesystem paths, computed once."""
    data_dir: str
    documents_dir: str
    config_file: str
    secrets_file: str
    log_file: str
    launcher_log_file: str
    pid_file: str
    shared_dir: str
    docker_config_dir: str
    bin_dir: str
    docker_dir: str
    colima_home: str
    docker_socket: str
    app_bundle: str  # root of OnionPress.app (or repo root if unbundled)


def resolve_paths(data_dir: str = None, documents_dir: str = None,
                   app_bundle: str = None) -> OnionPressPaths:
    """Compute all OnionPress paths from data_dir and app_bundle root.

    Args:
        data_dir: Override for ~/.onionpress/. Defaults to ~/.onionpress/.
        documents_dir: Override for ~/OnionPress/. Defaults to platform default.
        app_bundle: Path to OnionPress.app. If None, attempts find_app_bundle().
    """
    if data_dir is None:
        data_dir = os.path.join(os.path.expanduser("~"), ".onionpress")
    if documents_dir is None:
        documents_dir = default_documents_dir()
    if app_bundle is None:
        app_bundle = find_app_bundle() or ""

    colima_home = os.path.join(data_dir, "colima")

    # Resolve resource paths from app bundle
    if app_bundle:
        contents = os.path.join(app_bundle, "Contents")
        resources = os.path.join(contents, "Resources")
        bin_dir = os.path.join(resources, "bin")
        docker_dir = os.path.join(resources, "docker")
    else:
        bin_dir = ""
        docker_dir = ""

    # On Linux, prefer the rootless Docker socket (what install.sh sets up);
    # fall back to the rootful system socket only if rootless is absent.
    # On macOS, always use Colima's socket under the data dir.
    if detect_os() == OS.LINUX:
        rootless_sock = f"/run/user/{os.getuid()}/docker.sock"
        docker_socket = rootless_sock if os.path.exists(rootless_sock) else "/var/run/docker.sock"
    else:
        docker_socket = os.path.join(colima_home, "default", "docker.sock")

    return OnionPressPaths(
        data_dir=data_dir,
        documents_dir=documents_dir,
        config_file=os.path.join(data_dir, "config"),
        secrets_file=os.path.join(data_dir, "secrets"),
        log_file=os.path.join(data_dir, "onionpress.log"),
        launcher_log_file=os.path.join(data_dir, "launcher.log"),
        pid_file=os.path.join(data_dir, "onionpress.pid"),
        shared_dir=os.path.join(data_dir, "shared"),
        docker_config_dir=os.path.join(data_dir, "docker-config"),
        bin_dir=bin_dir,
        docker_dir=docker_dir,
        colima_home=colima_home,
        docker_socket=docker_socket,
        app_bundle=app_bundle,
    )


def is_moss_managed(app_bundle: str) -> bool:
    """True when this copy of OnionPress is the one moss stages and manages.

    moss installs the stack under ``~/.moss/stacks/onionpress/OnionPress.app``
    and drives it headlessly (provisioning via launcher subcommands, launch
    via ``open -a``). That staged copy must start with zero user-visible UI
    beyond the menu bar icon — no launch splash, no auto-opened browser —
    because moss owns the install/start experience and is deliberately quiet.

    Detection is by the bundle's own location: a path containing a
    ``.moss/stacks`` segment pair is moss's staged copy. This is copy-level,
    not launch-level, and that is the point — the same user can also install
    OnionPress standalone in /Applications and keep its full first-run UX,
    while the moss-managed copy stays quiet no matter who launches it.
    """
    if not app_bundle:
        return False
    parts = os.path.normpath(os.path.abspath(app_bundle)).split(os.sep)
    return any(
        parts[i] == ".moss" and parts[i + 1] == "stacks"
        for i in range(len(parts) - 1)
    )


_QUIET_TRUTHY = {"1", "true", "yes", "on"}
_QUIET_FALSY = {"0", "false", "no", "off"}


def is_quiet_launch(app_bundle: str, environ=None) -> bool:
    """Should this launch suppress all startup UI (splash, auto-browser)?

    ``ONIONPRESS_QUIET`` in the environment is an explicit override in both
    directions (for tests, and for any launcher that execs the binary
    directly rather than through ``open``). Absent an override, the answer
    is :func:`is_moss_managed` — the moss-staged copy is quiet by default,
    a standalone install keeps its existing startup UX.
    """
    env = os.environ if environ is None else environ
    override = (env.get("ONIONPRESS_QUIET") or "").strip().lower()
    if override in _QUIET_TRUTHY:
        return True
    if override in _QUIET_FALSY:
        return False
    return is_moss_managed(app_bundle)


def _is_app_bundle(path: str) -> bool:
    """Check if a directory looks like a macOS .app bundle."""
    return (
        path.endswith(".app")
        and os.path.isdir(path)
        and os.path.isdir(os.path.join(path, "Contents", "MacOS"))
    )


def find_app_bundle() -> str | None:
    """Try to locate the OnionPress app bundle.

    Walks up from this file's location looking for a .app bundle directory
    (by structure, not by name — the user may have renamed it in Finder).
    Falls back to /Applications/OnionPress.app.
    """
    # Walk up from this module
    current = os.path.dirname(os.path.abspath(__file__))
    for _ in range(10):
        if _is_app_bundle(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    # Check standard install location
    standard = "/Applications/OnionPress.app"
    if os.path.isdir(standard):
        return standard

    return None
