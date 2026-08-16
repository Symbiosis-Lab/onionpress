# DRAFT — umbrella tracking issue for brewsterkahle/onionpress

> Status: draft, not posted. Posting sequence (user sign-off required):
> 1. Personal note to Brewster (not GitHub): ~3 sentences + the demo video +
>    ONE link (this issue). Offer a call/live demo.
> 2. Post this issue with the video embedded at top (≤10 MB embeds inline;
>    otherwise a Symbiosis-Lab/onionpress release asset + GIF teaser in the
>    issue). Never a moss-repo link (private → 404 for him).
> 3. Open PR 1 the same day. Open PRs 2–4 same-day-numbered or after first
>    response — judgment call.
> 4. The three collaboration proposals stay a PARAGRAPH here; open them as
>    individual issues only when he engages with that thread. Drafts live in
>    this directory, ready.

---

**Title: Static-site publishing for OnionPress — working demo + PR series**

*(demo video embedded here)*

The video is [moss](https://mosspub.com) — a static-site publishing app —
publishing a site to a live `.onion` through OnionPress in one click.
OnionPress stays exactly what it is; it additionally becomes a publish
target any static-site generator can drive. Everything the demo uses is
offered back in the PRs below — we kept nothing needed to reproduce it.

## Try it yourself (~5 minutes)

1. Install OnionPress from our fork's release (or run your own build with
   the PRs applied) and start it.
2. `./test-receiver.sh` — publishes a fixture site over the loopback
   receiver and verifies it is served at the site root ahead of WordPress.
3. The wire protocol any SSG can implement: `docs/static-publish-protocol.md`.

## The PRs (suggested review order)

Each is self-contained and reviewable alone; nothing later is required to
accept something earlier.

| PR | What | Size |
|---|---|---|
| #__ | Bug fixes: port re-resolution after restart, a start that exited 1 on success, an auto-login open-redirect, reachability tri-state in status.json, 2 GB default VM memory | S |
| #__ | Tor bridge/pluggable-transport support (C Tor **and** Arti), a watchdog that escalates on "serving" not "bootstrapped", Wayback sweep hardening | M |
| #__ | Static-first serving: Apache rules that serve a published static site ahead of WordPress, runtime-injected and self-repairing | S |
| #__ | The static-publish receiver: loopback REST endpoints (status / upload / atomic commit), hardened tar extractor, headless `onionname` CLI, `--managed` unattended installs | M |

A design note on all of it: where OnionPress already had a mechanism —
Save Page Now archiving, the onion-service lifecycle, provisioning — we
extended that mechanism rather than building a parallel one.

## What we deliberately did NOT send

Our fork also carries a repointed self-updater, a fork-built tor image
(superseded by the one-line Dockerfile change in the bridges PR — you'd
rebuild and repin your own image), and fork CI. None of it is in these PRs.
Two pre-existing pins worth knowing: `containers.py` and `launcher_ops.py`
pin an older tor digest than docker-compose, so bridges won't reach the
takeover-worker path until all three move together (flagged in the bridges
PR).

## Where we'd like to go together

Three directions we'd love your read on — happy to open any of these as its
own issue if it interests you:

- **A lighter serving core**: decouple tor + static receiver + Apache from
  WordPress so any SSG integrates without carrying the full stack.
- **Clearnet domains too**: a DNS domain alongside the `.onion` when
  publishing through a static publisher — the dual life onionpress.org
  itself has.
- **Wayback behind the GFW**: making the archive fallback usable where
  web.archive.org itself is blocked.
