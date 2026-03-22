"""Reachability verification — verify-worker orchestration, redirect checks.

Replaces run_verify_worker, wait_for_takeover, wait_for_recovery, verify_redirects,
and diagnose_stragglers from bash.
"""

import json
import random
import time
from collections import Counter

from .config import StressConfig
from .phases import StressLogger, PhaseResult, fmt_duration
from .metrics import WorkerInfoStore, Dashboard

from onionpress.docker import Docker


def run_verify_worker(
    docker: Docker,
    config: StressConfig,
    logger: StressLogger,
    store: WorkerInfoStore,
    expected_code: str,
    target_count: int,
    timeout_secs: int,
    poll_start: int,
    poll_count: int,
    addr_type: str = "content",
) -> PhaseResult:
    """Launch verify-worker.py across poll clients and aggregate results.

    Distributes addresses across poll clients using list[list[str]]
    (replaces bash eval arrays).

    Args:
        expected_code: "200" or "302"
        target_count: Number of sites to verify
        timeout_secs: Maximum seconds to wait
        poll_start: Global index of first site to check
        poll_count: Number of sites to check
        addr_type: "content" or "healthcheck"

    Returns:
        PhaseResult with success/failure and timing.
    """
    # Get addresses
    if addr_type == "healthcheck":
        addrs = store.get_hc_addrs(poll_start, poll_count)
    else:
        addrs = store.get_content_addrs(poll_start, poll_count)

    if not addrs:
        return PhaseResult(False, "No addresses to verify")

    # Distribute addresses across poll clients (round-robin)
    num_clients = config.num_poll_clients
    client_addrs: list[list[str]] = [[] for _ in range(num_clients)]
    for i, addr in enumerate(addrs):
        client_addrs[i % num_clients].append(addr)

    # Kill previous verify-workers and launch new ones
    for i in range(num_clients):
        cname = config.poll_client_name(i)
        docker.exec(cname,
            "pkill -f verify-worker.py 2>/dev/null; rm -f /tmp/verify-results.json",
            timeout=10)

        if not client_addrs[i]:
            continue

        addr_args = " ".join(client_addrs[i])
        docker.exec(cname,
            f"python3 /verify-worker.py {expected_code} --timeout {timeout_secs} {addr_args} > /tmp/verify-worker.log 2>&1 &",
            timeout=10)

    # Wait for verify-workers to start
    time.sleep(10)

    # Poll results
    start_ts = time.time()
    deadline = start_ts + timeout_secs
    last_dashboard = 0.0
    prev_verified = 0
    total_verified = 0

    while time.time() < deadline:
        time.sleep(5)

        total_verified = 0
        total_pending = 0
        code_counts: Counter = Counter()

        for i in range(num_clients):
            cname = config.poll_client_name(i)
            result = docker.exec(cname, "cat /tmp/verify-results.json", timeout=10)
            if not result.ok or not result.output.strip():
                continue
            try:
                data = json.loads(result.output)
                total_verified += data.get("verified", 0)
                total_pending += len(data.get("pending", []))
                for info in data.get("results", {}).values():
                    if isinstance(info, dict):
                        code_counts[info.get("code", "?")] += 1
            except json.JSONDecodeError:
                continue

        # Progress dots
        if total_verified > prev_verified:
            logger.progress_dot(total_verified - prev_verified)
            prev_verified = total_verified

        now = time.time()
        if now - last_dashboard >= 10:
            codes_str = " ".join(f"{code}:{n}" for code, n in sorted(code_counts.items()))
            logger.log(f"  (verified: {total_verified}/{target_count}, pending: {total_pending}) [{codes_str}]")
            last_dashboard = now

        if total_verified >= target_count:
            elapsed = int(time.time() - start_ts)
            # Kill verify-workers
            for i in range(num_clients):
                docker.exec(config.poll_client_name(i),
                    "pkill -f verify-worker.py 2>/dev/null", timeout=10)
            msg = f"{total_verified}/{target_count} verified in {fmt_duration(elapsed)}"
            return PhaseResult(True, msg, elapsed)

    # Timed out
    elapsed = int(time.time() - start_ts)
    for i in range(num_clients):
        cname = config.poll_client_name(i)
        docker.exec(cname, "pkill -f verify-worker.py 2>/dev/null", timeout=10)
        result = docker.exec(cname, "tail -5 /tmp/verify-worker.log", timeout=10)
        if result.ok and result.output.strip():
            logger.log(f"  verify-worker-{i} log: {result.output.strip()}")

    msg = f"{total_verified}/{target_count} verified, timed out after {fmt_duration(elapsed)}"
    return PhaseResult(False, msg, elapsed)


