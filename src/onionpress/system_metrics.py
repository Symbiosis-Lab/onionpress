"""Host + container resource snapshots for OnionPress.

Pure-function helpers used by the reachability snapshot path. All
collection paths are wrapped in try/except — a missing metric returns
None rather than raising, so the snapshot is best-effort.

macOS:  sysctl -n hw.memsize for total, vm_stat for used (matches
        Activity Monitor's "Memory Used" definition).
Linux:  /proc/meminfo MemTotal + MemAvailable.
Both:   os.getloadavg(), os.cpu_count(), `docker stats --no-stream`.

Used by:
- src/menubar.py — handle_sleep, handle_wake, 12h timer in check_status loop
- src/onionpress/onionheaven.py — 12h timer in _heartbeat_loop (Linux path)
"""

import os
import re
import subprocess
from typing import Optional

from .platform import detect_os, OS


def host_metrics() -> dict:
    """Return host CPU/RAM snapshot.

    Keys (any may be None on collection failure):
      ram_used_gb, ram_total_gb, load_1m, cpu_count
    """
    result = {
        "ram_used_gb": None,
        "ram_total_gb": None,
        "load_1m": None,
        "cpu_count": None,
    }
    try:
        result["load_1m"] = float(os.getloadavg()[0])
    except Exception:
        pass
    try:
        result["cpu_count"] = os.cpu_count()
    except Exception:
        pass
    try:
        if detect_os() == OS.MACOS:
            total = _macos_total_ram_bytes()
            used = _macos_used_ram_bytes()
            if total is not None:
                result["ram_total_gb"] = total / 1024 / 1024 / 1024
            if used is not None:
                result["ram_used_gb"] = used / 1024 / 1024 / 1024
        else:
            total, available = _linux_meminfo()
            if total is not None:
                result["ram_total_gb"] = total / 1024 / 1024 / 1024
                if available is not None:
                    result["ram_used_gb"] = (total - available) / 1024 / 1024 / 1024
    except Exception:
        pass
    return result


def container_metrics(docker) -> dict:
    """Return the OnionPress CPU/RAM rollup.

    Keys (any may be None):
      total_ram_mb       -- in-container workload (sum of MemUsage from
                            `docker stats`). On Mac this is what runs
                            inside the Colima VM, NOT including VM overhead.
      total_cpu_percent  -- sum of container CPU% from `docker stats`.
      onionpress_ram_mb  -- OnionPress's full footprint:
                              * Mac: RSS of OnionPress.app + Colima + Lima
                                + qemu host-side processes (the VM holds
                                the containers, so this *includes* them).
                              * Linux: same as total_ram_mb (no VM).
                            This is "the RAM used by OnionPress" from the
                            host operating system's point of view.

    Args:
        docker: A Docker instance from src/onionpress/docker.py.
    """
    out = {
        "total_ram_mb": None,
        "total_cpu_percent": None,
        "onionpress_ram_mb": None,
    }
    try:
        result = docker.run(
            ["stats", "--no-stream",
             "--format", "{{.MemUsage}}\t{{.CPUPerc}}"],
            timeout=15,
            quiet=True,
        )
        if result.ok:
            total_mb = 0.0
            total_cpu = 0.0
            any_row = False
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line or "\t" not in line:
                    continue
                mem_str, cpu_str = line.split("\t", 1)
                mb = _parse_mem_to_mb(mem_str.split("/", 1)[0].strip())
                cpu = _parse_percent(cpu_str.strip())
                if mb is not None:
                    total_mb += mb
                    any_row = True
                if cpu is not None:
                    total_cpu += cpu
            if any_row:
                out["total_ram_mb"] = total_mb
                out["total_cpu_percent"] = total_cpu
    except Exception:
        pass

    try:
        if detect_os() == OS.MACOS:
            colima_home = getattr(getattr(docker, "paths", None), "colima_home", None)
            rss_kb = _macos_onionpress_rss_kb(colima_home)
            if rss_kb is not None:
                out["onionpress_ram_mb"] = rss_kb / 1024
        else:
            # On Linux there's no VM layer — containers are the footprint.
            if out["total_ram_mb"] is not None:
                out["onionpress_ram_mb"] = out["total_ram_mb"]
    except Exception:
        pass
    return out


# --- helpers (module-private) ---

