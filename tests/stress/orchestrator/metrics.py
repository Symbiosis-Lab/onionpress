"""Dashboard and worker info — SQLite-backed worker store via Docker volume."""

import json
import os
from typing import Iterator

from .config import StressConfig
from .phases import StressLogger

from onionpress.docker import Docker


# Schema for the shared worker-info DB (also created by workers on first write)
WORKER_DB_SCHEMA = """\
CREATE TABLE IF NOT EXISTS workers (
    global_index        INTEGER PRIMARY KEY,
    local_index         INTEGER NOT NULL,
    container           INTEGER NOT NULL,
    content_address     TEXT,
    healthcheck_address TEXT,
    content_port        INTEGER,
    hc_port             INTEGER,
    registered          INTEGER NOT NULL DEFAULT 0,
    privkey_b64         TEXT,
    pubkey_b64          TEXT,
    ctor_key_b64        TEXT DEFAULT '',
    arti_key_pem        TEXT DEFAULT '',
    error               TEXT
);
"""


def init_worker_db_volume(docker: Docker, config: StressConfig):
    """Create the Docker volume and initialize the DB schema inside it."""
    docker.run(["volume", "create", config.db_volume], timeout=10)

    # Initialize DB + schema via a throwaway container
    docker.run([
        "run", "--rm",
        "-v", f"{config.db_volume}:/worker-data",
        "alpine", "sh", "-c",
        "apk add --no-cache sqlite >/dev/null 2>&1 && "
        "sqlite3 /worker-data/worker-info.db "
        "'PRAGMA journal_mode=WAL; "
        "CREATE TABLE IF NOT EXISTS workers ("
        "global_index INTEGER PRIMARY KEY, local_index INTEGER NOT NULL, "
        "container INTEGER NOT NULL, content_address TEXT, healthcheck_address TEXT, "
        "content_port INTEGER, hc_port INTEGER, registered INTEGER NOT NULL DEFAULT 0, "
        "privkey_b64 TEXT, pubkey_b64 TEXT, ctor_key_b64 TEXT DEFAULT \"\", "
        "arti_key_pem TEXT DEFAULT \"\", error TEXT);'",
    ], timeout=30)


class WorkerInfoStore:
    """Read and query worker info from the shared SQLite DB in a Docker volume.

    All containers write to the DB via the shared volume.
    The orchestrator reads via a single `docker exec` call on a running container.
    """

    def __init__(self, docker: Docker, config: StressConfig):
        self.docker = docker
        self.config = config
        self.db_path = config.db_container_path
        self._workers: dict[int, dict] = {}  # global_index -> worker dict

    def _read_container(self) -> str:
        """Find a running stress container to exec into for reads."""
        for idx in range(self.config.num_containers):
            name = self.config.container_name(idx)
            if self.docker.container_running(name):
                return name
        return self.config.container_name(0)

    def refresh(self):
        """Load all worker data from the DB via one docker exec."""
        ctr = self._read_container()
        result = self.docker.exec(ctr, [
            "python3", "-c",
            "import sqlite3,json;"
            f"conn=sqlite3.connect('{self.db_path}',timeout=10);"
            "conn.row_factory=sqlite3.Row;"
            "rows=[dict(r) for r in conn.execute('SELECT * FROM workers').fetchall()];"
            "conn.close();"
            "print(json.dumps(rows))",
        ], timeout=15)
        if result.ok and result.output.strip():
            try:
                workers = json.loads(result.output)
                self._workers = {w["global_index"]: w for w in workers}
            except (json.JSONDecodeError, KeyError):
                pass

    def refresh_counts(self) -> tuple[int, int]:
        """Fast count query — returns (total, registered) without loading all data."""
        ctr = self._read_container()
        result = self.docker.exec(ctr, [
            "python3", "-c",
            "import sqlite3;"
            f"conn=sqlite3.connect('{self.db_path}',timeout=10);"
            "r=conn.execute('SELECT COUNT(*),COALESCE(SUM(registered),0) FROM workers').fetchone();"
            "conn.close();"
            "print(r[0],r[1])",
        ], timeout=15)
        if result.ok and result.output.strip():
            parts = result.output.strip().split()
            if len(parts) >= 2:
                try:
                    return int(parts[0]), int(parts[1])
                except ValueError:
                    pass
        return 0, 0

    def load_all(self, num_containers: int = 0):
        """Refresh from the DB."""
        self.refresh()

    def load_from_globs(self):
        """For cleanup — just refresh from the DB."""
        self.refresh()

    def get_worker(self, global_index: int) -> dict | None:
        """Get worker info by global index."""
        w = self._workers.get(global_index)
        if w:
            w = dict(w)
            w["registered"] = bool(w.get("registered"))
        return w

    def all_workers(self) -> Iterator[dict]:
        """Iterate over all workers."""
        for w in self._workers.values():
            d = dict(w)
            d["registered"] = bool(d.get("registered"))
            yield d

    def get_content_addrs(self, start: int, count: int) -> list[str]:
        """Get content addresses for a range of global indices."""
        addrs = []
        for i in range(start, start + count):
            w = self._workers.get(i)
            if w and w.get("content_address"):
                addrs.append(w["content_address"])
        return addrs

    def get_hc_addrs(self, start: int, count: int) -> list[str]:
        """Get healthcheck addresses for a range of global indices."""
        addrs = []
        for i in range(start, start + count):
            w = self._workers.get(i)
            if w and w.get("healthcheck_address"):
                addrs.append(w["healthcheck_address"])
        return addrs

    def total_registered(self) -> int:
        """Count total registered workers."""
        return sum(1 for w in self._workers.values() if w.get("registered"))


