#!/usr/bin/env python3
"""
OnionHeaven Heartbeat Monitor — passive takeover orchestrator

Runs inside the onionheaven container alongside Arti (SOCKS + keystore),
onionheaven-server.py (registration API), and onionheaven-redirect.sh (302 redirects).

Unlike the old poller, this does NOT actively ping OnionPress instances.
Instead, OnionPress instances send periodic /online heartbeats to the server,
which updates last_healthy timestamps. This monitor:

  1. Scans the DB for stale heartbeats (missed 3+ beats = 180s) → takeover
  2. Audits recent takeovers by pinging the healthcheck address → detect false positives
  3. Manages farm scaling for takeover workers

All operations are local:
  - SQLite via Python sqlite3 (shared volume)
  - Post-takeover audits via curl through local Arti SOCKS (127.0.0.1:9050)
  - Takeover/release via /onionheaven-tor-manager.sh (same container)
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from onionheaven_common import (
    db_connect, db_commit_with_retry, db_ensure_schema, log,
    takeover_function, release_function, unregister_entry, flush_sighup_tor,
    is_farm_mode, check_worker_bootstrap, cleanup_dead_workers,
    _init_worker_index, _ensure_capacity, _pick_worker, _exec_takeover,
    _check_arti_key_errors,
    PROPAGATION_DELAY, ONIONHEAVEN_PEER_GRACE, TOR_MANAGER,
)

def wall_sleep(seconds):
    """Sleep using wall-clock busy-wait — time.sleep() is unreliable under qemu."""
    deadline = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    while datetime.now(timezone.utc) < deadline:
        time.sleep(10)


# How often the monitor scans the DB (seconds)
HEARTBEAT_INTERVAL = int(os.environ.get("ONIONHEAVEN_HEARTBEAT_INTERVAL", "15"))


def _get_tor_detached(container_name):
    """Query a worker's Tor for its actual detached onion services.

    Returns a set of content addresses (with .onion suffix), or None on error.
    """
    try:
        result = subprocess.run(
            ["docker", "exec", container_name, "python3", "-c",
             "import socket, binascii\n"
             "cookie = open('/var/lib/tor/control_auth_cookie','rb').read()\n"
             "s = socket.socket()\n"
             "s.settimeout(10)\n"
             "s.connect(('127.0.0.1',9051))\n"
             "s.send(('AUTHENTICATE ' + binascii.hexlify(cookie).decode() + '\\r\\n').encode())\n"
             "s.recv(256)\n"
             "s.send(b'GETINFO onions/detached\\r\\n')\n"
             "data = b''\n"
             "while True:\n"
             "    chunk = s.recv(8192)\n"
             "    if not chunk: break\n"
             "    data += chunk\n"
             "    if b'250 OK' in data: break\n"
             "s.close()\n"
             "for l in data.decode().strip().split('\\n'):\n"
             "    l = l.strip()\n"
             "    if l and not l.startswith('250') and l != '.':\n"
             "        print(l + '.onion')\n"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return None
        onions = set()
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line:
                onions.add(line)
        return onions
    except Exception:
        return None


def startup_reconciliation(conn):
    """Reconcile DB state after container restart.

    Taken-over entries: re-execute takeovers (container restart wiped Arti config,
    so the onion services need to be re-added). These stay taken-over — they were
    offline before the restart and probably still are.

    Online entries: reset last_healthy to now, giving OnionPress instances a full
    grace period (180s) to send their first heartbeat before we consider them stale.
    Without this, every restart would trigger a thundering herd of takeovers.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Re-execute takeovers for entries that were taken-over before restart
    # (Arti config was wiped, so we need to re-add the onion services)
    # But first: if the content_address has an online sibling, the takeover
    # is stale (instance re-registered with a new healthcheck) — unregister instead.
    taken_over = conn.execute(
        "SELECT content_address, healthcheck_address FROM registry "
        "WHERE status = 'taken-over' AND unregistered_at IS NULL"
    ).fetchall()

    re_takeover_addrs = []
    if taken_over:
        for row in taken_over:
            ca, ha = row[0], row[1]
            sibling_online = conn.execute(
                "SELECT 1 FROM registry WHERE content_address = ? AND status = 'online' "
                "AND unregistered_at IS NULL LIMIT 1",
                (ca,)
            ).fetchone()
            if sibling_online:
                log(f"  startup: {ca} has online sibling — unregistering stale takeover for {ha}")
                unregister_entry(conn, ca, ha, reason="superseded-by-new-healthcheck")
            else:
                if ca not in re_takeover_addrs:
                    re_takeover_addrs.append(ca)
        db_commit_with_retry(conn)

    if re_takeover_addrs:
        if is_farm_mode():
            # Clear old assignments — workers may have been restarted too.
            # Re-execute takeovers directly on available workers.
            conn.execute(
                "UPDATE registry SET takeover_container = NULL "
                "WHERE status = 'taken-over' AND unregistered_at IS NULL"
            )
            # Reset assigned_count since we cleared all assignments
            conn.execute("UPDATE takeover_containers SET assigned_count = 0")
            db_commit_with_retry(conn)

            # Wait for workers to bootstrap before re-assigning
            log(f"startup reconciliation: {len(re_takeover_addrs)} takeover(s) need re-execution, waiting for workers...")
            for _ in range(12):  # wait up to 60s for workers
                check_worker_bootstrap(conn)
                worker = _pick_worker(conn)
                if worker:
                    break
                time.sleep(5)

            if _pick_worker(conn):
                for addr in re_takeover_addrs:
                    # Find the registry row for this address
                    row = conn.execute(
                        "SELECT healthcheck_address FROM registry "
                        "WHERE content_address = ? AND status = 'taken-over' "
                        "AND unregistered_at IS NULL LIMIT 1",
                        (addr,)
                    ).fetchone()
                    if row:
                        worker = _pick_worker(conn)
                        if worker and _exec_takeover(worker, addr):
                            conn.execute(
                                "UPDATE registry SET takeover_container = ? "
                                "WHERE content_address = ? AND healthcheck_address = ?",
                                (worker, addr, row["healthcheck_address"])
                            )
                            conn.execute(
                                "UPDATE takeover_containers SET assigned_count = assigned_count + 1 "
                                "WHERE container_name = ?",
                                (worker,)
                            )
                db_commit_with_retry(conn)
                log(f"startup reconciliation: re-executed {len(re_takeover_addrs)} takeover(s) on workers")
            else:
                log(f"startup reconciliation: no workers available — {len(re_takeover_addrs)} takeover(s) will be assigned when workers bootstrap")
        else:
            log(f"startup reconciliation: re-executing {len(re_takeover_addrs)} takeover(s) locally")
            for addr in re_takeover_addrs:
                try:
                    subprocess.run([TOR_MANAGER, "takeover", "--no-sighup", addr],
                                   capture_output=True, text=True, timeout=30)
                    log(f"  re-took-over {addr}")
                except Exception as e:
                    log(f"  failed to re-takeover {addr}: {e}")

    # Clean up duplicate online rows for the same content_address.
    # Keep the one with the newest last_healthy, unregister the rest.
    dupes = conn.execute(
        "SELECT content_address FROM registry "
        "WHERE status = 'online' AND unregistered_at IS NULL "
        "GROUP BY content_address HAVING COUNT(*) > 1"
    ).fetchall()
    for dupe in dupes:
        ca = dupe[0]
        # Keep the row with the most recent last_healthy
        stale_rows = conn.execute(
            "SELECT healthcheck_address, last_healthy FROM registry "
            "WHERE content_address = ? AND status = 'online' AND unregistered_at IS NULL "
            "ORDER BY last_healthy DESC",
            (ca,)
        ).fetchall()
        for stale in stale_rows[1:]:  # skip the newest
            log(f"  startup: unregistering duplicate online row for {ca} (hc: {stale[0]})")
            unregister_entry(conn, ca, stale[0], reason="superseded-by-new-healthcheck")

    # Give online entries a fresh grace period
    conn.execute(
        "UPDATE registry SET last_healthy = ?, audit_result = NULL, audit_at = NULL "
        "WHERE status = 'online' AND unregistered_at IS NULL",
        (now,)
    )
    db_commit_with_retry(conn)

    online_count = conn.execute(
        "SELECT COUNT(*) FROM registry WHERE status = 'online' AND unregistered_at IS NULL"
    ).fetchone()[0]
    taken_count = len(taken_over) if taken_over else 0
    log(f"startup reconciliation complete — {online_count} online (grace period reset), {taken_count} taken-over (re-executed)")

    # Clean up dead worker containers
    cleanup_dead_workers(conn)


