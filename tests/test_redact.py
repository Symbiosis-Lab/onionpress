"""Tests for src/onionpress/redact.py."""

import ipaddress
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress import redact


class TestQueryParamScrubbing(unittest.TestCase):
    def test_sensitive_params_redacted(self):
        cases = [
            ("/wp-login.php?action=rp&key=abc123", "key=<redacted>"),
            ("/api?token=eyJhbGciOi&foo=bar", "token=<redacted>"),
            ("/wp-admin/admin-ajax.php?_wpnonce=xxx", "_wpnonce=<redacted>"),
            ("/oauth?access_token=abc&refresh_token=def",
             "access_token=<redacted>"),
        ]
        for inp, expected_fragment in cases:
            out = redact.redact_text(inp, salt=b"", scrub_ips=False)
            self.assertIn(expected_fragment, out,
                          f"{inp!r} → {out!r} missing {expected_fragment!r}")
            self.assertNotIn("abc123", out)

    def test_case_insensitive(self):
        out = redact.redact_text("/?TOKEN=abc&Key=def", salt=b"", scrub_ips=False)
        self.assertIn("TOKEN=<redacted>", out)
        self.assertIn("Key=<redacted>", out)

    def test_non_sensitive_params_preserved(self):
        inp = "/?page_id=42&category=news"
        out = redact.redact_text(inp, salt=b"", scrub_ips=False)
        self.assertEqual(inp, out)


class TestHeaderScrubbing(unittest.TestCase):
    def test_cookie_redacted(self):
        out = redact.redact_text(
            "Cookie: session=abc123; other=xyz", salt=b"", scrub_ips=False,
        )
        self.assertEqual(out, "Cookie: <redacted>")

    def test_authorization_redacted(self):
        out = redact.redact_text(
            "Authorization: Bearer eyJhbGciOi", salt=b"", scrub_ips=False,
        )
        self.assertEqual(out, "Authorization: <redacted>")

    def test_non_auth_headers_preserved(self):
        inp = "Content-Type: application/json"
        out = redact.redact_text(inp, salt=b"", scrub_ips=False)
        self.assertEqual(inp, out)


class TestIPPseudonymization(unittest.TestCase):
    def test_ipv4_pseudonymized(self):
        salt = b"\x00" * 32
        out = redact.redact_text("1.2.3.4 - visitor", salt, scrub_ips=True)
        self.assertNotIn("1.2.3.4", out)
        # Output contains an fd00::/8 address in place of the real one
        addr = out.split(" ")[0]
        ip = ipaddress.IPv6Address(addr)
        self.assertTrue(ip.is_private)

    def test_ipv6_pseudonymized(self):
        salt = b"\x00" * 32
        out = redact.redact_text(
            "2001:db8::1 client seen", salt, scrub_ips=True,
        )
        self.assertNotIn("2001:db8::1", out)

    def test_same_ip_same_salt_stable(self):
        salt = b"x" * 32
        a = redact.pseudonymize_ip("203.0.113.7", salt)
        b = redact.pseudonymize_ip("203.0.113.7", salt)
        self.assertEqual(a, b)

    def test_different_salts_differ(self):
        a = redact.pseudonymize_ip("203.0.113.7", b"a" * 32)
        b = redact.pseudonymize_ip("203.0.113.7", b"b" * 32)
        self.assertNotEqual(a, b)

    def test_pseudonym_in_private_range(self):
        pseudo = redact.pseudonymize_ip("203.0.113.7", b"salt" * 8)
        ip = ipaddress.IPv6Address(pseudo)
        self.assertTrue(ip.is_private)
        # fd00::/8 means the first byte is 0xfd; the first 16-bit group
        # can be any value in fd00-fdff. The underlying bytes must begin
        # with 0xfd.
        self.assertEqual(ip.packed[0], 0xfd)

    def test_scrub_ips_disabled(self):
        out = redact.redact_text("1.2.3.4", salt=b"", scrub_ips=False)
        self.assertEqual(out, "1.2.3.4")

    def test_version_strings_not_matched(self):
        # Bounded IPv4 regex shouldn't eat leading tokens of version
        # strings like "python/3.9.1" or "1.2.3.4-rc5".
        inp = "python/3.9.1 1.2.3.4-rc5"
        out = redact.redact_text(inp, salt=b"salt" * 8, scrub_ips=True)
        # "1.2.3.4-rc5" → should NOT be treated as bare IP (trailing -)
        self.assertIn("1.2.3.4-rc5", out)


class TestSaltManagement(unittest.TestCase):
    def test_salt_persists_within_day(self):
        with tempfile.TemporaryDirectory() as td:
            a = redact.get_daily_salt(td, day="2026-04-21")
            b = redact.get_daily_salt(td, day="2026-04-21")
            self.assertEqual(a, b)
            self.assertEqual(len(a), 32)

    def test_salt_changes_across_days(self):
        with tempfile.TemporaryDirectory() as td:
            a = redact.get_daily_salt(td, day="2026-04-21")
            b = redact.get_daily_salt(td, day="2026-04-22")
            self.assertNotEqual(a, b)

    def test_salt_file_is_private(self):
        with tempfile.TemporaryDirectory() as td:
            redact.get_daily_salt(td, day="2026-04-21")
            path = os.path.join(td, redact.SALT_DIR, "salt-2026-04-21")
            mode = os.stat(path).st_mode & 0o777
            self.assertEqual(mode, 0o600)


class TestScrubFn(unittest.TestCase):
    def test_scrub_fn_applies_all(self):
        with tempfile.TemporaryDirectory() as td:
            scrub = redact.make_scrub_fn(td, scrub_ips=True)
            raw = b"1.2.3.4 - - GET /?token=abc Cookie: s=xxx"
            out = scrub(raw)
            self.assertNotIn(b"1.2.3.4", out)
            self.assertIn(b"token=<redacted>", out)
            self.assertIn(b"Cookie: <redacted>", out)

    def test_scrub_fn_ip_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            scrub = redact.make_scrub_fn(td, scrub_ips=False)
            out = scrub(b"1.2.3.4 - /?token=abc")
            self.assertIn(b"1.2.3.4", out)
            self.assertIn(b"token=<redacted>", out)


if __name__ == "__main__":
    unittest.main()
