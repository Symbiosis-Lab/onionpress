"""Cross-platform launcher operations shared between Mac and Linux.

Primitives used by the Linux bash launcher (`linux/onionpress` via
`python3 -m onionpress.cli`), the Linux tray app (`linux/onionpress-tray`,
direct import), and — eventually — the Mac menubar app.

Each function takes explicit paths and doesn't read environment or globals.
Platform-aware glue (Tor Browser detection, which container runtime) lives
in callers.

Operations:
- open_in_browser(url, prefer_tor)         — launch via xdg-open / open / torbrowser-launcher
- tor_image_has_mkp224o(image)             — verify the tor image bundles mkp224o
- generate_vanity_in_container(...)        — docker run mkp224o, write to vanity-keys/
- get_admin_password(data_dir)             — read ~/.onionpress/wp-admin-password
"""

import os
import shutil
import subprocess
import sys
from typing import Optional


DEFAULT_TOR_IMAGE = "ghcr.io/brewsterkahle/onionpress-tor:latest"


def open_in_browser(url: str, *, prefer_tor: bool = False) -> None:
    """Spawn a browser at `url`. Detached — does not wait."""
    if prefer_tor and shutil.which("torbrowser-launcher"):
        cmd = ["torbrowser-launcher", url]
    elif sys.platform == "darwin":
        cmd = ["open", url]
    else:
        cmd = ["xdg-open", url]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL,
                         start_new_session=True)
    except (OSError, FileNotFoundError) as e:
        raise RuntimeError(f"failed to launch browser ({cmd[0]}): {e}") from e


def tor_image_has_mkp224o(image: str = DEFAULT_TOR_IMAGE) -> bool:
    """Return True iff the tor container image is pulled AND contains mkp224o.

    Verifies via a `docker run --rm --entrypoint test` probe so we don't
    fall through to attempting vanity generation on an older image that
    predates the mkp224o build stage.
    """
    inspect = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True, timeout=15,
    )
    if inspect.returncode != 0:
        return False
    probe = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "test", image,
         "-x", "/usr/local/bin/mkp224o"],
        capture_output=True, timeout=30,
    )
    return probe.returncode == 0


def generate_vanity_in_container(
    prefix: str,
    vanity_dir: str,
    *,
    image: str = DEFAULT_TOR_IMAGE,
    jobs: Optional[int] = None,
    log_func=None,
) -> Optional[str]:
    """Run mkp224o inside the tor container; return the generated .onion address.

    Writes the key bundle into `vanity_dir/<addr>.onion/`. Returns the
    address (basename of the dir, including `.onion`) on success, None on
    failure. Caller is responsible for ensuring the dir is empty first if
    a fresh key is required.
    """
    if not (2 <= len(prefix) <= 6):
        raise ValueError(f"prefix must be 2-6 chars (got {prefix!r})")

    os.makedirs(vanity_dir, exist_ok=True)
    if jobs is None:
        jobs = os.cpu_count() or 2

    if log_func:
        log_func(f"[STAGE] vanity Starting mkp224o for prefix '{prefix}' "
                 f"with {jobs} threads...")

    # Run as the host user so the resulting files are owned correctly.
    cmd = [
        "docker", "run", "--rm",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{vanity_dir}:/out",
        image,
        "/usr/local/bin/mkp224o",
        "-n", "1", "-j", str(jobs), "-d", "/out", prefix,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if log_func and proc.stdout:
        for line in proc.stdout.splitlines():
            log_func(f"mkp224o: {line}")
    if proc.returncode != 0:
        if log_func:
            log_func(f"mkp224o failed (exit {proc.returncode}): {proc.stderr.strip()}")
        return None

    # mkp224o writes <addr>.onion/ directory under /out
    candidates = [
        d for d in os.listdir(vanity_dir)
        if d.startswith(prefix) and d.endswith(".onion")
        and os.path.isdir(os.path.join(vanity_dir, d))
        and os.path.exists(os.path.join(vanity_dir, d, "hs_ed25519_secret_key"))
    ]
    if not candidates:
        if log_func:
            log_func(f"mkp224o exit 0 but no {prefix}*.onion directory in {vanity_dir}")
        return None
    addr = candidates[0]
    if log_func:
        log_func(f"[STAGE] vanity Generated address: {addr}")
    return addr


def get_admin_password(data_dir: str) -> Optional[str]:
    """Read the auto-generated WP admin password (`~/.onionpress/wp-admin-password`).

    None if the file doesn't exist (user has done their own setup, or
    headless install hasn't run yet).
    """
    path = os.path.join(data_dir, "wp-admin-password")
    try:
        with open(path) as f:
            value = f.read().strip()
            return value or None
    except OSError:
        return None
