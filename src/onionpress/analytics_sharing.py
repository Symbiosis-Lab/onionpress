"""Opt-in analytics log sharing with OnionHome.

Background thread that periodically uploads completed (rolled) log files
to the OnionHome hub for remote debugging.  Only runs when the user has
set ``SHARE_ANALYTICS_WITH_ONIONHOME=yes`` in their config.
"""

import base64
import hashlib
import json
import os
import platform
import re
import subprocess
import threading
import time

# Valid log file name pattern
_LOG_NAME_RE = re.compile(
    r"^(onionpress|wordpress-access|wordpress-visitors|container-onionpress-tor|container-onionheaven|container-onionheaven-takeover-\d+|clearnet|launcher)-"
    r"\d{4}-\d{2}-\d{2}-\d{3}\.log$|^launcher\.log$"
)


def start_analytics_sharing(app):
    """Start the analytics sharing daemon thread.

    Called once from menubar.py after services are running.
    """
    thread = threading.Thread(target=_sharing_loop, args=(app,), daemon=True)
    thread.start()
    return thread


def _sharing_loop(app):
    """Sleep until this instance's designated hour, upload, repeat daily.

    If the instance was offline and missed its window, uploads on the
    next wake-up after a short delay (so Tor has time to reconnect).
    """
    from datetime import datetime, timezone, timedelta

    upload_hour = _pick_upload_hour(app)
    last_upload_date = None

    while True:
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")

        if last_upload_date == today:
            # Already uploaded today — sleep until tomorrow's window
            target = now.replace(hour=upload_hour, minute=0, second=0, microsecond=0)
            target += timedelta(days=1)
            time.sleep((target - now).total_seconds())
        elif now.hour >= upload_hour:
            # Missed or hit our window today — wait 30 minutes so the
            # instance isn't busy right after startup, and flaky
            # wake/sleep cycles don't spam OnionHome
            time.sleep(1800)
        else:
            # Haven't hit our window yet today — sleep until it
            target = now.replace(hour=upload_hour, minute=0, second=0, microsecond=0)
            time.sleep((target - now).total_seconds())

        if getattr(app, "_sleeping", False):
            continue

        try:
            enabled = app.read_config_value(
                "SHARE_ANALYTICS_WITH_ONIONHOME", "no"
            ).lower()
        except Exception:
            continue
        if enabled != "yes":
            continue

        try:
            _do_upload_cycle(app)
            last_upload_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        except Exception as e:
            app.log(f"Analytics sharing error: {e}")


def _pick_upload_hour(app):
    """Derive a stable hour (0-23) from the onion address for upload jitter."""
    addr = getattr(app, "onion_address", "") or ""
    if addr:
        h = hashlib.sha256(addr.encode()).digest()
        return h[0] % 24
    # Fallback: random hour per process
    import random
    return random.randint(0, 23)