def wait_for_bootstrap(
    docker: Docker,
    config: StressConfig,
    logger: StressLogger,
    store: WorkerInfoStore,
) -> PhaseResult:
    """Wait for all sites to bootstrap and register with OnionHeaven.

    Reads from the shared SQLite DB via one docker exec per poll cycle.
    """
    timeout = config.bootstrap_timeout
    logger.log(f"Waiting for all sites to bootstrap and register (timeout: {timeout}s)...")

    start_ts = time.time()
    deadline = start_ts + timeout
    last_status = 0.0
    registered_count = 0
    prev_registered = 0
    total_in_db = 0

    while time.time() < deadline:
        total_in_db, registered_count = store.refresh_counts()

        # Progress dots
        if registered_count > prev_registered:
            logger.progress_dot(registered_count - prev_registered)
            prev_registered = registered_count

        now = time.time()
        if now - last_status >= 10:
            logger.log(f"  Bootstrap: {total_in_db}/{config.total} sites in DB, {registered_count}/{config.total} registered")
            last_status = now

        if total_in_db >= config.total and registered_count >= config.total:
            elapsed = int(time.time() - start_ts)
            logger.progress_end(f"{registered_count}/{config.total}")
            msg = f"{registered_count}/{config.total} registered in {fmt_duration(elapsed)}"
            logger.log(f"All stress containers bootstrapped: {registered_count} sites registered")
            return PhaseResult(True, msg, elapsed)

        time.sleep(5)

    elapsed = int(time.time() - start_ts)
    logger.progress_end(f"{registered_count}/{config.total} (timed out)")
    msg = f"{registered_count}/{config.total} registered, timed out after {fmt_duration(elapsed)}"
    logger.log("WARNING: Bootstrap timed out — some sites not ready")
    return PhaseResult(False, msg, elapsed)


def wait_for_takeover(
    docker: Docker,
    config: StressConfig,
    logger: StressLogger,
    store: WorkerInfoStore,
    dashboard: Dashboard,
    expected: int,
    timeout_secs: int,
    poll_start: int | None = None,
    poll_count: int | None = None,
) -> PhaseResult:
    """Wait for expected number of takeovers (HTTP 302)."""
    if poll_start is None:
        poll_start = config.fail_start
    if poll_count is None:
        poll_count = config.failing

    logger.log(f"Waiting for {expected} takeovers via verify-worker (timeout: {timeout_secs}s)...")

    result = run_verify_worker(
        docker, config, logger, store,
        "302", expected, timeout_secs, poll_start, poll_count,
    )

    if result.success:
        logger.progress_end(f"{expected}/{expected}")
    else:
        logger.progress_end("(timed out)")
        # Diagnose stragglers
        addrs = store.get_content_addrs(poll_start, poll_count)
        _diagnose_stragglers(docker, config, logger, addrs, "302")

    logger.log(f"Takeover: {result.message}")
    dashboard.print_dashboard()
    return result


def wait_for_recovery(
    docker: Docker,
    config: StressConfig,
    logger: StressLogger,
    store: WorkerInfoStore,
    dashboard: Dashboard,
    expected: int,
    timeout_secs: int,
    poll_start: int | None = None,
    poll_count: int | None = None,
) -> PhaseResult:
    """Wait for recovery (HTTP 200)."""
    if poll_start is None:
        poll_start = config.fail_start
    if poll_count is None:
        poll_count = config.failing

    logger.log(f"Waiting for recovery via verify-worker ({expected} sites, timeout: {timeout_secs}s)...")

    result = run_verify_worker(
        docker, config, logger, store,
        "200", expected, timeout_secs, poll_start, poll_count,
    )

    if result.success:
        logger.progress_end(f"{expected}/{expected}")
    else:
        logger.progress_end("(timed out)")
        addrs = store.get_content_addrs(poll_start, poll_count)
        _diagnose_stragglers(docker, config, logger, addrs, "200")

    logger.log(f"Recovery: {result.message}")
    dashboard.print_dashboard()
    return result


