"""OnionHeaven stress test orchestrator.

Replaces tests/onionheaven-stress-test.sh with a testable Python package.
Container-side scripts (verify-worker.py, worker-bootstrap.py, worker-server.py)
stay as-is.

Usage:
    python3 -m tests.stress.orchestrator --total 5
    python3 -m tests.stress.orchestrator --cleanup
    python3 -m tests.stress.orchestrator --mode coordinator
"""
