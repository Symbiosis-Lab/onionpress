#!/usr/bin/env python3
"""OnionHeaven live dashboard — standalone registry monitor.

Queries the registry via docker exec (local mode) or the /status API
(remote mode). Local mode has richer data.

Usage:
    python3 tests/stress/dashboard.py                          # local (Pi)
    python3 tests/stress/dashboard.py --remote 192.168.178.136 # remote via API
    python3 tests/stress/dashboard.py --interval 5             # faster refresh
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime


# ---------------------------------------------------------------------------
# Docker helpers (local mode)
# ---------------------------------------------------------------------------

def docker_exec(container, cmd, timeout=10):
    """Run a command inside a container, return stdout or None on failure."""
    try:
        result = subprocess.run(
            ["docker", "exec", container, "sh", "-c", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, Exception):
        pass
    return None


def docker_run(args, timeout=10):
    """Run a docker command, return stdout or None on failure."""
    try:
        result = subprocess.run(
            ["docker"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, Exception):
        pass
    return None


# ---------------------------------------------------------------------------
# Local queries (docker exec + sqlite3)
# ---------------------------------------------------------------------------

DB = "/var/lib/onionpress/onionheaven/registry.db"


def query_registry():
    """Query registry for status counts."""
    sql = (
        "SELECT "
        "  SUM(CASE WHEN status='online' AND unregistered_at IS NULL THEN 1 ELSE 0 END), "
        "  SUM(CASE WHEN status='taken-over' AND unregistered_at IS NULL THEN 1 ELSE 0 END), "
        "  SUM(CASE WHEN unregistered_at IS NOT NULL THEN 1 ELSE 0 END), "
        "  SUM(CASE WHEN unregistered_at IS NULL THEN 1 ELSE 0 END) "
        "FROM registry;"
    )
    out = docker_exec("onionheaven", f"sqlite3 {DB} \"{sql}\"")
    if not out:
        return None
    parts = out.split("|")
    if len(parts) < 4:
        return None
    return {
        "online": int(parts[0] or 0),
        "taken_over": int(parts[1] or 0),
        "unregistered": int(parts[2] or 0),
        "active": int(parts[3] or 0),
    }


def query_versions():
    """Get distinct versions of active entries."""
    sql = (
        "SELECT version, COUNT(*) FROM registry "
        "WHERE unregistered_at IS NULL GROUP BY version ORDER BY COUNT(*) DESC LIMIT 5;"
    )
    out = docker_exec("onionheaven", f"sqlite3 {DB} \"{sql}\"")
    if not out:
        return []
    versions = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) == 2:
            versions.append((parts[0], int(parts[1])))
    return versions


def query_workers():
    """Get takeover worker status from DB."""
    sql = (
        "SELECT container_name, assigned_count, bootstrapped, max_services "
        "FROM takeover_containers ORDER BY container_name;"
    )
    out = docker_exec("onionheaven", f"sqlite3 {DB} \"{sql}\"")
    if not out:
        return []
    workers = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) >= 4:
            workers.append({
                "name": parts[0],
                "assigned": int(parts[1]),
                "bootstrapped": bool(int(parts[2])),
                "max": int(parts[3]),
            })
    return workers


def query_queue_status(container_name):
    """Get queue manager status from a worker."""
    out = docker_exec(container_name,
                      "python3 /onionheaven-queue-manager.py status", timeout=5)
    if not out:
        return None
    try:
        return json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None


def get_heartbeat_info():
    """Get last heartbeat line from the log."""
    out = docker_exec("onionheaven",
                      "tail -1 /var/lib/onionpress/onionheaven/heartbeat.log")
    if not out:
        return "?", "?"
    try:
        ts_str = out.split("]")[0].lstrip("[").strip()
        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        age = (datetime.now() - ts).total_seconds()
        # Extract the message after the timestamp + prefix
        msg = out.split("OnionHeaven: ", 1)[-1] if "OnionHeaven: " in out else ""
        return f"{age:.0f}s ago", msg
    except (ValueError, IndexError):
        return "?", ""


def count_containers(prefix):
    """Count running containers matching a name prefix."""
    out = docker_run(["ps", "--filter", f"name={prefix}", "--format", "{{.Names}}"])
    if not out:
        return 0
    return len(out.splitlines())


def get_container_mem(name):
    """Get memory usage string for a container."""
    out = docker_run(["stats", "--no-stream", "--format", "{{.MemUsage}}", name])
    if not out:
        return "?"
    return out.split("/")[0].strip()


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def clear_screen():
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def c(color, text):
    """Wrap text in ANSI color. green=32, yellow=33, red=31, cyan=36."""
    return f"\033[{color}m{text}\033[0m"


def print_dashboard(iteration):
    """Print one dashboard frame (local mode)."""
    now = datetime.now().strftime("%H:%M:%S")

    reg = query_registry()
    if not reg:
        print(f"[{now}] #{iteration} | ERROR: cannot query registry")
        return

    workers = query_workers()
    versions = query_versions()
    hb_age, hb_msg = get_heartbeat_info()

    # Header
    print(f"[{now}] OnionHeaven Dashboard  #{iteration}  (heartbeat: {hb_age})")
    print(f"{'─' * 72}")

    # Registry
    online = reg["online"]
    taken = reg["taken_over"]
    active = reg["active"]
    unreg = reg["unregistered"]
    print(f"  Registry:    {active} active  |  "
          f"{c(32, f'{online} online')}  |  "
          f"{c(33, f'{taken} taken-over')}"
          f"{f'  |  {unreg} unregistered' if unreg else ''}")

    # Versions
    if versions:
        ver_parts = [f"{v}: {n}" for v, n in versions]
        print(f"  Versions:    {', '.join(ver_parts)}")

    # Last heartbeat message
    if hb_msg:
        if len(hb_msg) > 60:
            hb_msg = hb_msg[:57] + "..."
        print(f"  Last pass:   {hb_msg}")

    # Workers
    if workers:
        print(f"  Workers:")
        for w in workers:
            boot = c(32, "✓") if w["bootstrapped"] else c(31, "⏳")
            bar_len = 20
            fill = int(bar_len * w["assigned"] / max(w["max"], 1))
            bar = "█" * fill + "░" * (bar_len - fill)

            line = f"    {w['name']:30s}  {boot}  [{bar}] {w['assigned']}/{w['max']}"

            # Queue details
            qs = query_queue_status(w["name"])
            if qs:
                q_parts = []
                active_q = qs.get("active", 0)
                queued = qs.get("queued", 0)
                fly = qs.get("in_flight", 0)
                failed = qs.get("failed", 0)
                if active_q:
                    q_parts.append(f"act={active_q}")
                if queued:
                    q_parts.append(f"q={queued}")
                if fly:
                    q_parts.append(f"fly={fly}")
                if failed:
                    q_parts.append(c(31, f"fail={failed}"))
                if q_parts:
                    line += f"  ({', '.join(q_parts)})"

            print(line)
    else:
        print(f"  Workers:     none")

    # Containers + memory
    stress = count_containers("stress-worker-")
    poll = count_containers("stress-poll-client-")
    takeover = count_containers("onionheaven-takeover-")
    oh_mem = get_container_mem("onionheaven")

    ctr_parts = [f"{takeover} takeover"]
    if stress:
        ctr_parts.append(f"{stress} stress")
    if poll:
        ctr_parts.append(f"{poll} poll")
    print(f"  Containers:  {', '.join(ctr_parts)}  |  OH mem: {oh_mem}")

    print(f"{'─' * 72}")


def main():
    parser = argparse.ArgumentParser(description="OnionHeaven live dashboard")
    parser.add_argument("--interval", "-i", type=int, default=15,
                        help="Refresh interval in seconds (default: 15)")
    parser.add_argument("--once", action="store_true",
                        help="Print once and exit")
    args = parser.parse_args()

    iteration = 0
    try:
        while True:
            iteration += 1
            if not args.once:
                clear_screen()
            print_dashboard(iteration)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


if __name__ == "__main__":
    main()