class Dashboard:
    """Query and display OnionHeaven metrics."""

    def __init__(self, config: StressConfig, docker: Docker, logger: StressLogger):
        self.config = config
        self.docker = docker
        self.logger = logger
        self._status_cache: dict | None = None
        self._status_ts: float = 0

    def query_status(self) -> dict:
        """Query OnionHeaven /status API over Tor (cached for 5s)."""
        import time
        now = time.time()
        if now - self._status_ts < 5 and self._status_cache:
            return self._status_cache

        result = self.docker.exec("onionpress-tor-client",
            f'curl -s --socks5-hostname "status:x@127.0.0.1:9050" --max-time 30 '
            f'"http://{self.config.onionheaven_addr}:8083/status"',
            timeout=35)
        result_text = result.output if result.ok else ""

        try:
            status = json.loads(result_text)
            if "total" in status:
                self._status_cache = status
                self._status_ts = now
                return status
        except (json.JSONDecodeError, ValueError):
            pass

        return {"total": 0, "healthy": 0, "failing": 0, "taken_over": 0}

    def get_container_mem_mb(self, container: str) -> int:
        """Get container memory usage in MB."""
        result = self.docker.run(
            ["stats", "--no-stream", "--format", "{{.MemUsage}}", container],
            timeout=10,
        )
        if not result.ok:
            return 0
        mem_str = result.output.split()[0] if result.output else "0"
        try:
            if "GiB" in mem_str:
                return int(float(mem_str.replace("GiB", "")) * 1024)
            elif "MiB" in mem_str:
                return int(float(mem_str.replace("MiB", "")))
            elif "KiB" in mem_str:
                return int(float(mem_str.replace("KiB", "")) / 1024)
        except ValueError:
            pass
        return 0

    def get_system_mem_pct(self) -> int:
        """Get VM memory usage percentage."""
        result = self.docker.exec("onionpress-tor",
            "awk '/MemAvailable/{a=$2} /MemTotal/{t=$2} END{if(t>0) printf \"%d\", (t-a)*100/t}' /proc/meminfo",
            timeout=10)
        try:
            return int(result.output.strip()) if result.ok else 0
        except ValueError:
            return 0

    def get_last_pass_duration(self) -> str:
        """Get last heartbeat pass duration from /status API."""
        status = self.query_status()
        return str(status.get("last_pass_duration", "-"))

    def get_onionheaven_version(self) -> str:
        """Get OnionHeaven server version from /status API."""
        status = self.query_status()
        return status.get("version", "pre-2.4.22")

    def print_dashboard(self):
        """Print the full dashboard."""
        status = self.query_status()
        tor_mem = self.get_container_mem_mb("onionpress-tor")
        wp_mem = self.get_container_mem_mb("onionpress-wordpress")
        mem_pct = self.get_system_mem_pct()
        pass_dur = self.get_last_pass_duration()

        reg_count = status.get("total", 0)
        healthy = status.get("online", 0)
        taken_over = status.get("taken_over", 0)
        hb_ok = status.get("heartbeat_healthy", 0)
        wp_bad = status.get("wordpress_unhealthy", 0)
        takeover_ctrs = status.get("takeover_containers", 0)

        # Count running stress containers
        result = self.docker.run(
            ["ps", "--filter", "name=stress-worker-", "--format", "{{.Names}}"],
            timeout=10,
        )
        stress_ctrs = len(result.output.strip().splitlines()) if result.ok and result.output.strip() else 0
        result = self.docker.run(
            ["ps", "--filter", "name=stress-poll-client-", "--format", "{{.Names}}"],
            timeout=10,
        )
        poll_ctrs = len(result.output.strip().splitlines()) if result.ok and result.output.strip() else 0

        fail_count = max(0, reg_count - healthy - taken_over)

        self.logger.log(f"Registry: {reg_count} entries | Tor mem: {tor_mem}MB | WP mem: {wp_mem}MB")
        print(f"           Online: {healthy} | Taken over: {taken_over} | Heartbeat: {hb_ok} ok / WP unhealthy: {wp_bad} | VM mem: {mem_pct}%")
        print(f"           Farm: {takeover_ctrs} takeover + {stress_ctrs} stress + {poll_ctrs} poll containers | Last pass: {pass_dur}s")

        self.logger.log_json(
            f'"registry_count":{reg_count},"tor_mem_mb":{tor_mem},"wp_mem_mb":{wp_mem},'
            f'"online":{healthy},"failing":{fail_count},"takeovers":{taken_over},'
            f'"heartbeat_healthy":{hb_ok},"wordpress_unhealthy":{wp_bad},'
            f'"vm_mem_pct":{mem_pct},"pass_duration":"{pass_dur}",'
            f'"takeover_containers":{takeover_ctrs},"stress_containers":{stress_ctrs}'
        )
