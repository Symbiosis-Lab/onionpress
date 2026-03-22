#!/usr/bin/env python3
"""Backward-compatible wrapper for the Python stress test orchestrator.

Usage:
    python3 tests/onionheaven-stress-test.py --total 5
    python3 -m tests.stress.orchestrator --total 5   # equivalent
"""

import os
import sys

# Ensure src/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from stress.orchestrator.__main__ import main

if __name__ == "__main__":
    main()
