# Twitter / X fetcher (host-side, optional)

Companion tool that scrapes your own X.com account in a logged-in browser on
your Mac, then packages the result so the OnionPress Twitter archive
importer accepts it. **Not part of the shipped OnionPress.app** — this lives
here in `tools/` for users who can't wait for Twitter's official archive
download.

Tracked in [issue #240](https://github.com/brewsterkahle/onionpress/issues/240).

---

## When to use this

Use the fetcher only when **all** of the following are true:

- You want your own tweets imported into your OnionPress site **now**, and the
  official archive hasn't arrived (or won't).
- You have direct internet access on your Mac (not just Tor). The fetcher
  must **not** be run through Tor — see "When NOT to use" below.
- You're comfortable logging into x.com in a Playwright-controlled browser
  on your own machine, once.
- You've requested the official archive too, and you understand the
  fetcher's output is a **subset** of what the official archive will give
  you. Run both; the importer dedups.

Typical situations where it's the right call:

- The official archive is taking weeks and you want partial data soon.
- The archive request keeps failing or never lands.
- You want to spot-check what your timeline looks like before committing to
  a full import.

## When NOT to use this

- **Never run it through Tor.** X aggressively flags Tor-exit logins as
  account-compromise events. Even one login over Tor can lock your account.
  The fetcher must run on the host Mac with normal direct internet — *not*
  inside the OnionPress containers, not behind torsocks, not via a SOCKS
  proxy to onionpress-tor. This is a hard rule.
- **Don't use it for someone else's account.** It's built on the assumption
  that you're logging into *your own* X session. Anything else is scraping
  someone else's profile, which has different anti-abuse risks and isn't
  what this tool is for.
- **Don't use it as the primary archive source if Twitter's official archive
  is available to you.** The official `.zip` is strictly more complete:
  it ships the actual image/video files, DMs, lists, accurate retweet
  bodies, and account metadata. The fetcher only captures tweet text and
  metadata + remote media URLs.
- **Don't paste your X password anywhere.** The login flow is a real browser
  window — you type your password into x.com itself, the same as you would
  in normal Chrome. The fetcher never sees the password and never asks for
  one on its own.
- **Don't bundle this into the .app.** It's a separate developer-side tool
  with a Playwright/Chromium dependency (~100 MB). The OnionPress.app
  should not depend on it.

---

## Setup (one-time)

```sh
cd tools/twitter-fetch
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

This drops Chromium into Playwright's cache (`~/Library/Caches/ms-playwright`)
and creates `.venv/`. Both are gitignored.

## First run (log in)

```sh
python fetch.py <your-handle> --headed
```

`--headed` opens a real Chromium window. Sign in to x.com normally (including
2FA). When the prompt in the terminal says "Press Enter once logged in…",
press Enter. The login cookies persist in `tools/twitter-fetch/.profile/`
so subsequent runs don't need `--headed`.

## Subsequent runs

```sh
# Quick top-up: just grab tweets since last run, bail after 200 known tweets.
python fetch.py <your-handle> --minutes 20

# Push the oldest frontier further back. Don't bail on known streaks; rely
# on the wall-clock cap. Re-run as many times as needed to reach the end.
python fetch.py <your-handle> --minutes 30 --no-stop-on-known

# All tabs (default is tweets,replies,media).
python fetch.py <your-handle> --tabs tweets,replies,media,likes --minutes 60
```

Stop conditions per run:

| Flag | Effect |
| --- | --- |
| `--minutes N` | Wall-clock cap. 0 = no cap. |
| `--stop-on-known 200` (default) | Bail after 200 consecutive already-known tweets. Good for daily catch-up. |
| `--no-stop-on-known` | Disable that early exit. Use this to push past the previous frontier into older history. |
| End-of-timeline detection | Built-in — 6 stagnant scroll-height checks plus 20 s with no new captures. |

Capture is incremental: every tweet's `legacy` blob is appended to
`output/<handle>/tweets.jsonl`, deduped by `id_str`. Re-running never
duplicates entries.

## Convert and import

```sh
python to_archive.py <your-handle>
# → output/<handle>/<your-handle>-fetch.zip
```

The ZIP mimics Twitter's official archive shape (`data/tweets.js` +
`data/account.js`) so the existing OnionPress Twitter Archive importer
takes it without modification. Upload it via:

`WP Admin → Tools → Social Archives → Twitter / X → Upload archive`

The importer's `_source_id = "twitter:<id_str>"` idempotency means you can
run this ZIP and the official archive in either order — overlap imports
exactly once.

If `to_archive.py` warns that it can't detect your account ID, re-run with
`--account-id <your-numeric-twitter-user-id>` (you can find it under
account.js in your official archive, or via any Twitter ID lookup tool).
Without it, self-reply threading won't work, but everything else still imports.

---

## Output layout

```
tools/twitter-fetch/
  fetch.py
  to_archive.py
  requirements.txt
  .profile/                          # Playwright user data dir (login cookies)
  output/
    <handle>/
      state.json                     # newest/oldest ID, per-run summaries
      tweets.jsonl                   # cumulative store, one tweet per line
      <handle>-fetch.zip             # built by to_archive.py
      runs/
        20260519-143022/
          raw/UserTweets-00001.json  # raw GraphQL responses (forensic)
          ...
          log.jsonl
```

`output/`, `.profile/`, and `.venv/` are gitignored. Nothing committed by
this tool contains your tweets, cookies, or credentials.

## Limitations and risks

- **Anti-bot risk.** X may surface captchas or rate-limits mid-scroll. The
  fetcher pauses 1.2 – 2.8 s between scrolls (configurable via
  `--min-delay`/`--max-delay` in ms) to look human. If you see a captcha
  in the browser, solve it manually — the persistent profile keeps you
  signed in for the rest of the session.
- **Account lockout risk** is low when running from your home IP but never
  zero. Don't push aggressively. Long, slow sessions are safer than fast
  ones.
- **No media files.** Output references X's CDN URLs (`pbs.twimg.com`),
  which would require a clearnet fetch to materialize — out of scope for
  this tool. The official archive is the right source for media; tweet-level
  dedup means the archive's local files will attach correctly once it lands.
- **GraphQL shape rotates.** X periodically changes its internal query
  shapes. The fetcher extracts tweets by walking responses for any object
  with a `legacy` field containing `id_str` + `full_text`/`text`, which is
  resilient to most rotations, but a deep restructuring would break it.
  If captures drop to zero unexpectedly, that's the likely cause — file
  an issue.
- **Likes tab includes others' tweets.** If you scrape `likes`, captured
  tweets won't all be authored by you. The converter's `--account-id`
  auto-detection picks the dominant `user_id_str`, which is still you for
  any reasonable ratio of own-tweets to liked-tweets, but watch the top-5
  counts the converter prints to confirm.
