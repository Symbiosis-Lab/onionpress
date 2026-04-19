"""Smoke test for the Wayback archiving pipeline.

Publishes a throwaway test post, force-drains the queue, and for each URL
that save_post queued, polls SPN's /save/status/<job_id> endpoint until
the job terminates. Only counts a URL as "archived" when SPN reports
status=success — not when it just accepts the submission. SPN's .onion
crawler flakes out regularly (error:no-captures / "unreachable"), and a
submission ack doesn't mean the capture happened.

Exit 0 = every queued URL reached SPN status "success".
Exit 1 = one or more URLs failed to archive (submission rejected, SPN
        crawl failure, timeout, or system misconfiguration).
"""

import json
import re
import subprocess
import time
from typing import Callable


_WORDPRESS_CONTAINER = "onionpress-wordpress"
_LOG_PATH = "/var/lib/onionpress/wayback.log"
_QUEUE_PATH = "/var/lib/onionpress/wayback-queue.json"

# Archive.org's Save Page Now status endpoint. We reach it via the
# WordPress container curl'ing through onionpress-tor's SOCKS proxy.
_SPN_STATUS_URL = "https://web.archive.org/save/status/"

# How long to poll SPN per job. .onion crawls can take 30–120s; SPN also
# has rate-limiting that can delay a job further. 5 minutes is generous
# without being absurd.
_SPN_POLL_TIMEOUT_SEC = 300
_SPN_POLL_INTERVAL_SEC = 10


def _docker_exec(args: list, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", _WORDPRESS_CONTAINER] + args,
        capture_output=True, text=True, encoding='utf-8',
        errors='replace', **kwargs,
    )


def _wp(args: list, url: str = None, **kwargs) -> subprocess.CompletedProcess:
    cmd = ["wp"] + args + ["--path=/var/www/html", "--allow-root"]
    if url:
        cmd.append("--url=" + url)
    return _docker_exec(cmd, **kwargs)


def _read_container_file(path: str) -> str:
    r = _docker_exec(["cat", path], timeout=10)
    return r.stdout if r.returncode == 0 else ""


def _read_queue() -> list:
    raw = _read_container_file(_QUEUE_PATH)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def _pick_test_site() -> dict:
    """Return the site dict (blog_id, path, url) to publish the test post on.

    Prefers a real subsite (path != '/') over the network root, so the test
    exercises the same publish flow real users take. Falls back to the
    network root on single-site / branded installs.
    """
    r = _wp(["site", "list", "--fields=blog_id,path,url", "--format=json"],
            timeout=15)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"wp site list failed: {r.stderr or r.stdout}")
    sites = json.loads(r.stdout)
    subsites = [s for s in sites if s.get("path") != "/"]
    return subsites[0] if subsites else sites[0]


