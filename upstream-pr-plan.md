# Upstreaming plan: fork → brewsterkahle/onionpress

**This file coordinates the upstream contribution. Read it before touching receiver
naming, tor image pins, or anything under `upstream/*` branches.** It is fork-internal
and is never itself sent upstream. Status section at the bottom is live — update it as
branches and PRs move.

## Decisions (locked 2026-08-16)

- **Shape:** a stacked series of 4 upstream PRs, smallest and least controversial first.
  Each PR is a handful of squashed logical commits, not our raw history (64 commits at
  the time of the audit, 9 merges — not rebasable as a range).
- **Upstream base:** `94ce1a363` (upstream main; fully contained in our history, so
  there is no divergence to reconcile — we are strictly ahead).
- **Branches:** `upstream/fixes`, `upstream/tor-bridges`, `upstream/static-first-serving`,
  `upstream/static-publish` — each created from `94ce1a363`, built by cherry-pick from a
  frozen snapshot of our main (`SNAP=3f40aa75` for this wave). They are **frozen**: do
  not merge fork main into them; anything landing on main later is the next wave.
- **Rename in the fork FIRST:** the receiver becomes generic
  (`onionpress-static-receiver.php`, `onionpress_static_*`) on our main *before* the
  upstream branch is cut, and moss's plugin is updated to match. This keeps fork and
  upstream converged instead of diverging forever on names.
- **Hardening before proposing the receiver upstream:** allowlist permission check
  (denylist → positive loopback+gateway check), fixture tests for the tar extractor's
  reject paths, and dropping the legacy raw-body upload (+ its 512M `memory_limit`)
  once moss is confirmed multipart-only.

## The four PRs

| PR | Branch | Content | Depends on |
|---|---|---|---|
| 1 | `upstream/fixes` | port-offset resolution fix, EXIT-trap fix, auto-login `redirect_to` sanitization (+bypass tests), reachability tri-state + status.json fields, VM_MEMORY=2 | — |
| 2 | `upstream/tor-bridges` | tor image PT binaries (Dockerfile), entrypoint bridge/PT/proxy config (C Tor + Arti), compose env passthrough, config-template docs, watchdog serving-ladder, bootstrap diagnostic tool, wayback sweep hardening | — |
| 3 | `upstream/static-first-serving` | Apache static-first conf + runtime inject/repair (`install/ensure_static_site_conf`), `--apache-conf-dir` on all provision paths | — |
| 4 | `upstream/static-publish` | renamed receiver mu-plugin, `docs/static-publish-protocol.md`, `onionname` CLI, `--managed`, start-idempotency | PR 3 |

## Never upstream (any PR)

- Self-updater repoint: `updater.py:114`, `menubar.py:4349,4413`, `cli.py:339` point at
  `Symbiosis-Lab/onionpress` releases. Sending that upstream is a supply-chain change.
  Revert to `brewsterkahle` in every upstream branch.
- `ghcr.io/symbiosis-lab/onionpress-tor` image pins (`docker-compose.yml:3,135`,
  `tools/diagnose-tor-bootstrap.sh:30`) and `.github/workflows/fork-tor-image.yml`.
  The fork image exists only because upstream's image lacks obfs4proxy/snowflake-client;
  PR 2's Dockerfile change makes it unnecessary. Upstream must rebuild + repin its own
  image — the PR text says so. Also flag: `containers.py:25` and `launcher_ops.py:27`
  still pin an older upstream digest without PT binaries (bridges silently no-op on the
  takeover-worker/onionheaven path).
- Fork CI: `.github/workflows/build-dmg.yml` (repo-gated, `v*-moss.*` tags),
  `docker-publish.yml` repo guards.
- Fork docs: `BUILD-FORK.md`, `moss-integration-roadmap.md`, `self-healing-design.md`,
  `CLAUDE.md`, this file.
- `install-receiver-live.sh` (dev convenience, superseded by `install_static_site_conf`).
- `build/build-dmg-simple.sh` improvements (universal-arch gate, codesign fix) are
  upstream-worthy but tangled with fork CI in `b50483dc` — deferred to an optional PR 5.

## Cluster → commit map (against `94ce1a363..60dd126a`, for cherry-picking)

- Tor/bridge: 24cca087 aa4e1435 d2ec0865 ce1c8491 9b60328b 828bee44 23457e52 d371a8fd
- Watchdog: (in the feat/watchdog-escalation lineage merged via d0873b45)
- Wayback: 13faeb58 60a305d8 34bf5ffc 43d19de8 a6d2d33a
- Receiver: 0634ae91 27ceb0e7 9d62a168 cae1f206 f508db33 fdbc0b28 5037f53c + v1.2
  (8053adba 228c12a0 3628ad2a)
- Fork-image (drop wholesale): 3939e0bf a30f4640 6d8bfdc6 a36ab83a f07d2c8d —
  6d8bfdc6 also touches `config-template.txt` (legit bridge docs); compose must be
  hand-reconciled, not cherry-picked.
- Updater repoint (never pick): 5f874c7e. Note `onionpress-settings.php` was never
  repointed — no revert needed there.
- Mixed commit needing a split: b50483dc (build-dmg script vs fork workflows).

## Branch topology

**Fork `main` is the integration branch** — the single most-up-to-date truth.
Everything converges there: feature branches, this wave's `upstream-prep`
hygiene/hardening work, and (after each upstream PR merges) a back-merge of
upstream main. The `upstream/*` branches are the opposite of integration
branches: frozen export snapshots cut from the upstream base for review, fed
*from* main's content, never merged *into* from main while their PR is open.

## Working rules

- All fork work happens in worktrees under `.worktrees/`; never commit on the root
  checkout.
