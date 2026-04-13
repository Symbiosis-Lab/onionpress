"""
OnionPress onionname registry — DNS for the onion web.

Runs on OnionHome only. Maps human-readable onionnames (e.g. "brewsterkahle")
to .onion addresses (e.g. "op2ijk...onion"). Case-insensitive uniqueness with
display case preserved.

Design notes:
- Atomic register — no reservation on /suggest or /check.
- Names are sticky forever; no liveness-based recycling. Explicit /release only.
- Reserved names come from two layers: a hardcoded set (this module) and a
  dynamic table populated from the onionpress.org WordPress page slugs.
- Schema designed so a /lookup can serve on every request without touching the
  network or WordPress.

Callers should open the DB via db_connect() and treat it as a short-lived
connection (no long-running transactions).
"""

import json
import os
import random
import re
import secrets
import sqlite3
import string
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

DATA_DIR = "/var/lib/onionhome"
DB_PATH = os.path.join(DATA_DIR, "onionnames.db")

MIN_NAME_LEN = 5
MAX_NAME_LEN = 40

# WordPress container hostname on the Docker network — used to refresh the
# dynamic reservations table from /wp-json. Short timeout: if WP is not up
# we skip this refresh and try again on the next tick.
WP_API_URL = "http://wordpress:80/wp-json/wp/v2/pages"
WP_API_TIMEOUT = 5

# How often to refresh dynamic reservations (seconds).
DYNAMIC_REFRESH_INTERVAL = 3600

# Canonical charset: a-z A-Z 0-9 . _ -
_NAME_ALLOWED_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]$")
_ALL_NUMERIC_RE = re.compile(r"^[0-9]+$")
_ONION_RE = re.compile(r"^[a-z2-7]{56}\.onion$")


# Hardcoded reserved names. Lowercased. Matching is case-insensitive.
# Keep this list narrow — anything we genuinely need to reserve. Add marketing
# site paths via the dynamic reservations table instead, so the list can grow
# without code changes.
HARDCODED_RESERVED = frozenset([
    # WordPress internals
    "wp-admin", "wp-content", "wp-login", "wp-config", "wp-includes",
    "wp-json", "wp-cron", "wp-signup", "wp-activate", "wp-trackback",
    "wp-mail", "wp-comments-post", "feed", "xmlrpc", "xmlrpc.php",
    # OnionPress paths
    "follow", "onionpress-status", "onionpress-settings",
    # Brand / network
    "onionpress", "onionhome", "onionheaven", "onioncellar",
    "tor", "torproject", "archive", "archiveorg",
    # Common handles that would be confusing
    "admin", "administrator", "root", "system", "staff", "moderator",
    "test", "null", "undefined", "anonymous", "user", "users",
    "api", "apis", "www", "mail", "ftp", "ssh", "help", "support",
    "about", "contact", "home", "index", "static", "assets",
    "public", "private", "security", "abuse", "legal", "terms",
    "privacy", "dmca", "login", "logout", "signin", "signup",
    "register", "settings", "account", "profile", "search",
])


# ---------------------------------------------------------------------------
# Logging — mirrors the style used by onionheaven_common.log()
# ---------------------------------------------------------------------------

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"[{ts}] onionnames: {msg}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# DB schema / connection
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS onionnames (
    name_lower    TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    onionaddress  TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    last_seen_at  TEXT
);
CREATE INDEX IF NOT EXISTS onionnames_by_address
    ON onionnames (onionaddress);