def _do_upload_cycle(app, include_active=False):
    """Collect completed logs, send manifest, upload wanted files.

    When *include_active* is True (manual upload), current/active log files
    are included alongside completed ones.  OnionHome re-requests a file if
    the offered size is larger than what it already has.
    """
    # Collect completed files from all rotating logs
    all_files = []
    log_instances = [
        getattr(app, "_onionpress_log", None),
        getattr(app, "_wp_access_log", None),
        getattr(app, "_wp_visitors_log", None),
        getattr(app, "_tor_log", None),
        getattr(app, "_onionheaven_log", None),
        getattr(app, "_clearnet_log", None),
    ]
    # Include any takeover container logs
    for _name, (_proc, _thread) in getattr(app, "_container_log_processes", {}).items():
        pass  # Takeover logs are discovered dynamically below

    # Scan logs dir for all container-* rotating logs (catches takeover workers)
    import glob as _glob
    logs_dir = os.path.join(getattr(app, "app_support", ""), "logs")
    for log_inst in log_instances:
        if log_inst is not None:
            all_files.extend(log_inst.completed_files())
            if include_active:
                # Include the current active file too
                path = log_inst.current_path()
                if os.path.exists(path):
                    try:
                        size = os.path.getsize(path)
                        if size > 0:
                            name = os.path.basename(path)
                            # Avoid duplicates
                            if not any(f["name"] == name for f in all_files):
                                all_files.append({"name": name, "size": size, "path": path})
                    except OSError:
                        pass

    # Also include launcher.log (not a rotating log, just a flat file)
    launcher_log = os.path.join(getattr(app, "app_support", ""), "launcher.log")
    if os.path.exists(launcher_log):
        try:
            size = os.path.getsize(launcher_log)
            if size > 0:
                all_files.append({
                    "name": "launcher.log",
                    "size": size,
                    "path": launcher_log,
                })
        except OSError:
            pass

    if not all_files:
        return

    # Limit to most recent files, capped at 1MB total
    all_files.sort(key=lambda f: f["name"], reverse=True)  # newest first by date in name
    capped = []
    total_size = 0
    for f in all_files:
        if total_size + f["size"] > 1_048_576:
            break
        capped.append(f)
        total_size += f["size"]
    all_files = capped

    if not all_files:
        return

    content_addr = getattr(app, "onion_address", None)
    hc_addr = getattr(app, "healthcheck_address", None)
    if not content_addr or not hc_addr:
        return

    # Read OnionHome address from config
    onionhome_addr = app.read_config_value(
        "ONIONHOME_ADDRESS",
        "op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion",
    )
    if not onionhome_addr:
        return

    # Sign the manifest
    try:
        import key_manager
        import onion_auth

        secret_key_bytes, public_key_raw = key_manager.extract_keys()
        timestamp = onion_auth.make_timestamp()
        signature = onion_auth.sign_payload(
            secret_key_bytes, public_key_raw,
            "logs", content_addr, hc_addr, timestamp,
        )
    except Exception as e:
        app.log(f"Analytics sharing: sign error: {e}")
        return

    manifest = {
        "content_address": content_addr,
        "healthcheck_address": hc_addr,
        "version": getattr(app, "version", "unknown"),
        "tor_impl": app.read_config_value("TOR_IMPL", "unknown"),
        "os_version": platform.mac_ver()[0] or "unknown",
        "files": [{"name": f["name"], "size": f["size"]} for f in all_files],
        "timestamp": timestamp,
        "signature": signature,
    }

    # Build a name→path map for quick lookup
    file_map = {f["name"]: f["path"] for f in all_files}

    api_port = 8083
    base_url = f"http://{onionhome_addr}:{api_port}"

    # Step 1: POST manifest
    wanted = _post_json(app, f"{base_url}/logs/manifest", manifest)
    if wanted is None:
        return
    wanted_names = wanted.get("wanted", [])
    if not wanted_names:
        return

    app.log(f"Analytics sharing: OnionHome wants {len(wanted_names)} file(s)")

    # Step 2: Upload each wanted file
    import onion_auth as _oa

    for name in wanted_names:
        if not _LOG_NAME_RE.match(name):
            continue
        path = file_map.get(name)
        if not path or not os.path.exists(path):
            continue

        try:
            with open(path, "rb") as f:
                content_b64 = base64.b64encode(f.read()).decode("ascii")
        except OSError:
            continue

        ts2 = _oa.make_timestamp()
        sig2 = _oa.sign_payload(
            secret_key_bytes, public_key_raw,
            "logs", content_addr, hc_addr, ts2,
        )
        upload_payload = {
            "content_address": content_addr,
            "healthcheck_address": hc_addr,
            "file_name": name,
            "file_content": content_b64,
            "timestamp": ts2,
            "signature": sig2,
        }
        resp = _post_json(app, f"{base_url}/logs/upload", upload_payload)
        if not resp or not resp.get("stored"):
            # Retry once after 10s
            time.sleep(10)
            resp = _post_json(app, f"{base_url}/logs/upload", upload_payload)
        if resp and resp.get("stored"):
            app.log(f"Analytics sharing: uploaded {name}")


def _post_json(app, url, payload_dict):
    """POST JSON to *url* via docker exec curl through Tor SOCKS.

    Uses stdin piping to avoid ARG_MAX limits on large payloads.
    Returns parsed JSON response or None on failure.
    """
    # Import docker helpers from onionheaven (same pattern)
    import onionheaven

    docker_bin = onionheaven._docker_bin(app)
    docker_env = onionheaven._docker_env(app)

    payload_json = json.dumps(payload_dict)

    cmd = [
        docker_bin, "exec", "-i", "onionpress-tor",
        "curl", "-s", "-X", "POST",
        "--socks5-hostname", "127.0.0.1:9050",
        "-H", "Content-Type: application/json",
        "-d", "@-",  # read payload from stdin
        "--max-time", "120",
        url,
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            env=docker_env,
        )
        stdout, stderr = proc.communicate(input=payload_json, timeout=150)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        app.log("Analytics sharing: curl timed out")
        return None
    except Exception as e:
        app.log(f"Analytics sharing: curl error: {e}")
        return None

    if proc.returncode != 0:
        app.log(f"Analytics sharing: curl failed rc={proc.returncode}")
        return None

    try:
        return json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        app.log(f"Analytics sharing: bad JSON response: {stdout[:200]}")
        return None
