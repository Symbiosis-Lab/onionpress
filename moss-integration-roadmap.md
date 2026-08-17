# OnionPress full seta-parity integration — roadmap + track contracts

**Goal:** a fully workable OnionPress-in-moss flow to demo to Brewster — exactly the seta flow, but OnionPress: moss downloads a hosted OnionPress binary → user picks an onion name → one-click publish → live at `<name>`'s `.onion` address.

**Status 2026-07-21:** design + gap-map complete (4-track mapping workflow). Publish transport (plugin↔receiver) + deploy-target commands + site_url wiring + download UI prototype already built on `feat/host-row-download-ui`. This doc drives the remaining build.

## Locked decisions (2026-07-21)

- **Fork:** `guoliu/onionpress` (public — correct for the eventual upstream PR to Brewster).
- **Apache static-first conf → RUNTIME-INJECT**, not baked into the WordPress image. Provision-time `docker cp` + `a2enconf` (like the mu-plugin already does). ⇒ **no image rebuild, no registry, reuse Brewster's images unchanged.** This erases the biggest infra cost.
- **Arch:** arm64-only DMG for the demo (moss is a Mac app; buildable locally, no self-hosted runner).
- **Self-updater:** repoint `updater.py` to the fork so forked installs don't silently update back to Brewster's receiver-less upstream.
- **DMG hosting:** the fork's GitHub releases (`release.sh` already does `gh release create`).
- **Naming transport → headless OnionPress CLI** (revised — avoids a tor-image rebuild). Add an `onionname` subcommand wrapping the already-built `Registrar` client (which uses the working `docker exec onionpress-tor curl --socks5` pattern). moss drives it via `execute_binary` on the staged app. Rejected: routing through the WordPress receiver, which would force the WP container to reach OnionHome over Tor (SOCKS plumbing / tor-image change).
- **Name semantics for moss:** onionname → onion-address mapping, root-served. Ignore OnionPress's WP-admin/subsite-path coupling (moss serves at the address root; the name is a memorable handle that resolves to the address).
- **First-publish wizard:** a **sibling `OnionPressPublishModal`**, not a generalization of the moss `FirstPublishModal` (isolation; must not risk the seta path).
- **Name step:** included (the user explicitly wants "set onion name"). No email verify (onion has no email gate).
- **Acquisition staging dir:** `~/.moss/stacks/onionpress/OnionPress.app` (matches the `~/.moss/bin` precedent; user-owned, no admin prompt).
- **Install auto-starts the stack** (one action; the 3–5 min first run shows as progress on the tile/hairline).

## Inter-track wire contracts (build against these)

### OnionPress headless naming CLI (added by Track OP) + moss naming commands (added by Track ACQ; consumed by Track PARITY)
OnionPress side — add to BOTH `src/onionpress/cli.py` and the shell launcher `app/MacOS/onionpress`, wrapping the existing `Registrar` (`onionnames_registrar.py`). Each prints ONE JSON line to stdout:
- `onionpress onionname suggest` → `{ "name": "<suggested>" }`
- `onionpress onionname check <name>` → `{ "available": bool, "reason": "<str>", "suggestions": ["…"] }`
- `onionpress onionname register <name>` → `{ "ok": true, "name": "<n>", "address": "<addr>.onion", "url": "http://<addr>.onion/" }` | `{ "ok": false, "error": "…", "suggestions": […] }`
- Name rules mirror OnionHome: 5–40 chars, `^[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]$`, not all-numeric (validate client-side too via the existing `onionnames_client.py`).

moss side (Track ACQ) — three Tauri commands that resolve the staged app's `Contents/MacOS/onionpress` and `execute_binary` the subcommand, parsing the JSON line:
- `onionpress_name_suggest() → Result<{name}, String>`
- `onionpress_name_check(name) → Result<{available, reason, suggestions}, String>`
- `onionpress_name_register(name) → Result<{ok, name, address, url}, String>`
Track PARITY calls these from the name picker (build against these signatures; ACQ implements).