def _macos_total_ram_bytes() -> Optional[int]:
    try:
        r = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
        )
        if r.returncode == 0:
            return int(r.stdout.strip())
    except Exception:
        pass
    return None


def _macos_used_ram_bytes() -> Optional[int]:
    """Sum (active + wired + compressed) pages * page size.

    Matches Activity Monitor's "Memory Used" — excludes inactive (cache)
    pages that the OS treats as available.
    """
    try:
        r = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            return None
        pagesize = 16384
        used_pages = 0
        for line in r.stdout.splitlines():
            m = re.search(r"page size of (\d+) bytes", line)
            if m:
                pagesize = int(m.group(1))
                continue
            for tag in ("Pages active:", "Pages wired down:",
                        "Pages occupied by compressor:"):
                if line.startswith(tag):
                    val = line.split(":", 1)[1].strip().rstrip(".")
                    used_pages += int(val)
                    break
        return used_pages * pagesize if used_pages else None
    except Exception:
        return None


def _macos_onionpress_rss_kb(colima_home: Optional[str] = None) -> Optional[int]:
    """Sum RSS of all host-side OnionPress processes on macOS (KB).

    Two sources, de-duplicated by PID:
      1. ps by command-line pattern — catches anything launched from
         /Applications/OnionPress.app/ (launcher, menubar, bundled
         colima/limactl/docker binaries).
      2. lsof on the lima dir — catches the Apple Virtualization.framework
         VM XPC service. The VM is reparented to launchd so it's invisible
         to source #1, but it has the disk image + sockets open in the
         colima dir.

    The VM is where the containers actually run, so the returned RSS
    already accounts for in-container memory — no need to add docker
    stats on top.
    """
    pids = set()

    # Source 1: ps by command pattern.
    try:
        r = subprocess.run(
            ["ps", "-A", "-o", "pid=,command="],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 1)
                if len(parts) < 2:
                    continue
                pid_str, cmd = parts
                if "/Applications/OnionPress.app/" in cmd:
                    try:
                        pids.add(int(pid_str))
                    except ValueError:
                        pass
    except Exception:
        pass

    # Source 2: lsof on the lima dir — finds the VM XPC service that
    # has the disk image open. Scoped to *our* lima dir so other Lima
    # or Docker Desktop VMs on the same machine don't pollute the
    # number.
    if colima_home:
        lima_dir = os.path.join(colima_home, "_lima", "colima")
        if os.path.isdir(lima_dir):
            try:
                r = subprocess.run(
                    ["lsof", "-t", "+D", lima_dir],
                    capture_output=True, text=True, timeout=20,
                    encoding="utf-8", errors="replace",
                )
                # lsof commonly returns rc=1 when some files in the tree
                # aren't readable, even when it printed useful PIDs to
                # stdout. Parse whatever we got rather than rejecting.
                for p in r.stdout.split():
                    try:
                        pids.add(int(p))
                    except ValueError:
                        pass
            except Exception:
                pass

    if not pids:
        return None

    # Sum RSS via a single ps call.
    try:
        r = subprocess.run(
            ["ps", "-o", "rss=", "-p", ",".join(str(p) for p in pids)],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            return None
        total_kb = 0
        for line in r.stdout.splitlines():
            line = line.strip()
            if line:
                try:
                    total_kb += int(line)
                except ValueError:
                    pass
        return total_kb if total_kb else None
    except Exception:
        return None


def _linux_meminfo() -> tuple:
    """Return (total_bytes, available_bytes) from /proc/meminfo."""
    try:
        total = None
        available = None
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) * 1024
                if total is not None and available is not None:
                    break
        return total, available
    except Exception:
        return None, None


def _parse_mem_to_mb(s: str):
    """Parse docker stats memory strings like '123MiB' / '1.5GiB' to MB."""
    if not s:
        return None
    try:
        m = re.match(r"([\d.]+)\s*([KMGT])iB", s)
        if not m:
            return None
        num = float(m.group(1))
        unit = m.group(2)
        if unit == "K":
            return num / 1024
        if unit == "M":
            return num
        if unit == "G":
            return num * 1024
        if unit == "T":
            return num * 1024 * 1024
        return None
    except Exception:
        return None


def _parse_percent(s: str):
    try:
        return float(s.rstrip("%"))
    except Exception:
        return None
