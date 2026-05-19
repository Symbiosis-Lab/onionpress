#!/usr/bin/env python3
"""
Draft host-side X.com fetcher for OnionPress (issue #240).

Usage (one-time):
    cd tools/twitter-fetch
    python3 -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    playwright install chromium

Run (re-runnable — each run resumes against the same per-handle store):
    python fetch.py <handle> --headed             # first run, log in
    python fetch.py <handle> --minutes 20         # 20-minute slow pass
    python fetch.py <handle> --tabs tweets,replies --minutes 30
    python fetch.py <handle> --no-stop-on-known   # don't early-exit; push older

State for handle `H` lives under output/H/:
    state.json       — last_run, newest/oldest tweet IDs, per-tab progress
    tweets.jsonl     — one tweet legacy object per line, deduped by id_str
    runs/<ts>/raw/   — raw GraphQL responses for forensics
    runs/<ts>/log.jsonl

DO NOT run this through Tor — see issue #240.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

from playwright.async_api import Page, Response, async_playwright

HERE = Path(__file__).resolve().parent
PROFILE_DIR = HERE / ".profile"
OUTPUT_ROOT = HERE / "output"

TWEET_OPS = {
    "UserByScreenName",
    "UserTweets",
    "UserTweetsAndReplies",
    "UserMedia",
    "Likes",
    "Bookmarks",
    "TweetDetail",
    "HomeTimeline",
    "HomeLatestTimeline",
}

TABS = {
    "tweets":  "",
    "replies": "/with_replies",
    "media":   "/media",
    "likes":   "/likes",
}


def op_name(url: str) -> str | None:
    parts = url.split("/i/api/graphql/", 1)
    if len(parts) != 2:
        return None
    return parts[1].split("?", 1)[0].split("/", 1)[-1]


def extract_tweets(obj: Any, out: dict[str, dict]) -> None:
    """Walk a GraphQL response and pull out any nested tweet `legacy` blobs.
    Resilient to X's frequent shape rotations — we just look for the marker."""
    if isinstance(obj, dict):
        legacy = obj.get("legacy")
        if isinstance(legacy, dict) and "id_str" in legacy and (
            "full_text" in legacy or "text" in legacy
        ):
            out[legacy["id_str"]] = legacy
        for v in obj.values():
            extract_tweets(v, out)
    elif isinstance(obj, list):
        for v in obj:
            extract_tweets(v, out)


class Store:
    """Per-handle cumulative store. Reads existing state on init so repeated
    runs dedup against everything previously captured."""

    def __init__(self, handle: str):
        self.handle = handle
        self.root = OUTPUT_ROOT / handle
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"
        self.tweets_path = self.root / "tweets.jsonl"

        self.state: dict = {
            "handle": handle,
            "newest_id": None,
            "oldest_id": None,
            "total_tweets": 0,
            "runs": [],
        }
        if self.state_path.exists():
            self.state.update(json.loads(self.state_path.read_text()))

        self.seen: set[str] = set()
        if self.tweets_path.exists():
            with self.tweets_path.open() as f:
                for line in f:
                    try:
                        self.seen.add(json.loads(line)["id_str"])
                    except Exception:
                        pass
        print(f"Store: {len(self.seen)} tweets already captured for @{handle}")

        self._tweets_out = self.tweets_path.open("a")

    def ingest(self, tweets: dict[str, dict]) -> tuple[int, int]:
        """Returns (new_count, known_count)."""
        new = 0
        known = 0
        for tid, tw in tweets.items():
            if tid in self.seen:
                known += 1
                continue
            self.seen.add(tid)
            self._tweets_out.write(json.dumps(tw, ensure_ascii=False) + "\n")
            new += 1
            # Track oldest/newest by numeric ID (Snowflake IDs are time-ordered).
            tid_int = int(tid)
            if self.state["newest_id"] is None or tid_int > int(self.state["newest_id"]):
                self.state["newest_id"] = tid
            if self.state["oldest_id"] is None or tid_int < int(self.state["oldest_id"]):
                self.state["oldest_id"] = tid
        if new:
            self._tweets_out.flush()
            self.state["total_tweets"] = len(self.seen)
        return new, known

    def finalize_run(self, run_summary: dict) -> None:
        self.state["runs"].append(run_summary)
        self.state["runs"] = self.state["runs"][-50:]
        self.state_path.write_text(json.dumps(self.state, indent=2))
        self._tweets_out.close()