CREATE TABLE IF NOT EXISTS dynamic_reservations (
    name_lower   TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    refreshed_at TEXT NOT NULL
);
"""


def db_connect(path=DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def db_init(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_name(name):
    """Validate a candidate onionname. Returns (ok, reason).

    reason is None on success; otherwise a short machine-readable string
    ('too_short', 'too_long', 'invalid_chars', 'all_numeric').
    """
    if not isinstance(name, str) or not name:
        return False, "empty"
    if len(name) < MIN_NAME_LEN:
        return False, "too_short"
    if len(name) > MAX_NAME_LEN:
        return False, "too_long"
    if not _NAME_ALLOWED_RE.match(name):
        return False, "invalid_chars"
    if _ALL_NUMERIC_RE.match(name):
        return False, "all_numeric"
    return True, None


def is_hardcoded_reserved(name):
    return name.lower() in HARDCODED_RESERVED


def is_dynamic_reserved(conn, name):
    row = conn.execute(
        "SELECT 1 FROM dynamic_reservations WHERE name_lower = ?",
        (name.lower(),)
    ).fetchone()
    return row is not None


def is_reserved(conn, name):
    return is_hardcoded_reserved(name) or is_dynamic_reserved(conn, name)


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def check_name(conn, name):
    """Return a dict describing availability for `name`.

    Keys:
      available: bool
      reason:    one of 'invalid_chars', 'too_short', 'too_long',
                 'all_numeric', 'reserved', 'taken', or None.
    """
    ok, reason = validate_name(name)
    if not ok:
        return {"available": False, "reason": reason}
    if is_reserved(conn, name):
        return {"available": False, "reason": "reserved"}
    row = conn.execute(
        "SELECT name FROM onionnames WHERE name_lower = ?",
        (name.lower(),)
    ).fetchone()
    if row is not None:
        return {"available": False, "reason": "taken"}
    return {"available": True, "reason": None}


def lookup_name(conn, name):
    """Look up an onionname. Case-insensitive. Returns a dict or None."""
    if not isinstance(name, str) or not name:
        return None
    row = conn.execute(
        "SELECT name, onionaddress, registered_at, last_seen_at "
        "FROM onionnames WHERE name_lower = ?",
        (name.lower(),)
    ).fetchone()
    if row is None:
        return None
    return {
        "onionname": row["name"],
        "onionaddress": row["onionaddress"],
        "url": f"http://{row['onionaddress']}/{row['name']}",
        "registered_at": row["registered_at"],
        "last_seen_at": row["last_seen_at"],
    }


def register_name(conn, name, onionaddress):
    """Atomic register. Returns (ok, error_reason, alternatives).

    On success: (True, None, None) — the row was inserted.
    On failure: (False, reason, alternatives) where `reason` is one of
        'invalid_chars', 'too_short', 'too_long', 'all_numeric',
        'reserved', 'taken', 'invalid_address'
    and `alternatives` is a list of 3 candidate names (only for 'taken'/
    'reserved').
    """
    if not _ONION_RE.match(onionaddress or ""):
        return False, "invalid_address", None

    ok, reason = validate_name(name)
    if not ok:
        return False, reason, None

    if is_reserved(conn, name):
        return False, "reserved", generate_alternatives(conn, name)

    name_lower = name.lower()
    try:
        conn.execute(
            "INSERT INTO onionnames "
            "(name_lower, name, onionaddress, registered_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name_lower, name, onionaddress, _utcnow(), _utcnow())
        )
        conn.commit()
        return True, None, None
    except sqlite3.IntegrityError:
        return False, "taken", generate_alternatives(conn, name)


def release_name(conn, name, onionaddress):
    """Release `name` iff it's currently held by `onionaddress`.

    Returns (ok, reason). reason is None on success, else 'not_found' or
    'not_owner'.
    """
    row = conn.execute(
        "SELECT onionaddress FROM onionnames WHERE name_lower = ?",
        (name.lower(),)
    ).fetchone()
    if row is None:
        return False, "not_found"
    if row["onionaddress"] != onionaddress:
        return False, "not_owner"
    conn.execute(
        "DELETE FROM onionnames WHERE name_lower = ?",
        (name.lower(),)
    )
    conn.commit()
    return True, None


def touch_last_seen(conn, onionaddress):
    conn.execute(
        "UPDATE onionnames SET last_seen_at = ? WHERE onionaddress = ?",
        (_utcnow(), onionaddress)
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------

_WORDLIST_CACHE = {}
_WORDLIST_LOCK = threading.Lock()


def _load_wordlist(lang):
    """Import wordlists.<lang>; falls back to English."""
    with _WORDLIST_LOCK:
        if lang in _WORDLIST_CACHE:
            return _WORDLIST_CACHE[lang]
    try:
        from wordlists import SUPPORTED_LANGUAGES, get_wordlist
    except ImportError as e:
        log(f"wordlists package unavailable: {e}")
        return None, None
    try:
        adjectives, nouns = get_wordlist(lang)
    except Exception as e:
        log(f"failed to load wordlist for {lang!r}: {e}")
        return None, None
    with _WORDLIST_LOCK:
        _WORDLIST_CACHE[lang] = (adjectives, nouns)
    return adjectives, nouns


def suggest_name(conn, lang="en", max_attempts=20):
    """Return a random adjective-noun onionname that's currently available.

    Returns None if the language's wordlist is unavailable or every attempt
    hit a collision (shouldn't happen with millions of combinations, but
    bounded anyway).
    """
    adjectives, nouns = _load_wordlist(lang)
    if not adjectives or not nouns:
        return None
    rnd = random.SystemRandom()
    for _ in range(max_attempts):
        candidate = f"{rnd.choice(adjectives)}-{rnd.choice(nouns)}"
        # Suggestions must also pass validation (word lists curated but
        # defensive check in case of length overflow from long combos).
        ok, _ = validate_name(candidate)
        if not ok:
            continue
        result = check_name(conn, candidate)
        if result["available"]:
            return candidate
    return None


def generate_alternatives(conn, base, count=3, lang="en"):
    """Generate up to `count` available alternatives around `base`.

    Strategy:
      1. base-NNNN (random 4-digit suffix) — up to count//2
      2. base-<random-noun> — fills remaining slots
    Falls back to an empty list if nothing available.
    """
    alternatives = []
    seen = {base.lower()}
    rnd = random.SystemRandom()

    # Number-suffix attempts
    for _ in range(count * 4):
        if len(alternatives) >= max(1, count // 2):
            break
        suffix = f"{rnd.randint(0, 9999):04d}"
        candidate = f"{base}-{suffix}"
        if candidate.lower() in seen:
            continue
        seen.add(candidate.lower())
        ok, _ = validate_name(candidate)
        if not ok:
            continue
        if check_name(conn, candidate)["available"]:
            alternatives.append(candidate)

    # Noun-suffix attempts
    adjectives, nouns = _load_wordlist(lang)
    if nouns:
        for _ in range(count * 8):
            if len(alternatives) >= count:
                break
            suffix = rnd.choice(nouns)
            candidate = f"{base}-{suffix}"
            if candidate.lower() in seen:
                continue
            seen.add(candidate.lower())
            ok, _ = validate_name(candidate)
            if not ok:
                continue
            if check_name(conn, candidate)["available"]:
                alternatives.append(candidate)

    return alternatives


# ---------------------------------------------------------------------------
# Dynamic reservations — fetched from onionpress.org's WordPress pages
# ---------------------------------------------------------------------------

def _extract_slugs(payload):
    """Pull slug strings out of a wp-json pages payload."""
    slugs = set()
    if not isinstance(payload, list):
        return slugs
    for item in payload:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if isinstance(slug, str) and slug:
            slugs.add(slug.lower())
    return slugs


def fetch_wp_slugs(url=WP_API_URL, timeout=WP_API_TIMEOUT):
    """Fetch top-level page slugs from the WordPress REST API.

    Returns a set of lowercased slugs, or an empty set on error.
    """
    slugs = set()
    per_page = 100
    for page in range(1, 6):  # cap at 500 pages (5 * 100)
        q = f"?per_page={per_page}&page={page}&_fields=slug"
        try:
            req = urllib.request.Request(url + q, headers={
                "Accept": "application/json",
                "User-Agent": "onionnames-refresh/1",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            log(f"fetch_wp_slugs: {e}")
            break
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            break
        batch = _extract_slugs(data)
        if not batch:
            break
        slugs |= batch
        if len(batch) < per_page:
            break
    return slugs


def refresh_dynamic_reservations(conn, slugs=None):
    """Upsert `slugs` into dynamic_reservations; trim entries no longer present.

    If `slugs` is None, fetch from WordPress via fetch_wp_slugs().

    Returns the number of rows in the table after the refresh, or None if the
    fetch failed (no changes made).
    """
    if slugs is None:
        slugs = fetch_wp_slugs()
        if not slugs:
            return None

    now = _utcnow()
    slugs_lower = {s.lower() for s in slugs if isinstance(s, str) and s}

    with conn:
        conn.executemany(
            "INSERT INTO dynamic_reservations (name_lower, source, refreshed_at) "
            "VALUES (?, 'wp-pages', ?) "
            "ON CONFLICT(name_lower) DO UPDATE SET refreshed_at = excluded.refreshed_at",
            [(s, now) for s in slugs_lower]
        )
        # Prune slugs that are no longer in the WP response.
        placeholders = ",".join(["?"] * len(slugs_lower)) if slugs_lower else "''"
        params = tuple(slugs_lower) if slugs_lower else ()
        conn.execute(
            f"DELETE FROM dynamic_reservations "
            f"WHERE source = 'wp-pages' AND name_lower NOT IN ({placeholders})",
            params
        )

    count = conn.execute(
        "SELECT COUNT(*) FROM dynamic_reservations"
    ).fetchone()[0]
    return count


def start_refresh_thread(db_path=DB_PATH, interval=DYNAMIC_REFRESH_INTERVAL):
    """Start a daemon thread that refreshes dynamic reservations periodically.

    Returns the Thread object. First refresh happens after `interval` so
    startup isn't blocked — callers typically run a synchronous refresh once
    at boot before calling this.
    """
    def loop():
        while True:
            time.sleep(interval)
            try:
                conn = db_connect(db_path)
                try:
                    count = refresh_dynamic_reservations(conn)
                    if count is not None:
                        log(f"dynamic reservations refreshed: {count} entries")
                finally:
                    conn.close()
            except Exception as e:
                log(f"refresh thread error: {e}")

    t = threading.Thread(target=loop, name="onionnames-refresh", daemon=True)
    t.start()
    return t
