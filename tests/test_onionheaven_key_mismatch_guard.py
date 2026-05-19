#!/usr/bin/env python3
"""Tests for the key-vs-address mismatch guards in OnionHeaven.

The /online endpoint pre-commit 542cb61b accepted any PEM matching basic
structure and unconditionally wrote it to KEYS_DIR/<content_address>/. A
buggy client could (and did) post /online with content_address=A but
arti_key_pem=B's_key, silently replacing A's on-disk key with B's. Stale
residue files survived the input fix; every reconcile pass fed the wrong
key into ADD_ONION and Tor registered the service under the derived
address (B) rather than the expected slot (A), driving the RECONCILE
loop forever.

Two guards pin this down:
  1. key-convert.py pem-to-onion-address — pure derivation, used as
     ground truth by the shell-side guards.
  2. queue-manager.py TorCommandConn.add_onion — verifies Tor's returned
     ServiceID matches the address the caller asked for, rolls back the
     ADD on mismatch.

The shell tor-manager.sh guards are exercised indirectly via the same
derivation logic; this file targets the Python pieces that have unit
test infrastructure.
"""

import base64
import hashlib
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
DOCKER_TOR_DIR = os.path.join(PROJECT_DIR, "app", "Resources", "docker", "tor")
SRC_DIR = os.path.join(PROJECT_DIR, "src")
sys.path.insert(0, DOCKER_TOR_DIR)
sys.path.insert(0, SRC_DIR)

from onionpress import key_manager  # noqa: E402

KEY_CONVERT = os.path.join(DOCKER_TOR_DIR, "key-convert.py")


def _synthetic_keypair(seed):
    """Build a deterministic (expanded_64, public_32, onion_addr) triple.

    The private bytes are not a real ed25519 expansion of `public`, but
    the derivation we're testing only uses the public half.
    """
    expanded = bytes((seed + i) & 0xFF for i in range(64))
    public = bytes(((seed * 7) + i) & 0xFF for i in range(32))
    checksum = hashlib.sha3_256(b".onion checksum" + public + b"\x03").digest()[:2]
    addr = base64.b32encode(public + checksum + b"\x03").decode().lower()
    return expanded, public, addr


def _write_pem(tmpdir, name, expanded, public):
    pem = key_manager.build_openssh_key(expanded, public)
    path = os.path.join(tmpdir, name)
    with open(path, "wb") as f:
        f.write(pem)
    return path


class TestPemToOnionAddress(unittest.TestCase):
    """The shell-side guards rely on key-convert.py pem-to-onion-address
    producing the exact base32 v3 address. Pin the derivation."""

    def test_matches_locally_computed_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            expanded, public, expected = _synthetic_keypair(seed=42)
            path = _write_pem(tmp, "ks.pem", expanded, public)

            result = subprocess.run(
                ["python3", KEY_CONVERT, "pem-to-onion-address", path],
                capture_output=True, text=True, timeout=10
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertEqual(result.stdout.strip(), expected)

    def test_different_keys_produce_different_addresses(self):
        with tempfile.TemporaryDirectory() as tmp:
            e1, p1, addr1 = _synthetic_keypair(seed=1)
            e2, p2, addr2 = _synthetic_keypair(seed=2)
            self.assertNotEqual(addr1, addr2)
            f1 = _write_pem(tmp, "k1.pem", e1, p1)
            f2 = _write_pem(tmp, "k2.pem", e2, p2)

            def derive(path):
                r = subprocess.run(
                    ["python3", KEY_CONVERT, "pem-to-onion-address", path],
                    capture_output=True, text=True, timeout=10,
                )
                return r.stdout.strip()

            self.assertEqual(derive(f1), addr1)
            self.assertEqual(derive(f2), addr2)


class TestAddOnionGuard(unittest.TestCase):
    """add_onion must roll back the ADD and return key_mismatch when
    Tor's ServiceID doesn't match the address the caller asked for."""

    def setUp(self):
        # Import lazily so the path insertion above takes effect first.
        # The module imports `socket` at top level; we don't connect.
        import importlib
        # The file uses a hyphen which isn't a valid Python identifier, so
        # load it as a generic module.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "queue_manager",
            os.path.join(DOCKER_TOR_DIR, "onionheaven-queue-manager.py"),
        )
        self.qm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.qm)

    def _make_conn(self, responses):
        """Build a TorCommandConn whose _send/_read_response is mocked
        to deliver `responses` (a list of strings) in order."""
        conn = self.qm.TorCommandConn()
        conn.sock = mock.Mock()
        conn._send = mock.Mock()
        conn._read_response = mock.Mock(side_effect=list(responses))
        return conn

    def test_match_returns_success(self):
        addr = "op2ijk3cvd7kswainvwlg7uqxuoghaxzns6quht2csz3cdp5sgr2lnqd.onion"
        sid = addr.replace(".onion", "")
        conn = self._make_conn([
            f"250-ServiceID={sid}\r\n250 OK\r\n",
        ])
        ok, result = conn.add_onion(addr, "AAAAAA==")
        self.assertTrue(ok)
        self.assertEqual(result, sid)

    def test_mismatch_rolls_back_and_returns_typed_error(self):
        expected_addr = "op2hecfkpduw2iou5aiul3tbg57tnkxqgwgq4uht4vyedanvbcv2wwqd.onion"
        derived_sid = "op2ijk3cvd7kswainvwlg7uqxuoghaxzns6quht2csz3cdp5sgr2lnqd"
        # First response: ADD_ONION returns the *wrong* ServiceID.
        # Second response: DEL_ONION acknowledged.
        conn = self._make_conn([
            f"250-ServiceID={derived_sid}\r\n250 OK\r\n",
            "250 OK\r\n",
        ])
        ok, result = conn.add_onion(expected_addr, "AAAAAA==")
        self.assertFalse(ok)
        self.assertIn("key_mismatch", result)
        self.assertIn(f"expected={expected_addr.replace('.onion','')}", result)
        self.assertIn(f"actual={derived_sid}", result)

        # The guard must have rolled back the orphaned service.
        sent_commands = [call.args[0] for call in conn._send.call_args_list]
        self.assertTrue(
            any(cmd.startswith(f"DEL_ONION {derived_sid}") for cmd in sent_commands),
            f"Expected DEL_ONION {derived_sid} rollback, got: {sent_commands}",
        )

    def test_collision_branch_unaffected(self):
        addr = "op2ijk3cvd7kswainvwlg7uqxuoghaxzns6quht2csz3cdp5sgr2lnqd.onion"
        # Collision path doesn't echo a ServiceID — the guard should
        # leave its existing has_onion-based handling alone.
        conn = self._make_conn([
            "550 Onion address collision\r\n",
            # has_onion polls onions/detached then onions/current.
            f"250+onions/detached=\r\n{addr.replace('.onion','')}\r\n.\r\n250 OK\r\n",
        ])
        ok, result = conn.add_onion(addr, "AAAAAA==")
        self.assertTrue(ok)
        self.assertEqual(result, addr.replace(".onion", ""))


if __name__ == "__main__":
    unittest.main()