# ---------------------------------------------------------------------------
# Main heartbeat monitor loop
# ---------------------------------------------------------------------------

def main():
    log("heartbeat monitor starting")

    # Wait for the DB directory to exist
    for _ in range(30):
        if os.path.isdir("/var/lib/onionpress/onionheaven"):
            break
        time.sleep(2)
    else:
        log("WARNING: data dir not found after 60s, creating it")
        os.makedirs("/var/lib/onionpress/onionheaven", exist_ok=True)

    conn = db_connect()
    db_ensure_schema(conn)
    _init_worker_index(conn)
    startup_reconciliation(conn)
    conn.close()

    # Scan Arti keystore for corrupted keys left over from previous runs
    log("Scanning Arti keystore for corrupted keys...")
    _check_arti_key_errors()

    log("heartbeat monitor started")

    while True:
        try:
            conn = db_connect()

            # Check worker bootstrap status and clean up dead workers
            if is_farm_mode():
                check_worker_bootstrap(conn)
                cleanup_dead_workers(conn)

                # Re-sync worker index if takeover_containers is empty
                # (e.g. after a reset-onionheaven cleared the table)
                try:
                    tc_count = conn.execute(
                        "SELECT COUNT(*) FROM takeover_containers"
                    ).fetchone()[0]
                    if tc_count == 0:
                        _init_worker_index(conn)
                except sqlite3.OperationalError:
                    pass

                # Check for assigned_count drift
                try:
                    workers = conn.execute(
                        "SELECT container_name, assigned_count FROM takeover_containers"
                    ).fetchall()
                    for w in workers:
                        actual = conn.execute(
                            "SELECT COUNT(*) FROM registry WHERE status='taken-over' "
                            "AND takeover_container = ? AND unregistered_at IS NULL",
                            (w["container_name"],)
                        ).fetchone()[0]
                        if actual != w["assigned_count"]:
                            log(f"WARNING: assigned_count drift on {w['container_name']}: "
                                f"DB says {w['assigned_count']}, actual {actual}")
                except sqlite3.OperationalError:
                    pass

                # Reconcile: compare what Tor actually serves vs what DB expects.
                # Log-only for now — helps diagnose silent service drops.
                try:
                    for w in (workers if workers else []):
                        name = w["container_name"]
                        tor_onions = _get_tor_detached(name)
                        if tor_onions is None:
                            continue
                        db_rows = conn.execute(
                            "SELECT content_address FROM registry "
                            "WHERE takeover_container = ? AND status = 'taken-over' "
                            "AND unregistered_at IS NULL",
                            (name,)
                        ).fetchall()
                        db_addrs = set(r["content_address"] for r in db_rows)
                        dropped = db_addrs - tor_onions
                        extra = tor_onions - db_addrs
                        if dropped or extra:
                            log(f"RECONCILE: {name} — "
                                f"Tor has {len(tor_onions)}, DB expects {len(db_addrs)}, "
                                f"dropped={len(dropped)}, extra={len(extra)}")
                except Exception as e:
                    log(f"RECONCILE error: {e}")

            # Get list of active entry keys (content_address + healthcheck_address).
            # We only fetch the keys here — each entry is re-queried fresh before
            # acting on it, so we never act on stale data from a snapshot.
            entry_keys = conn.execute(
                "SELECT content_address, healthcheck_address FROM registry "
                "WHERE unregistered_at IS NULL ORDER BY registered_at"
            ).fetchall()

            if not entry_keys:
                log("heartbeat pass complete — 0 entries in 0.0s")
                conn.close()
                wall_sleep(HEARTBEAT_INTERVAL)
                continue

            pass_start = datetime.now(timezone.utc)

            stale_count = 0
            stale_cleanup_count = 0

            for key_row in entry_keys:
                ca = key_row["content_address"]
                ha = key_row["healthcheck_address"]

                # Re-query this entry fresh — it may have changed since we fetched the key list
                now = datetime.now(timezone.utc)
                now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                entry = conn.execute(
                    "SELECT * FROM registry WHERE content_address = ? AND healthcheck_address = ? "
                    "AND unregistered_at IS NULL",
                    (ca, ha)
                ).fetchone()
                if not entry:
                    continue  # entry was unregistered or removed since we fetched keys
                entry = dict(entry)

                if entry["status"] == "online":
                    # Check if heartbeat is stale
                    last_healthy_stale = True
                    if entry["last_healthy"]:
                        try:
                            lh = datetime.fromisoformat(
                                entry["last_healthy"].replace("Z", "+00:00")
                            )
                            elapsed = (now - lh).total_seconds()
                            last_healthy_stale = elapsed > PROPAGATION_DELAY
                        except (ValueError, TypeError):
                            last_healthy_stale = True

                    if last_healthy_stale:
                        # OnionHeaven peers get a longer grace period before takeover.
                        # They run OnionHeaven themselves, so restarts take longer and
                        # a premature takeover would redirect their hosted sites.
                        if entry.get("is_onionheaven"):
                            if entry["last_healthy"]:
                                try:
                                    lh = datetime.fromisoformat(
                                        entry["last_healthy"].replace("Z", "+00:00")
                                    )
                                    peer_elapsed = (now - lh).total_seconds()
                                except (ValueError, TypeError):
                                    peer_elapsed = ONIONHEAVEN_PEER_GRACE + 1
                            else:
                                peer_elapsed = ONIONHEAVEN_PEER_GRACE + 1
                            if peer_elapsed < ONIONHEAVEN_PEER_GRACE:
                                log(f"Stale heartbeat for {ha} (content: {ca}) — OnionHeaven peer, waiting ({int(peer_elapsed)}s / {ONIONHEAVEN_PEER_GRACE}s grace)")
                                continue
                            log(f"Stale heartbeat for {ha} (content: {ca}) — OnionHeaven peer grace period exceeded ({int(peer_elapsed)}s), triggering takeover")
                        stale_count += 1
                        log(f"Stale heartbeat for {ha} (content: {ca}) — triggering takeover")
                        takeover_function(conn, ca, ha, force=False)

                elif entry["status"] == "taken-over":
                    # Unassigned taken-over entry — execute takeover directly.
                    if not entry.get("takeover_container") and is_farm_mode():
                        worker = _pick_worker(conn)
                        if worker and _exec_takeover(worker, ca):
                            conn.execute(
                                "UPDATE registry SET takeover_container = ? "
                                "WHERE content_address = ? AND healthcheck_address = ?",
                                (worker, ca, ha)
                            )
                            conn.execute(
                                "UPDATE takeover_containers SET assigned_count = assigned_count + 1 "
                                "WHERE container_name = ?",
                                (worker,)
                            )
                            db_commit_with_retry(conn)
                        _ensure_capacity(conn)

                    # If another row for the same content_address is online, release this takeover.
                    # This happens when an instance re-registers with a new healthcheck address.
                    sibling_online = conn.execute(
                        "SELECT 1 FROM registry WHERE content_address = ? AND status = 'online' "
                        "AND unregistered_at IS NULL LIMIT 1",
                        (ca,)
                    ).fetchone()
                    if sibling_online:
                        log(f"Content address {ca} has an online sibling — releasing stale takeover for {ha}")
                        unregister_entry(conn, ca, ha, reason="superseded-by-new-healthcheck")
                        continue

                    # Compute since_takeover for audit queueing and auto-cleanup
                    last_taken_over = entry.get("last_taken_over")
                    since_takeover = 0
                    if last_taken_over:
                        try:
                            lto = datetime.fromisoformat(
                                last_taken_over.replace("Z", "+00:00")
                            )
                            since_takeover = (now - lto).total_seconds()
                        except (ValueError, TypeError):
                            pass

                    # Queue audit on the takeover worker (no curl here)
                    audit_result = entry.get("audit_result")
                    if not audit_result and not entry.get("audit_pending"):
                        if 10 < since_takeover <= 300:
                            conn.execute(
                                "UPDATE registry SET audit_pending = ? "
                                "WHERE content_address = ? AND healthcheck_address = ?",
                                (now_str, ca, ha))
                            db_commit_with_retry(conn)
                        elif since_takeover > 300:
                            conn.execute(
                                "UPDATE registry SET audit_result = 'confirmed_dead', audit_at = ? "
                                "WHERE content_address = ? AND healthcheck_address = ?",
                                (now_str, ca, ha))
                            db_commit_with_retry(conn)

                    # Auto-cleanup: unregister stress-test entries taken-over for >2 hours
                    # Real users will re-register when they come back online; stress tests won't.
                    version = entry.get("version", "")
                    if version and version.startswith("stress-test") and since_takeover > 7200:
                        stale_cleanup_count += 1
                        log(f"Auto-cleanup stale stress-test entry: {ca} (taken-over {since_takeover/3600:.1f}h ago)")
                        unregister_entry(conn, ca, ha, reason="stale-stress-test-cleanup")

                db_commit_with_retry(conn)

            # Flush pending SIGHUPs (only relevant for non-farm local Tor)
            if not is_farm_mode():
                flush_sighup_tor()

            elapsed = (datetime.now(timezone.utc) - pass_start).total_seconds()
            parts = [f"{len(entry_keys)} entries"]
            if stale_count:
                parts.append(f"{stale_count} takeovers")
            if stale_cleanup_count:
                parts.append(f"{stale_cleanup_count} stress-test cleanups")
            log(f"heartbeat pass complete — {', '.join(parts)} in {elapsed:.1f}s")

            conn.close()
            wall_sleep(max(HEARTBEAT_INTERVAL, elapsed))

        except Exception as e:
            log(f"heartbeat monitor error: {e}")
            try:
                conn.close()
            except Exception:
                pass
            wall_sleep(60)


if __name__ == "__main__":
    main()