def smoke_test_wayback(log_func: Callable[[str], None]) -> int:
    """Run the smoke test. Returns 0 on success, non-zero on failure."""
    try:
        site = _pick_test_site()
    except Exception as e:
        log_func(f"FAIL: {e}")
        return 1

    site_url = site["url"].rstrip("/") + "/"
    log_func(f"Publishing test post on {site_url} (blog_id={site.get('blog_id')})")

    # Snapshot existing state so we only verify URLs OUR test added.
    queue_pre_urls = {e.get("url", "") for e in _read_queue()}
    log_pre = _read_container_file(_LOG_PATH)

    marker = f"onionpress-smoke-{int(time.time())}"
    title = f"Wayback smoke test {marker}"

    r = _wp(
        ["post", "create",
         "--post_title=" + title,
         "--post_content=Wayback archiving smoke test. Safe to delete.",
         "--post_status=publish",
         "--porcelain"],
        url=site_url, timeout=30,
    )
    if r.returncode != 0 or not r.stdout.strip().isdigit():
        log_func(f"FAIL: could not publish test post: {r.stderr or r.stdout}")
        return 1
    post_id = r.stdout.strip()
    log_func(f"Published post ID {post_id}")

    try:
        # save_post fired synchronously — grab the URLs it just queued.
        queue_post = _read_queue()
        new_entries = [e for e in queue_post
                       if e.get("url", "") not in queue_pre_urls]

        if not new_entries:
            log_func("FAIL: publish completed but no URLs were queued. "
                     "Is the wayback plugin loaded?")
            return 1

        non_onion = [e for e in new_entries if ".onion" not in e.get("url", "")]
        if non_onion:
            log_func("FAIL: queued URLs are not .onion form:")
            for e in non_onion:
                log_func(f"  bad: {e.get('url')}")
            return 1

        log_func(f"Queue gained {len(new_entries)} URL(s):")
        for e in new_entries:
            log_func(f"  queued: {e['url']}")

        # Drain manually — cron picks one URL per run. Add a safety margin
        # in case the queue also has pre-existing items we inherited.
        target_urls = [e["url"] for e in new_entries]
        drain_rounds = len(queue_post) + 1
        for _ in range(drain_rounds):
            _wp(["cron", "event", "run", "onionpress_drain_wayback_queue"],
                timeout=60)
            time.sleep(1)  # let plugin flush its log line
            remaining = {e.get("url", "") for e in _read_queue()}
            if not any(u in remaining for u in target_urls):
                break

        # Extract SPN job_ids that came back for each URL from the plugin log.
        log_post = _read_container_file(_LOG_PATH)
        new_log = log_post[len(log_pre):]

        url_to_job = {}
        missing_submissions = []
        for url in target_urls:
            # "Submitted <url> — HTTP 2xx — {...job_id":"<id>"...}"
            pattern = re.compile(
                r"Submitted " + re.escape(url)
                + r" — HTTP 2\d\d — .*\"job_id\":\"(spn2-[a-f0-9]+)\""
            )
            m = pattern.search(new_log)
            if m:
                url_to_job[url] = m.group(1)
            else:
                missing_submissions.append(url)

        if missing_submissions:
            log_func(f"FAIL: {len(missing_submissions)} URL(s) not successfully "
                     "submitted to SPN (no HTTP 2xx + job_id):")
            for url in missing_submissions:
                log_func(f"  missing ok-submission for: {url}")
            log_func("--- wayback.log (this run) ---")
            for line in new_log.splitlines()[-30:]:
                log_func("  " + line)
            return 1

        log_func(f"SPN accepted all {len(url_to_job)} submissions. "
                 "Polling /save/status/ for crawl outcomes…")

        # Poll SPN job status for each URL until terminal (success or error)
        # or timeout. Acceptance != archival — .onion crawls fail silently
        # about 30–50% of the time.
        results = {}  # url -> (status, detail)
        for url, job_id in url_to_job.items():
            status, detail = _poll_spn_job(job_id, log_func)
            results[url] = (status, detail)

        failed_crawls = [(u, d) for u, (s, d) in results.items() if s != "success"]
        if failed_crawls:
            log_func(f"FAIL: {len(failed_crawls)} URL(s) submitted but SPN crawl "
                     "did not succeed:")
            for url, detail in failed_crawls:
                log_func(f"  {url}: {detail}")
            log_func(f"Successful: {len(results) - len(failed_crawls)}/{len(results)}")
            return 1

        log_func(f"PASS: all {len(results)} URL(s) archived "
                 "(SPN status=success for every job_id)")
        return 0

    finally:
        # Always clean up the test post, even on failure.
        cleanup = _wp(["post", "delete", post_id, "--force"],
                      url=site_url, timeout=30)
        if cleanup.returncode == 0:
            log_func(f"Deleted test post ID {post_id}")
        else:
            log_func(f"WARNING: could not delete test post {post_id}: "
                     f"{cleanup.stderr or cleanup.stdout}")


def _poll_spn_job(job_id: str, log_func: Callable[[str], None]):
    """Poll SPN for job status until terminal or timeout.

    Returns (status, detail) where status is one of:
      "success"   — SPN captured the URL
      "error"     — SPN reported a terminal error (detail has status_ext)
      "timeout"   — polled until _SPN_POLL_TIMEOUT_SEC, still pending
      "unknown"   — couldn't read SPN's response (network error, malformed)
    """
    deadline = time.monotonic() + _SPN_POLL_TIMEOUT_SEC
    last_detail = "no response from SPN"
    while time.monotonic() < deadline:
        r = _docker_exec(
            ["curl", "-sL",
             "--socks5-hostname", "onionpress-tor:9050",
             "--max-time", "45",
             _SPN_STATUS_URL + job_id],
            timeout=60,
        )
        if r.returncode != 0 or not r.stdout.strip():
            last_detail = f"curl failed (rc={r.returncode})"
            time.sleep(_SPN_POLL_INTERVAL_SEC)
            continue
        try:
            payload = json.loads(r.stdout)
        except json.JSONDecodeError:
            last_detail = "SPN returned non-JSON"
            time.sleep(_SPN_POLL_INTERVAL_SEC)
            continue

        status = payload.get("status", "")
        if status == "success":
            ts = payload.get("timestamp", "")
            log_func(f"  {job_id}: success (timestamp {ts})")
            return "success", ts
        if status == "error":
            ext = payload.get("status_ext", "error")
            msg = payload.get("message", "")
            log_func(f"  {job_id}: error ({ext}) — {msg}")
            return "error", f"{ext}: {msg}"
        # pending / running — keep polling
        last_detail = f"status={status}"
        time.sleep(_SPN_POLL_INTERVAL_SEC)

    log_func(f"  {job_id}: timed out after {_SPN_POLL_TIMEOUT_SEC}s "
             f"(last status: {last_detail})")
    return "timeout", last_detail
