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

## Working rules

- All fork work happens in worktrees under `.worktrees/`; never commit on the root
  checkout.
- `target` (a stray symlink to a build host path) was removed and gitignored in this
  wave; if it reappears, something is running the fork-image workflow locally.
- moss-side coordination: after the rename lands on fork main, update moss's
  `plugins/onionpress/` references (`onionpress-moss-receiver` → new name) and the
  `stack-manifest.json` release pin. Wire protocol is unchanged — name-level only.

## Status (update me)

| Item | State |
|---|---|
| Phase 0 hygiene/rename/hardening (branch `upstream-prep`) | in progress, 2026-08-16 |
| `upstream/fixes` | not started |
| `upstream/tor-bridges` | not started |
| `upstream/static-first-serving` | not started |
| `upstream/static-publish` | not started |
| moss plugin rename follow-up | not started |