class Capture:
    def __init__(self, store: Store, run_dir: Path):
        self.store = store
        self.raw_dir = run_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.log = (run_dir / "log.jsonl").open("a")
        self.seq = 0
        self.new_in_run = 0
        self.known_in_run = 0
        self.known_streak = 0
        self.last_new_t = time.time()

    async def on_response(self, resp: Response) -> None:
        op = op_name(resp.url)
        if op not in TWEET_OPS:
            return
        try:
            body = await resp.json()
        except Exception:
            return
        self.seq += 1
        fname = f"{op}-{self.seq:05d}.json"
        (self.raw_dir / fname).write_text(json.dumps(body, ensure_ascii=False))

        tweets: dict[str, dict] = {}
        extract_tweets(body, tweets)
        new, known = self.store.ingest(tweets)
        self.new_in_run += new
        self.known_in_run += known
        if new:
            self.known_streak = 0
            self.last_new_t = time.time()
        else:
            self.known_streak += known

        self.log.write(json.dumps({
            "t": time.time(), "op": op, "status": resp.status,
            "file": fname, "tweets": len(tweets), "new": new, "known": known,
        }) + "\n")
        self.log.flush()
        print(f"  {op} → +{new} new, {known} known "
              f"(run: +{self.new_in_run}, streak: {self.known_streak})",
              flush=True)


async def is_logged_in(page: Page) -> bool:
    cookies = await page.context.cookies("https://x.com")
    return any(c["name"] == "auth_token" and c["value"] for c in cookies)


async def login_flow(page: Page) -> None:
    print("Not logged in — opening x.com/login. Sign in (incl. 2FA), then press Enter.")
    await page.goto("https://x.com/login", wait_until="domcontentloaded")
    await asyncio.get_event_loop().run_in_executor(None, input, "Press Enter once logged in… ")
    if not await is_logged_in(page):
        print("ERROR: still no auth_token cookie. Aborting.", file=sys.stderr)
        sys.exit(1)


async def scroll_tab(
    page: Page, cap: Capture, url: str,
    deadline: float | None, stop_on_known: int, min_d: int, max_d: int,
) -> str:
    """Returns a reason: 'deadline' | 'known-streak' | 'end-of-timeline' | 'idle'."""
    print(f"→ {url}")
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)

    last_height = 0
    stagnant_height = 0
    while True:
        if deadline and time.time() >= deadline:
            return "deadline"
        if stop_on_known and cap.known_streak >= stop_on_known:
            return "known-streak"

        height = await page.evaluate("document.documentElement.scrollHeight")
        if height == last_height:
            stagnant_height += 1
        else:
            stagnant_height = 0
        last_height = height

        # 6 ticks with no scroll growth AND >20s with no new tweets → end of timeline.
        if stagnant_height >= 6 and (time.time() - cap.last_new_t) > 20:
            return "end-of-timeline"

        await page.evaluate("window.scrollBy(0, window.innerHeight * 0.85)")
        await page.wait_for_timeout(random.randint(min_d, max_d))


async def run(args: argparse.Namespace) -> None:
    store = Store(args.handle)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = store.root / "runs" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    cap = Capture(store, run_dir)

    deadline = (time.time() + args.minutes * 60) if args.minutes else None
    stop_on_known = 0 if args.no_stop_on_known else args.stop_on_known

    print(f"Run dir: {run_dir}")
    print(f"Deadline: {'∞' if not deadline else f'{args.minutes} min'} | "
          f"stop-on-known: {stop_on_known or 'off'}")

    tab_results: dict[str, str] = {}
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=not args.headed,
            viewport={"width": 1280, "height": 1800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/132.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.on("response", lambda r: asyncio.create_task(cap.on_response(r)))

        await page.goto("https://x.com/home", wait_until="domcontentloaded")
        if not await is_logged_in(page):
            await login_flow(page)

        for tab in args.tabs.split(","):
            suffix = TABS.get(tab)
            if suffix is None:
                print(f"unknown tab '{tab}', skipping", file=sys.stderr)
                continue
            reason = await scroll_tab(
                page, cap, f"https://x.com/{args.handle}{suffix}",
                deadline, stop_on_known, args.min_delay, args.max_delay,
            )
            tab_results[tab] = reason
            print(f"  ↳ stop reason: {reason}")
            if reason == "deadline":
                break

        await ctx.close()

    summary = {
        "stamp": stamp,
        "new": cap.new_in_run,
        "known": cap.known_in_run,
        "responses": cap.seq,
        "tabs": tab_results,
        "duration_s": int(time.time() - (deadline - args.minutes * 60) if deadline else 0),
    }
    store.finalize_run(summary)
    print(f"\nDone. +{cap.new_in_run} new tweets this run "
          f"(total: {store.state['total_tweets']}).")
    print(f"Newest: {store.state['newest_id']}  Oldest: {store.state['oldest_id']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("handle", help="your X handle, no @")
    ap.add_argument("--tabs", default="tweets,replies,media",
                    help=f"comma-separated subset of {sorted(TABS)}")
    ap.add_argument("--minutes", type=int, default=0,
                    help="wall-clock cap in minutes (0 = no limit)")
    ap.add_argument("--stop-on-known", type=int, default=200,
                    help="bail after N consecutive already-known tweets (head-only refresh)")
    ap.add_argument("--no-stop-on-known", action="store_true",
                    help="never bail on known streak — use this to push the oldest frontier")
    ap.add_argument("--min-delay", type=int, default=1500, help="ms between scrolls (min)")
    ap.add_argument("--max-delay", type=int, default=2800, help="ms between scrolls (max)")
    ap.add_argument("--headed", action="store_true",
                    help="show the browser window (required on first run for login)")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
