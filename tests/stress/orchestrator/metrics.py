"""Dashboard and worker info — replaces repeated python3 -c subprocess calls."""

import glob
import json
import os
from typing import Iterator

from .config import StressConfig
from .phases import StressLogger

from onionpress.docker import Docker


class WorkerInfoStore:
    """Load and query worker-info.json files.

    Replaces the bash pattern of calling python3 -c "import json..."
    once per site to extract addresses.
    """

    def __init__(self, output_dir: str, per_ctr: int):
        self.output_dir = output_dir
        self.per_ctr = per_ctr
        self._workers: dict[int, list[dict]] = {}  # ctr_idx -> worker list

    def load_all(self, num_containers: int):
        """Load worker info from all container info files."""
        self._workers.clear()
        for idx in range(num_containers):
            path = os.path.join(self.output_dir, f"worker-{idx}-info.json")
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        self._workers[idx] = json.load(f)
                except (json.JSONDecodeError, OSError):
                    self._workers[idx] = []

    def load_from_globs(self):
        """Load from all worker-*-info.json files (for cleanup)."""
        self._workers.clear()
        patterns = [
            os.path.join(self.output_dir, "worker-*-info.json"),
            os.path.join(self.output_dir, "run-*", "worker-*-info.json"),
        ]
        seen_addrs: set[str] = set()
        idx = 0
        for pattern in patterns:
            for path in sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True):
                try:
                    with open(path) as f:
                        workers = json.load(f)
                    # Dedup by content address
                    deduped = []
                    for w in workers:
                        ca = w.get("content_address", "")
                        if ca and ca not in seen_addrs:
                            seen_addrs.add(ca)
                            deduped.append(w)
                    if deduped:
                        self._workers[idx] = deduped
                        idx += 1
                except (json.JSONDecodeError, OSError):
                    pass

    def get_worker(self, global_index: int) -> dict | None:
        """Get worker info by global index."""
        ctr_idx = global_index // self.per_ctr
        local_idx = global_index % self.per_ctr
        workers = self._workers.get(ctr_idx, [])
        for w in workers:
            if w.get("local_index") == local_idx:
                return w
        return None

    def all_workers(self) -> Iterator[dict]:
        """Iterate over all workers across all containers."""
        for workers in self._workers.values():
            yield from workers

    def get_content_addrs(self, start: int, count: int) -> list[str]:
        """Get content addresses for a range of global indices."""
        addrs = []
        for i in range(start, start + count):
            w = self.get_worker(i)
            if w and w.get("content_address"):
                addrs.append(w["content_address"])
        return addrs

    def get_hc_addrs(self, start: int, count: int) -> list[str]:
        """Get healthcheck addresses for a range of global indices."""
        addrs = []
        for i in range(start, start + count):
            w = self.get_worker(i)
            if w and w.get("healthcheck_address"):
                addrs.append(w["healthcheck_address"])
        return addrs

    def total_registered(self) -> int:
        """Count total registered workers."""
        return sum(
            1 for w in self.all_workers() if w.get("registered")
        )


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
