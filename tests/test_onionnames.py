"""Tests for onionnames — the OnionHome name registry.

Exercises validation, reserved-name logic, case-insensitive uniqueness,
register/lookup/release round-trip, suggestion + alternatives, dynamic
reservations, and the Ed25519 signing envelope.
"""

import hashlib
import json
import os
import sys
import tempfile
import unittest

# Make app/Resources/docker/tor/ importable so we can load onionnames and
# the wordlists package the same way the tor container does.
_TOR_DIR = os.path.join(
    os.path.dirname(__file__), "..", "app", "Resources", "docker", "tor"
)
sys.path.insert(0, _TOR_DIR)

import onion_auth  # noqa: E402
import onionnames  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_keypair(seed_bytes):
    """Derive a standard ed25519 expanded key + public key from a 32-byte seed."""
    h = hashlib.sha512(seed_bytes).digest()
    a_bytes = bytearray(h[:32])
    a_bytes[0] &= 248
    a_bytes[31] &= 127
    a_bytes[31] |= 64
    prefix = h[32:]
    expanded = bytes(a_bytes) + prefix
    a = int.from_bytes(a_bytes, "little")
    A = onion_auth._scalar_mult(a, onion_auth._B)
    pub = onion_auth._encode_point(A)
    return expanded, pub


def _make_identity(seed_byte):
    """Return (expanded_key, public_key, onionaddress) for a given seed byte."""
    expanded, pub = _make_keypair(bytes([seed_byte]) * 32)
    return expanded, pub, onion_auth.derive_onion_address(pub)


class _FreshDBMixin:
    """Open a fresh SQLite DB in a tmp path, initialized with the schema."""
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = self._tmp.name
        self.conn = onionnames.db_connect(self.db_path)
        onionnames.db_init(self.conn)

    def tearDown(self):
        try:
            self.conn.close()
        finally:
            os.unlink(self.db_path)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation(unittest.TestCase):

    def test_accepts_simple_lowercase(self):
        self.assertEqual(onionnames.validate_name("brewsterkahle"), (True, None))

    def test_accepts_mixed_case(self):
        self.assertEqual(onionnames.validate_name("BrewsterKahle"), (True, None))

    def test_accepts_allowed_punctuation_in_middle(self):
        for name in ("alice.bob", "alice_bob", "alice-bob", "a1.b2_c3-d4"):
            ok, reason = onionnames.validate_name(name)
            self.assertTrue(ok, f"{name} should be valid (got {reason})")

    def test_rejects_too_short(self):
        self.assertEqual(onionnames.validate_name("alex"), (False, "too_short"))
        self.assertEqual(onionnames.validate_name(""), (False, "empty"))

    def test_rejects_too_long(self):
        self.assertEqual(
            onionnames.validate_name("a" * 41), (False, "too_long")
        )

    def test_rejects_non_ascii(self):
        self.assertEqual(onionnames.validate_name("café1"), (False, "invalid_chars"))

    def test_rejects_spaces(self):
        self.assertEqual(
            onionnames.validate_name("hello world"), (False, "invalid_chars")
        )

    def test_rejects_at_sign(self):
        self.assertEqual(
            onionnames.validate_name("brewster@archive"),
            (False, "invalid_chars")
        )

    def test_rejects_leading_and_trailing_specials(self):
        for bad in (".alice", "alice.", "-alice", "alice-", "_alice", "alice_"):
            ok, reason = onionnames.validate_name(bad)
            self.assertFalse(ok, f"{bad!r} should be invalid")
            self.assertEqual(reason, "invalid_chars")

    def test_rejects_all_numeric(self):
        self.assertEqual(
            onionnames.validate_name("12345"), (False, "all_numeric")
        )


# ---------------------------------------------------------------------------
# Reserved names
# ---------------------------------------------------------------------------

class TestReserved(_FreshDBMixin, unittest.TestCase):

    def test_hardcoded_reserved_are_unavailable(self):
        # All names ≥ 5 chars so they pass length validation first.
        for name in ("wp-admin", "follow", "onionpress", "admin", "settings"):
            result = onionnames.check_name(self.conn, name)
            self.assertFalse(result["available"], f"{name} should be reserved")
            self.assertEqual(
                result["reason"], "reserved",
                f"{name} expected 'reserved', got {result['reason']}"
            )

    def test_hardcoded_reserved_case_insensitive(self):
        result = onionnames.check_name(self.conn, "WP-Admin")
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "reserved")

    def test_dynamic_reserved_blocks_registration(self):
        onionnames.refresh_dynamic_reservations(
            self.conn, slugs={"pricing", "download", "faq"}
        )
        result = onionnames.check_name(self.conn, "pricing")
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "reserved")

    def test_dynamic_refresh_prunes_removed_slugs(self):
        onionnames.refresh_dynamic_reservations(
            self.conn, slugs={"pricing", "faq"}
        )
        self.assertTrue(onionnames.is_dynamic_reserved(self.conn, "pricing"))
        onionnames.refresh_dynamic_reservations(
            self.conn, slugs={"faq"}
        )
        self.assertFalse(onionnames.is_dynamic_reserved(self.conn, "pricing"))
        self.assertTrue(onionnames.is_dynamic_reserved(self.conn, "faq"))

    def test_extract_slugs_ignores_junk(self):
        # _extract_slugs must tolerate a WP API response with extra fields
        # and reject entries without a slug string.
        payload = [
            {"slug": "one", "id": 1},
            {"slug": "Two"},      # mixed case — we lowercase it
            {"id": 99},            # no slug → ignored
            {"slug": 123},         # non-string slug → ignored
            "not-an-object",       # wrong type → ignored
        ]
        slugs = onionnames._extract_slugs(payload)
        self.assertEqual(slugs, {"one", "two"})