- `target` (a stray symlink to a build host path) was removed and gitignored in this
  wave; if it reappears, something is running the fork-image workflow locally.
- moss-side coordination: after the rename lands on fork main, update moss's
  `plugins/onionpress/` references (`onionpress-moss-receiver` → new name) and the
  `stack-manifest.json` release pin. Wire protocol is unchanged — name-level only.

### Ongoing development flow (the wave model)

Converged 2026-08-16 across the hel and Mac sessions; the moss-side counterpart
lives in moss's `.claude/CLAUDE.md` ("Multi-agent development flow").

- **Develop on fork `main`, always.** Nobody develops on `upstream/*`. When work
  is ready to go upstream, freeze a snapshot SHA of main and transplant the
  upstreamable deltas onto export branches cut from the upstream base — that is
  a *wave*. Anything landing on main after the freeze is the next wave, never
  scope creep on open PRs.
- **Commit purity on main: every commit is wholly upstreamable or wholly
  fork-only, never mixed.** Mixed commits (`b50483dc`, the updater repoint
  inside `cli.py`) cost ~10x at extraction time what splitting costs at write
  time. Fork-only mechanisms go in dedicated files where possible, and register
  themselves in this file's never-upstream list in the same commit.
- **While a PR is open, review fixes land on the frozen export branch** and are
  cherry-picked back to main — never merge main into an open export branch.
- **After upstream merges a PR, back-merge upstream main into fork main.** Each
  merged PR shrinks the fork↔upstream delta; the end state is fork main =
  upstream + the never-upstream set.
- **Version-gated contracts have one owner.** `receiver_version`, image digest
  pins: exactly one branch may bump the next value at a time; say so here or in
  the fabric before bumping. (The 1.3-vs-2.0 receiver fork cost a full
  coordination cycle.)
- **Renames of contract-bearing files are sequenced, not just announced:**
  fork main → consumers (moss plugin) → upstream export. Proven with the
  receiver rename this wave.
- **Push branches at the first coherent commit** and after each green commit —
  an unpushed branch is invisible to every peer and can't be in anyone's
  snapshot. Subagents doing multi-file work commit WIP early; before redoing a
  "dead" agent's work, verify its worktree (PR3's agent died *after*
  committing — relaunching blindly would have clobbered finished work).
- **Never use FETCH_HEAD in this repo** — concurrent fetches from parallel
  agents clobber it silently. Resolve explicit SHAs once and pass those.

## Outreach: the reader-friendly path (designed from Brewster's chair)

Sequence, in the order he experiences it (drafts in `docs/proposals/`; none
posted without sign-off):

1. **Personal note first, not GitHub** — ~3 sentences + the demo video + ONE
   link (the umbrella issue). Offer a call/live demo. GitHub notifications
   are the artifact trail, not the knock on the door.
2. **Umbrella issue** = the single entrance point:
   "Static-site publishing for OnionPress — working demo + PR series".
   Contents in order: the demo recording at the top (≤10 MB attaches inline;
   else a Symbiosis-Lab/onionpress release asset + GIF teaser; NEVER a
   moss-repo link — private, 404s for him), a **"try it yourself in ~5
   minutes"** section (fork release + `./test-receiver.sh` +
   `docs/static-publish-protocol.md`), the 4-PR table smallest-first, a
   **"what we deliberately did NOT send"** section (self-filtering is the
   trust signal), and a short "where we'd like to go together" paragraph.
3. **PR 1 opens with the umbrella** — the tiny, obviously-correct on-ramp
   that builds the working relationship. PRs 2–4 same-day-numbered ("part N
   of 4, independently reviewable") or after first response — judgment call
   at posting time. Every PR body: problem → change → why generic to any
   SSG → how tested; each links the umbrella.
4. **The 3 collaboration proposals stay a paragraph in the umbrella.** Open
   as individual issues only when he engages with that thread (4 PRs + 3
   RFC issues on day one reads as homework). Reserve drafts:
   `docs/proposals/{decouple-serving-core,dns-domains,wayback-behind-gfw}.md`;
   umbrella draft: `docs/proposals/umbrella-issue.md`.

Legitimacy is carried by shape, not assertion: a small first PR, honest PR
bodies, the explicit not-sent list, and one sentence noting we extended
OnionPress's own mechanisms rather than building parallel ones.

1. **Decouple OnionPress from WordPress** so the serving core (tor + static
   receiver + Apache/current-symlink) is a light target for any SSG to
   integrate; WordPress becomes one optional publisher among many. PR 3/4
   (static-first serving + receiver) are the natural evidence base.
2. **DNS domain support when publishing via a static publisher** — the same
   dual onion+clearnet story onionpress.org itself has, exposed through the
   publish flow / naming CLI.
3. **Making the Wayback fallback usable behind the GFW** — archive access
   paths that survive when web.archive.org itself is blocked; builds on the
   sweep-hardening work in PR 2.

Draft the issue texts in the fork (`docs/proposals/…`, fork-only) before
posting, so they get the same review as code.

## Status (update me)

| Item | State |
|---|---|
| Phase 0 hygiene/rename/hardening | done, merged to main, pushed (2026-08-16) |
| Outreach drafts (umbrella + 3 proposals) | drafted in `docs/proposals/`, not posted |
| `upstream/fixes` (PR1, 5 commits) | built, verified, pushed |
| `upstream/tor-bridges` (PR2, 5 commits) | built, verified, pushed |
| `upstream/static-first-serving` (PR3, 2 commits) | built, verified, pushed |
| `upstream/static-publish` (PR4, 4 commits, stacks on PR3) | built, verified, pushed |
| moss plugin rename follow-up | done, committed in moss repo |
| Opening PRs/issues on brewsterkahle/onionpress | not started — needs user sign-off before posting |
