"""OnionHeaven API notifications — /offline, /online, /unregister.

Replaces ~400 lines of nearly-identical bash (notify_offline/notify_online).
Generates signed payloads via direct onion_auth calls (no subprocess).
"""

import base64
import json
import os
import sys

from .config import StressConfig
from .phases import StressLogger
from .metrics import WorkerInfoStore

from onionpress.docker import Docker

# Import onion_auth from the repo src
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))
from onion_auth import sign_payload, make_timestamp


def generate_signed_payloads(
    store: WorkerInfoStore,
    endpoint: str,
    start: int,
    count: int,
    stress_version: str = "",
) -> list[str]:
    """Generate signed JSON payloads for a range of sites.

    Args:
        store: WorkerInfoStore with loaded worker info.
        endpoint: API endpoint name ('offline', 'online', 'unregister').
        start: Global index of first site.
        count: Number of sites.
        stress_version: Version string (required for 'online' payloads).

    Returns:
        List of JSON strings, one per site.
    """
    payloads = []
    for i in range(start, start + count):
        worker = store.get_worker(i)
        if not worker:
            continue
        ca = worker.get("content_address", "")
        ha = worker.get("healthcheck_address", "")
        pk = worker.get("privkey_b64", "")
        pub = worker.get("pubkey_b64", "")
        if not (ca and ha and pk and pub):
            continue

        privkey = base64.b64decode(pk)
        pubkey = base64.b64decode(pub)
        ts = make_timestamp()
        sig = sign_payload(privkey, pubkey, endpoint, ca, ha, ts)
        payload = {
            "content_address": ca,
            "healthcheck_address": ha,
            "timestamp": ts,
            "signature": sig,
        }
        # /online requires arti_key_pem and version
        if endpoint == "online":
            payload["arti_key_pem"] = worker.get("arti_key_pem", "")
            payload["version"] = stress_version
            payload["wordpress_healthy"] = True
        payloads.append(json.dumps(payload))
    return payloads


def generate_unregister_payloads(store: WorkerInfoStore) -> list[str]:
    """Generate signed /unregister payloads for ALL sites in the store."""
    payloads = []
    seen = set()
    for worker in store.all_workers():
        ca = worker.get("content_address", "")
        ha = worker.get("healthcheck_address", "")
        pk = worker.get("privkey_b64", "")
        pub = worker.get("pubkey_b64", "")
        if not (ca and pk and pub) or ca in seen:
            continue
        seen.add(ca)
        privkey = base64.b64decode(pk)
        pubkey = base64.b64decode(pub)
        ts = make_timestamp()
        sig = sign_payload(privkey, pubkey, "unregister", ca, ha, ts)
        payloads.append(json.dumps({
            "content_address": ca,
            "healthcheck_address": ha,
            "timestamp": ts,
            "signature": sig,
        }))
    return payloads


def _send_one_notification(
    docker: Docker,
    config: StressConfig,
    endpoint: str,
    payload: str,
    index: int,
    max_attempts: int = 3,
) -> tuple[bool, str]:
    """Send a single notification with retries. Returns (success, detail)."""
    tag = endpoint[:3]
    code = "000"
    body = ""
    for attempt in range(1, max_attempts + 1):
        socks_tag = f"{tag}{index}r{attempt}"
        # Capture both HTTP code and response body
        result = docker.exec(
            "onionpress-tor-client",
            [
                "curl", "-s", "-w", "\n%{http_code}",
                "--socks5-hostname", f"{socks_tag}:x@127.0.0.1:9050",
                "--max-time", "30",
                "-X", "POST",
                f"http://{config.onionheaven_addr}:8083/{endpoint}",
                "-H", "Content-Type: application/json",
                "-d", payload,
            ],
            timeout=45,
        )
        if result.ok and result.output.strip():
            lines = result.output.strip().rsplit("\n", 1)
            body = lines[0] if len(lines) > 1 else ""
            code = lines[-1].strip()
        else:
            code = "000"
            body = result.stderr.strip()[:100] if result.stderr else ""

        if code == "200":
            return True, f"HTTP 200 (attempt {attempt})"
        if attempt < max_attempts:
            import time
            time.sleep(5)

    # Include the error response body so we know why it failed
    detail = f"HTTP {code} after {max_attempts} attempts"
    if body:
        detail += f": {body[:150]}"
    return False, detail