# ---------------------------------------------------------------------------
# Register / lookup / release round-trip
# ---------------------------------------------------------------------------

class TestRegistration(_FreshDBMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        _, _, self.addr1 = _make_identity(0x11)
        _, _, self.addr2 = _make_identity(0x22)

    def test_register_and_lookup(self):
        ok, reason, _ = onionnames.register_name(
            self.conn, "BrewsterKahle", self.addr1
        )
        self.assertTrue(ok)
        self.assertIsNone(reason)

        # Case preserved in display
        row = onionnames.lookup_name(self.conn, "BrewsterKahle")
        self.assertIsNotNone(row)
        self.assertEqual(row["onionname"], "BrewsterKahle")
        self.assertEqual(row["onionaddress"], self.addr1)
        self.assertEqual(row["url"], f"http://{self.addr1}/BrewsterKahle")

        # Lookup is case-insensitive
        self.assertEqual(
            onionnames.lookup_name(self.conn, "brewsterkahle")["onionname"],
            "BrewsterKahle",
        )
        self.assertEqual(
            onionnames.lookup_name(self.conn, "BREWSTERKAHLE")["onionname"],
            "BrewsterKahle",
        )

    def test_collision_is_case_insensitive(self):
        ok, _, _ = onionnames.register_name(self.conn, "alice1", self.addr1)
        self.assertTrue(ok)
        ok, reason, alts = onionnames.register_name(
            self.conn, "ALICE1", self.addr2
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "taken")
        self.assertIsInstance(alts, list)
        # Alternatives should not themselves collide or be invalid.
        for alt in alts:
            valid, _ = onionnames.validate_name(alt)
            self.assertTrue(valid, f"{alt} should be a valid alternative")
            self.assertNotEqual(alt.lower(), "alice1")

    def test_register_rejects_reserved(self):
        ok, reason, alts = onionnames.register_name(
            self.conn, "wp-admin", self.addr1
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "reserved")
        self.assertIsInstance(alts, list)

    def test_register_rejects_invalid_address(self):
        ok, reason, _ = onionnames.register_name(
            self.conn, "alice1", "not-a-real-onion"
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_address")

    def test_release_requires_owner_match(self):
        onionnames.register_name(self.conn, "alice1", self.addr1)
        ok, reason = onionnames.release_name(self.conn, "alice1", self.addr2)
        self.assertFalse(ok)
        self.assertEqual(reason, "not_owner")
        # Still present
        self.assertIsNotNone(onionnames.lookup_name(self.conn, "alice1"))

    def test_release_succeeds_then_frees_name(self):
        onionnames.register_name(self.conn, "alice1", self.addr1)
        ok, _ = onionnames.release_name(self.conn, "alice1", self.addr1)
        self.assertTrue(ok)
        self.assertIsNone(onionnames.lookup_name(self.conn, "alice1"))
        # Now a different address can claim the same name.
        ok, _, _ = onionnames.register_name(self.conn, "alice1", self.addr2)
        self.assertTrue(ok)

    def test_release_missing_returns_not_found(self):
        ok, reason = onionnames.release_name(self.conn, "ghost1", self.addr1)
        self.assertFalse(ok)
        self.assertEqual(reason, "not_found")


# ---------------------------------------------------------------------------
# Suggestions + alternatives
# ---------------------------------------------------------------------------

class TestSuggestions(_FreshDBMixin, unittest.TestCase):

    def test_suggest_produces_valid_available_name(self):
        name = onionnames.suggest_name(self.conn, lang="en")
        self.assertIsNotNone(name, "suggest_name should succeed with default wordlist")
        ok, _ = onionnames.validate_name(name)
        self.assertTrue(ok, f"{name} should be valid")
        self.assertIn("-", name)
        self.assertTrue(
            onionnames.check_name(self.conn, name)["available"]
        )

    def test_suggest_is_randomized(self):
        names = {onionnames.suggest_name(self.conn, "en") for _ in range(10)}
        # With 16M+ combinations, getting the same name 10 times in a row
        # is statistically impossible.
        self.assertGreater(len(names), 1)

    def test_suggest_falls_back_to_english(self):
        # 'xx' isn't registered; should fall back to English without error.
        name = onionnames.suggest_name(self.conn, lang="xx")
        self.assertIsNotNone(name)

    def test_alternatives_avoid_collision(self):
        _, _, addr = _make_identity(0x33)
        onionnames.register_name(self.conn, "alice1", addr)
        alts = onionnames.generate_alternatives(self.conn, "alice1", count=3)
        self.assertEqual(len(alts), 3)
        for alt in alts:
            self.assertTrue(
                onionnames.check_name(self.conn, alt)["available"],
                f"alternative {alt} should be available"
            )
            self.assertNotEqual(alt.lower(), "alice1")


# ---------------------------------------------------------------------------
# Ed25519 signing envelope — the proof-of-ownership path
# ---------------------------------------------------------------------------

class TestSigningEnvelope(unittest.TestCase):

    def test_sign_and_verify_roundtrip(self):
        expanded, pub = _make_keypair(b"\x07" * 32)
        addr = onion_auth.derive_onion_address(pub)
        ts = onion_auth.make_timestamp()
        sig = onion_auth.sign_name_payload(
            expanded, pub, "register", addr, "brewsterkahle", ts
        )
        ok, err = onion_auth.verify_name_payload(
            addr, "register", "brewsterkahle", ts, sig
        )
        self.assertTrue(ok, f"signature should verify (got {err!r})")

    def test_case_difference_still_verifies(self):
        # The signing helper lowercases the name before signing; a server
        # receiving the original-case name should still verify.
        expanded, pub = _make_keypair(b"\x08" * 32)
        addr = onion_auth.derive_onion_address(pub)
        ts = onion_auth.make_timestamp()
        sig = onion_auth.sign_name_payload(
            expanded, pub, "register", addr, "BrewsterKahle", ts
        )
        ok, _ = onion_auth.verify_name_payload(
            addr, "register", "BrewsterKahle", ts, sig
        )
        self.assertTrue(ok)

    def test_tamper_different_endpoint_rejected(self):
        expanded, pub = _make_keypair(b"\x09" * 32)
        addr = onion_auth.derive_onion_address(pub)
        ts = onion_auth.make_timestamp()
        sig = onion_auth.sign_name_payload(
            expanded, pub, "register", addr, "brewsterkahle", ts
        )
        # Using a "release" signature for a register request must fail.
        ok, err = onion_auth.verify_name_payload(
            addr, "release", "brewsterkahle", ts, sig
        )
        self.assertFalse(ok)
        self.assertIn("signature", err.lower())

    def test_tamper_different_name_rejected(self):
        expanded, pub = _make_keypair(b"\x0a" * 32)
        addr = onion_auth.derive_onion_address(pub)
        ts = onion_auth.make_timestamp()
        sig = onion_auth.sign_name_payload(
            expanded, pub, "register", addr, "brewsterkahle", ts
        )
        ok, _ = onion_auth.verify_name_payload(
            addr, "register", "someoneelse", ts, sig
        )
        self.assertFalse(ok)

    def test_tamper_different_address_rejected(self):
        expanded_a, pub_a = _make_keypair(b"\x0b" * 32)
        addr_a = onion_auth.derive_onion_address(pub_a)
        _, pub_b = _make_keypair(b"\x0c" * 32)
        addr_b = onion_auth.derive_onion_address(pub_b)

        ts = onion_auth.make_timestamp()
        sig = onion_auth.sign_name_payload(
            expanded_a, pub_a, "register", addr_a, "brewsterkahle", ts
        )
        # Signature was made with addr_a's key; claiming addr_b must fail
        # because we derive the public key from the address.
        ok, _ = onion_auth.verify_name_payload(
            addr_b, "register", "brewsterkahle", ts, sig
        )
        self.assertFalse(ok)

    def test_stale_timestamp_rejected(self):
        expanded, pub = _make_keypair(b"\x0d" * 32)
        addr = onion_auth.derive_onion_address(pub)
        stale = "2020-01-01T00:00:00Z"
        sig = onion_auth.sign_name_payload(
            expanded, pub, "register", addr, "brewsterkahle", stale
        )
        ok, err = onion_auth.verify_name_payload(
            addr, "register", "brewsterkahle", stale, sig
        )
        self.assertFalse(ok)
        self.assertIn("timestamp", err.lower())

    def test_cross_namespace_replay_rejected(self):
        # An OnionHeaven payload signature must not validate as a name-registry
        # signature, even if we contrive the fields to line up.
        expanded, pub = _make_keypair(b"\x0e" * 32)
        addr = onion_auth.derive_onion_address(pub)
        ts = onion_auth.make_timestamp()
        # Sign an OnionHeaven payload where the "healthcheck_address" slot
        # happens to be the onionname — still a different canonical string.
        oh_sig = onion_auth.sign_payload(
            expanded, pub, "register", addr, "brewsterkahle", ts
        )
        ok, _ = onion_auth.verify_name_payload(
            addr, "register", "brewsterkahle", ts, oh_sig
        )
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