### Release manifest (produced by Track OP; consumed by Track ACQ)
The moss side pins a per-release artifact. For the demo:
- `dmg_url`: `https://github.com/guoliu/onionpress/releases/latest/download/onionpress.dmg`
- `sha256`: the pinned hash of that DMG (Track OP emits it at release; ACQ reads it from a small JSON committed in the onionpress plugin, `plugins/onionpress/stack-manifest.json`, updated per release).
- `version`: the fork release tag.

### moss deploy commands (already built; consumed by Track PARITY)
`get_deploy_targets() → {targets:[{id,name}], current}`, `set_deploy_target(id)`, `[deployment].site_url` persistence, `is_deployed()` widened for http onion. Registered + bindings present.

## Phased roadmap (each phase itself parallelizes)

### Phase 1 — Foundation (Track OP, onionpress fork)  [critical path]
Fork prep on `feat/fork-integration`: runtime-inject the Apache conf (remove Dockerfile bake, add provision-time inject), repoint `updater.py` + release origin to the fork, add the **receiver naming routes** above, build an arm64 DMG, and cut a fork release. **Deliverable:** a downloadable OnionPress DMG whose receiver does status + name + generation + commit, hosted at a stable URL, with its sha256.

### Phase 2 — Real acquisition (Track ACQ, moss)  [XL]
Turn the simulator into a real download→verify→stage→start→status loop: `DetachedTaskMeta` owner key + `StackInstall` kind; `DetachedRegistry`→frontend event bridge; a Rust `install_channel_stack` command (download+sha256+disk-check → `hdiutil attach` mount → stage `.app` to `~/.moss/stacks/` → `hdiutil detach` → drive `Contents/MacOS/onionpress start`); restart re-discovery (filesystem + `/status`, not in-memory); replace `stack-download-sim.ts` with the real feed; bundle the plugin (`build-config.toml`). Build/test against a locally-built DMG + placeholder manifest until Phase 1 publishes the real URL/hash. **Deliverable:** moss really downloads/installs/starts OnionPress; the tile + hairline show real progress.

### Phase 3 — seta-parity publish UX (Track PARITY, moss)  [XL]
Wire the Host row to the real `get/set_deploy_targets`; route first-publish by selected host; build `OnionPressPublishModal` (onionname picker with live `/name/check` availability + `/name/register`, then deploy, then a success screen with the `.onion` address, copy, "View on Tor"); persist the named address to `site_url`; make the plugin publish path emit a `FIRST_PUBLISH_COMPLETE` equivalent so the deployment-tab dot/label stay honest; "Start OnionPress, then Publish" empty state when `/status` is down. **Deliverable:** choose OnionPress → pick name → Publish → live at the named onion, feeling like the seta flow.

## Track file boundaries (avoid cross-track conflicts)
- **OP** (onionpress repo, `feat/fork-integration`): `app/Resources/plugins/onionpress-moss-receiver.php`, `app/Resources/docker/wordpress/*`, `src/onionpress/multisite.py`, `updater.py`, build/release scripts. Owns the receiver-contract naming extension.
- **ACQ** (moss worktree `onionpress-acquisition`): `src-tauri/src/system/detached_registry.rs`, a new `src-tauri/src/system/stack_install.rs` (+ lib.rs registration), `frontend/app/dev/stack-download-sim.ts`→real adapter, `frontend/app/settings/sections/plugin-catalog.ts` (feed swap only), `build-config.toml`, `plugins/onionpress/stack-manifest.json`.
- **PARITY** (moss worktree `onionpress-parity`): `frontend/app/settings/sections/deployment.ts` + `deployment/host-row.ts`, `frontend/app/workflows/deploy/deploy-handler.ts`, a new `OnionPressPublishModal`, `plugins/onionpress/src/*` (name calls). Does NOT touch plugin-catalog's sim feed (ACQ owns it).
- Bindings regen + final integration merge handled at convergence by the lead.