def send_notifications(
    docker: Docker,
    logger: StressLogger,
    config: StressConfig,
    endpoint: str,
    payloads: list[str],
    max_parallel: int = 10,
) -> int:
    """Send notification payloads to OnionHeaven API over Tor.

    Uses ThreadPoolExecutor for parallelism with per-payload logging.

    Returns:
        Number of successfully sent notifications.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not payloads:
        logger.log(f"WARNING: No payloads generated for /{endpoint}")
        return 0

    logger.log(f"  Generated {len(payloads)} /{endpoint} payload(s)")

    notified = 0
    failed_details = []

    with ThreadPoolExecutor(max_workers=max_parallel) as executor:
        futures = {}
        for i, payload in enumerate(payloads):
            # Extract content_address for logging
            try:
                addr = json.loads(payload).get("content_address", "?")
            except (json.JSONDecodeError, ValueError):
                addr = "?"
            fut = executor.submit(
                _send_one_notification, docker, config, endpoint, payload, i,
            )
            futures[fut] = (i, addr)

        for fut in as_completed(futures):
            idx, addr = futures[fut]
            try:
                success, detail = fut.result()
                if success:
                    notified += 1
                    logger.log(f"  /{endpoint} {addr[:20]}... → {detail}")
                else:
                    failed_details.append((addr, detail))
                    logger.log(f"  /{endpoint} FAILED {addr[:20]}... → {detail}")
            except Exception as e:
                failed_details.append((addr, str(e)))
                logger.log(f"  /{endpoint} ERROR {addr[:20]}... → {e}")

    logger.log(f"Sent /{endpoint} for {notified}/{len(payloads)} sites")
    if failed_details:
        logger.log(f"WARNING: {len(failed_details)} /{endpoint} notification(s) failed")

    logger.log_json(
        f'"event":"{endpoint}_notify","count":{len(payloads)},"notified":{notified}'
    )
    return notified


def flush_client_descriptor_cache(
    docker: Docker,
    config: StressConfig,
    logger: StressLogger,
    store: WorkerInfoStore,
    start: int,
    count: int,
):
    """Flush descriptor cache on all poll clients via NEWNYM + HSFETCH."""
    addrs = store.get_content_addrs(start, count)
    if not addrs:
        return

    # Strip .onion suffix
    sids = [a.replace(".onion", "") for a in addrs]
    logger.log(f"HSFETCH for {len(sids)} addresses across {config.num_poll_clients} poll clients...")

    # Step 1: NEWNYM on all poll clients
    for ci in range(config.num_poll_clients):
        cname = config.poll_client_name(ci)
        docker.exec(cname, """
            cookie=$(xxd -p /var/lib/tor/control_auth_cookie 2>/dev/null | tr -d '\\n')
            [ -z "$cookie" ] && exit 0
            printf 'AUTHENTICATE %s\\r\\nSIGNAL NEWNYM\\r\\nQUIT\\r\\n' "$cookie" | nc -w 5 127.0.0.1 9051 >/dev/null 2>&1
        """, timeout=15)

    import time
    time.sleep(3)

    # Step 2: HSFETCH on all poll clients
    hsfetch_cmds = "".join(f"HSFETCH {sid}\\r\\n" for sid in sids)
    for ci in range(config.num_poll_clients):
        cname = config.poll_client_name(ci)
        docker.exec(cname, f"""
            cookie=$(xxd -p /var/lib/tor/control_auth_cookie 2>/dev/null | tr -d '\\n')
            [ -z "$cookie" ] && exit 0
            printf 'AUTHENTICATE %s\\r\\n{hsfetch_cmds}QUIT\\r\\n' "$cookie" | nc -w 5 127.0.0.1 9051 >/dev/null 2>&1
        """, timeout=15)
