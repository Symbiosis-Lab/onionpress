#!/usr/bin/env python3
"""
Convert tweets.jsonl (from fetch.py) into a ZIP that mimics Twitter's
official archive, so the existing OnionPress Twitter importer plugin
(onionpress-social-archive-twitter.php) accepts it as-is.

Usage:
    python to_archive.py <handle>                     # → output/<handle>/<handle>-fetch.zip
    python to_archive.py <handle> -o /tmp/out.zip
    python to_archive.py <handle> --account-id 12345  # override auto-detection

Importing this ZIP and the official Twitter archive in either order is
safe — the importer dedups on `_source_id = "twitter:<id_str>"`, so
tweets present in both sources are imported exactly once.
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTPUT_ROOT = HERE / "output"


def detect_account_id(tweets: list[dict]) -> tuple[str | None, list[tuple[str, int]]]:
    """Best-effort: the user's own user_id_str dominates `tweets`/`replies` tabs.
    Returns (winner, top_5_for_inspection)."""
    c: Counter[str] = Counter()
    for t in tweets:
        uid = t.get("user_id_str")
        if uid:
            c[uid] += 1
    if not c:
        return None, []
    return c.most_common(1)[0][0], c.most_common(5)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("handle", help="your X handle, no @")
    ap.add_argument("-o", "--output", help="output ZIP path")
    ap.add_argument("--account-id", help="override auto-detected account ID")
    args = ap.parse_args()

    src = OUTPUT_ROOT / args.handle / "tweets.jsonl"
    if not src.exists():
        print(f"ERROR: no tweets.jsonl at {src}", file=sys.stderr)
        sys.exit(1)

    tweets: list[dict] = []
    with src.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                tweets.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not tweets:
        print("ERROR: tweets.jsonl is empty", file=sys.stderr)
        sys.exit(1)

    out_path = (
        Path(args.output) if args.output
        else OUTPUT_ROOT / args.handle / f"{args.handle}-fetch.zip"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    wrapped = [{"tweet": t} for t in tweets]
    tweets_js = "window.YTD.tweets.part0 = " + json.dumps(wrapped, ensure_ascii=False)

    account_id = args.account_id
    top5: list[tuple[str, int]] = []
    if not account_id:
        account_id, top5 = detect_account_id(tweets)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("data/tweets.js", tweets_js)
        if account_id:
            account = [{"account": {
                "accountId": str(account_id),
                "username": args.handle,
                "createdAt": "",
                "accountDisplayName": args.handle,
            }}]
            z.writestr(
                "data/account.js",
                "window.YTD.account.part0 = " + json.dumps(account, ensure_ascii=False),
            )

    print(f"Wrote {out_path}")
    print(f"  {len(tweets)} tweets")
    if account_id:
        print(f"  account_id: {account_id}")
        if top5 and len(top5) > 1:
            # Likes/bookmarks tabs include tweets by others — show counts so the
            # user can sanity-check we picked the right ID.
            print(f"  (top user_id_str counts: {top5})")
    else:
        print("  WARNING: no account_id found — self-reply threading will be disabled.")
        print("  Re-run with --account-id <your-numeric-twitter-user-id> to enable it.")


if __name__ == "__main__":
    main()