def verify_redirects(
    docker: Docker,
    config: StressConfig,
    logger: StressLogger,
    store: WorkerInfoStore,
    phase_label: str,
    sample_size: int = 5,
) -> PhaseResult:
    """Verify taken-over addresses redirect to Wayback Machine."""
    verify_start = time.time()
    deadline = verify_start + config.redirect_verify_timeout

    logger.log(f"{phase_label}: Verifying 302 redirects on sample of taken-over addresses...")

    # Use the addresses we know we disabled
    addrs = store.get_content_addrs(config.fail_start, config.failing)

    if not addrs:
        logger.log(f"{phase_label}: No taken-over addresses found — skipping redirect verification")
        return PhaseResult(True, "0/0 (skipped)")

    # Sample
    sampled = random.sample(addrs, min(sample_size, len(addrs)))

    # Find available poll clients
    verify_ctrs = []
    for ci in range(config.num_poll_clients):
        cname = config.poll_client_name(ci)
        result = docker.run(["inspect", cname], timeout=10)
        if result.ok:
            verify_ctrs.append(cname)

    if not verify_ctrs:
        verify_ctrs = ["onionpress-wordpress"]

    # Retry loop
    remaining = list(sampled)
    passed = 0
    round_num = 0

    while remaining and time.time() < deadline:
        round_num += 1
        still_failing = []

        for idx, addr in enumerate(remaining):
            vctr = verify_ctrs[idx % len(verify_ctrs)]
            result = docker.exec(vctr,
                f'curl -s -o /dev/null -w "%{{http_code}} %{{redirect_url}}" '
                f'--http1.0 --socks5-hostname "verify{idx}:x@127.0.0.1:9050" '
                f'--max-time 30 "http://{addr}"',
                timeout=35)

            if result.ok and result.output.strip():
                parts = result.output.strip().split(None, 1)
                code = parts[0]
                redirect_url = parts[1] if len(parts) > 1 else ""

                if code == "302" and ("web.archive.org" in redirect_url or "archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion" in redirect_url):
                    logger.log(f"  PASS: {addr} -> 302 -> {redirect_url}")
                    passed += 1
                else:
                    logger.log(f"  FAIL: {addr} -> {code} {redirect_url[:80]}")
                    still_failing.append(addr)
            else:
                code = result.output.strip()[:10] if result.ok else "000"
                logger.log(f"  FAIL: {addr} -> {code} (no response)")
                still_failing.append(addr)

        remaining = still_failing
        if remaining:
            elapsed = int(time.time() - verify_start)
            logger.log(f"  {phase_label}: {passed}/{len(sampled)} passed, {len(remaining)} still failing ({elapsed}s) — retrying in 15s...")
            time.sleep(15)

    # Log final failures
    for addr in remaining:
        logger.log(f"  FAIL: {addr} -> still unreachable after timeout")

    failed = len(sampled) - passed
    elapsed = int(time.time() - verify_start)
    msg = f"{passed}/{len(sampled)} passed, {failed} failed in {fmt_duration(elapsed)}"
    logger.log(f"{phase_label}: Redirect verification — {msg} ({len(addrs)} total taken over)")

    logger.log_json(
        f'"event":"redirect_verify","phase":"{phase_label}",'
        f'"total_taken":{len(addrs)},"sampled":{len(sampled)},"passed":{passed},"failed":{failed}'
    )

    return PhaseResult(passed == len(sampled), msg, elapsed)


def _diagnose_stragglers(
    docker: Docker,
    config: StressConfig,
    logger: StressLogger,
    addrs: list[str],
    expected_code: str,
):
    """Log which addresses are stuck and why."""
    logger.log("Straggler diagnostic:")
    for addr in addrs[:20]:  # Cap diagnostic at 20
        result = docker.exec(
            config.poll_client_name(0),
            f'curl -s --http1.0 --socks5-hostname "diag_{random.randint(0,9999)}:x@127.0.0.1:9050" '
            f'--max-time 10 -o /dev/null -w "%{{http_code}}" "http://{addr}/"',
            timeout=15,
        )
        code = result.output.strip() if result.ok else "000"
        if code != expected_code:
            logger.log(f"  STRAGGLER: {addr} -> HTTP {code} (wanted {expected_code})")
