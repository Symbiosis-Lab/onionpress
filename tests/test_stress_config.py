"""Unit tests for stress test configuration."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from stress.orchestrator.config import StressConfig


class TestStressConfig:
    """Tests for StressConfig."""

    def test_defaults(self):
        cfg = StressConfig()
        assert cfg.total == 5
        assert cfg.healthy == 3
        assert cfg.failing == 2
        assert cfg.per_ctr == 20
        assert cfg.num_containers == 1
        assert cfg.num_poll_clients == 1
        assert cfg.mode == "worker"
        assert cfg.no_healthcheck is False
        assert cfg.base_port == 9100

    def test_auto_split_even(self):
        cfg = StressConfig(total=10)
        assert cfg.healthy == 5
        assert cfg.failing == 5

    def test_auto_split_odd(self):
        cfg = StressConfig(total=7)
        assert cfg.failing == 3
        assert cfg.healthy == 4

    def test_explicit_failing(self):
        cfg = StressConfig(total=10, failing=3)
        cfg._failing_set = True
        cfg.__post_init__()
        assert cfg.failing == 3
        assert cfg.healthy == 7

    def test_explicit_healthy(self):
        cfg = StressConfig(total=10, healthy=8)
        cfg._healthy_set = True
        cfg.__post_init__()
        assert cfg.healthy == 8
        assert cfg.failing == 2

    def test_num_containers_exact(self):
        cfg = StressConfig(total=20, per_ctr=20)
        assert cfg.num_containers == 1

    def test_num_containers_partial(self):
        cfg = StressConfig(total=21, per_ctr=20)
        assert cfg.num_containers == 2

    def test_num_containers_large(self):
        cfg = StressConfig(total=100, per_ctr=50)
        assert cfg.num_containers == 2

    def test_poll_clients_clamped_min(self):
        cfg = StressConfig(total=2)
        assert cfg.num_poll_clients == 1  # minimum

    def test_poll_clients_clamped_max(self):
        cfg = StressConfig(total=1000)
        assert cfg.num_poll_clients == 20  # maximum

    def test_poll_clients_scaling(self):
        cfg = StressConfig(total=100)
        # (100/2 + 2) / 3 = 17
        assert cfg.num_poll_clients == 17

    def test_timeouts_default(self):
        cfg = StressConfig()
        assert cfg.bootstrap_timeout == 900
        assert cfg.takeover_timeout == 600

    def test_timeouts_no_timeout(self):
        cfg = StressConfig(no_timeout=True)
        assert cfg.bootstrap_timeout == 86400
        assert cfg.takeover_timeout == 86400

    def test_workers_in_container_last(self):
        cfg = StressConfig(total=25, per_ctr=20)
        assert cfg.workers_in_container(0) == 20
        assert cfg.workers_in_container(1) == 5

    def test_workers_in_container_exact(self):
        cfg = StressConfig(total=20, per_ctr=20)
        assert cfg.workers_in_container(0) == 20

    def test_container_name(self):
        cfg = StressConfig()
        assert cfg.container_name(0) == "stress-worker-0"
        assert cfg.container_name(5) == "stress-worker-5"

    def test_poll_client_name(self):
        cfg = StressConfig()
        assert cfg.poll_client_name(0) == "stress-poll-client-0"

    def test_fail_start(self):
        cfg = StressConfig(total=10)
        assert cfg.fail_start == 5  # total - failing

    def test_stress_version_format(self):
        cfg = StressConfig()
        assert cfg.stress_version.startswith("stress-test-")
        assert str(os.getpid()) in cfg.stress_version

    def test_from_args_basic(self):
        cfg = StressConfig.from_args(["--total", "20", "--per-ctr", "10"])
        assert cfg.total == 20
        assert cfg.per_ctr == 10
        assert cfg.num_containers == 2

    def test_from_args_cleanup(self):
        cfg = StressConfig.from_args(["--cleanup"])
        assert cfg.cleanup is True

    def test_from_args_no_timeout(self):
        cfg = StressConfig.from_args(["--no-timeout"])
        assert cfg.no_timeout is True
        assert cfg.bootstrap_timeout == 86400

    def test_from_args_mode(self):
        cfg = StressConfig.from_args(["--mode", "coordinator"])
        assert cfg.mode == "coordinator"
