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
) -> list[str]:
    """Generate signed JSON payloads for a range of sites.

    Args:
        store: WorkerInfoStore with loaded worker info.
        endpoint: API endpoint name ('offline', 'online', 'unregister').
        start: Global index of first site.
        count: Number of sites.

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
        payloads.append(json.dumps({
            "content_address": ca,
            "healthcheck_address": ha,
            "timestamp": ts,
            "signature": sig,
        }))
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


def send_notifications(
    docker: Docker,
    logger: StressLogger,
    config: StressConfig,
    endpoint: str,
    payloads: list[str],
    max_parallel: int = 10,
) -> int:
    """Send notification payloads to OnionHeaven API over Tor.

    Returns:
        Number of successfully sent notifications.
    """
    if not payloads:
        logger.log(f"WARNING: No payloads generated for /{endpoint}")
        return 0

    logger.log(f"  Generated {len(payloads)} /{endpoint} payload(s)")

    payload_text = "\n".join(payloads)
    debug_log = os.path.join(config.output_dir, f"notify_{endpoint}_debug.log")

    tag = endpoint[:3]
    curl_cmd = (
        f'curl -s -o /dev/null -w "%{{http_code}}" '
        f'--socks5-hostname "{tag}${{i}}r${{attempt}}:x@127.0.0.1:9050" '
        f'--max-time 30 '
        f'-X POST "http://{config.onionheaven_addr}:8083/{endpoint}" '
        f'-H "Content-Type: application/json" '
        f'-d "$payload"'
    )

    shell_script = f"""
tmpdir=$(mktemp -d); i=0
while IFS= read -r payload; do
    i=$((i+1))
    (for attempt in 1 2 3; do
        code=$({curl_cmd} 2>/dev/null)
        if [ "$code" = "200" ]; then
            touch "$tmpdir/ok.$i"
            break
        fi
        [ "$attempt" -lt 3 ] && sleep 5
     done) &
    [ $((i % {max_parallel})) -eq 0 ] && wait
done
wait
ls "$tmpdir"/ok.* 2>/dev/null | wc -l | tr -d " "
rm -rf "$tmpdir"
"""

    result = docker.run(
        ["exec", "-i", "onionpress-tor-client", "sh", "-c", shell_script],
        timeout=300,
        input=payload_text,
    )

    notified = 0
    if result.ok:
        try:
            notified = int(result.output.strip())
        except ValueError:
            pass

    if result.stderr:
        with open(debug_log, "a") as f:
            f.write(result.stderr)
        logger.log(f"  notify_{endpoint} debug: {result.stderr.strip()[:200]}")

    logger.log(f"Sent /{endpoint} for {notified} sites")
    if notified < len(payloads):
        missed = len(payloads) - notified
        logger.log(f"WARNING: {missed} /{endpoint} notification(s) failed")

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
