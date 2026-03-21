# OnionPress Project Memory

## Meta
- **This file (`CLAUDE.md`) is the project memory.** Store all new memories and notes here so they travel with the repo.

## Naming Rules (IMPORTANT)
- The project is called **OnionPress** (one word, capital O and P). Never "Onion.Press", "onion.press", or "onion-press".
- Data directory: `~/.onionpress/` (not `~/.onion.press/`)
- GitHub repo: `brewsterkahle/onionpress`
- Use **"onion service"** (not "hidden service") in all user-facing text. Tor Project deprecated "hidden service" terminology.
  - Exception: file paths like `/var/lib/tor/hidden_service/` and Docker image names like `goldy/tor-hidden-service` cannot change — those are external identifiers.
- When writing new code, docs, issues, or UI text, always use "OnionPress" and "onion service".

## Key Architecture
- macOS menubar app (py2app built from `src/menubar.py`)
- Launcher shell script at `OnionPress.app/Contents/MacOS/onionpress`
- Docker containers (tor, tor-client, wordpress, mariadb) run inside Colima VM
- Logs at `~/.onionpress/onionpress.log` and `~/.onionpress/launcher.log`

## Why py2app
- Modern Macs do NOT ship a usable Python — `/usr/bin/python3` is just a shim that prompts to install Xcode CLI Tools
- Apple removed Python 2 in macOS 12.3 and has no commitment to shipping Python 3 long-term
- py2app bundles the Python interpreter + all dependencies into a self-contained .app so the user never needs to know Python is involved
- This is essential for a consumer app — cannot ask non-technical users to install Xcode Command Line Tools

## Build & Release Process
- MenubarApp built with py2app via `setup.py` (extracted from `build/build-dmg-simple.sh` lines 228-276)
- Must copy `key_manager.py`, `backup_manager.py`, and `setup_window.py` to venv site-packages before build
- **After editing ANY of these `src/` files, you MUST rebuild the MenubarApp** and replace `OnionPress.app/Contents/Resources/MenubarApp/`. The py2app bundle contains compiled `.pyc` files — editing `src/` alone does NOT update the running app:
  - `src/menubar.py` — main app (entry point)
  - `src/onion_proxy.py` — HTTP proxy, setup form, Wayback Machine login
  - `src/key_manager.py` — vanity key management
  - `src/backup_manager.py` — backup/restore
  - `src/setup_window.py` — native setup window
  - `src/onionheaven.py` — OnionHeaven integration
  - `src/onion_auth.py` — ed25519 signatures for OnionHeaven API auth (also copied to `docker/tor/`)
  - `src/install_native_messaging.py` — browser extension support
  - `setup.py` — py2app config (if you add a new local module, add it to `includes` AND the build script's `cp` lines)
- **Release via GitHub releases only** (`gh release create`). Do NOT upload to Internet Archive.
- Version must be bumped in **all 5 locations** (Finder shows the MenubarApp plist version in Get Info):
  1. `src/menubar.py` — `self.version = "X.Y.Z"` (~line 529)
  2. `src/menubar.py` — `self.log("QUIT BUTTON CLICKED - vX.Y.Z RUNNING")` (~line 3446)
  3. `setup.py` — `CFBundleVersion` and `CFBundleShortVersionString` (2 values, same line area)
  4. `OnionPress.app/Contents/Info.plist` — `CFBundleShortVersionString`
  5. `OnionPress.app/Contents/Resources/MenubarApp/Contents/Info.plist` — `CFBundleShortVersionString` AND `CFBundleVersion` (py2app build artifact; the build script overwrites this, but it must also be updated manually for non-rebuild releases)
  6. `OnionPress.app/Contents/Resources/docker/tor/onionheaven-server.py` — `ONIONHEAVEN_SERVER_VERSION` (shown in `/status` response)
- **py2app vs setuptools 81+ incompatibility** — setuptools 81 (released 2026-02-06) removed `dry_run` from `distutils.spawn()`, which py2app 0.28.9 still uses. The build script (`build/build-dmg-simple.sh`) handles this automatically: it tries the build first, and falls back to `setuptools<81` only if py2app fails. Once py2app ships a fix, the fallback stops being needed. Track upstream: https://github.com/ronaldoussoren/py2app/issues/557

## Security
- **Database passwords are randomly generated per install** — never use defaults or hardcoded passwords. The `ensure_secrets` function generates unique passwords with `openssl rand` on first run, saved to `~/.onionpress/secrets`.
- Do not commit or log database passwords.

## Colima VM Sandboxing
- The VM is restricted to only mount `~/.onionpress/` (via `--mount "$DATA_DIR:w"`), NOT the full home directory
- This limits blast radius if a container (e.g., WordPress) is compromised — attacker cannot read `~/Documents`, `~/.ssh/`, etc.
- The only host bind mount needed is for vanity key import (`~/.onionpress/shared/vanity-keys/`)
- All other container data uses Docker named volumes (which live inside the VM)
- **Do not add additional `--mount` flags without considering security implications**

## Docker Image Pull Strategy (IMPORTANT)
- **Do NOT use `--pull always` on `docker compose up`** — it causes double container recreation
- The launcher already pulls images via `docker compose pull` before starting containers
- `docker compose up -d` (without `--pull always`) automatically recreates containers if the local image changed from the pull
- Adding `--pull always` back causes a redundant pull → container recreated mid-bootstrap → double Tor bootstrap → brief reachability then gap → browser opens to a dead service
- This has been added and reverted multiple times — the separate `pull` step is the correct approach

## Multi-User Support (v2.4.11+)
- Multiple macOS users can run OnionPress simultaneously from the same `/Applications/OnionPress.app`
- Each user gets their own `~/.onionpress/` data dir, Colima VM, and Docker containers
- **Port offsets**: second user auto-detects port 8080 is taken and offsets all ports by +10000 (18080/19050/19077), third user by +20000, etc. Max ~5 users.
- **Detection uses socket bind test**, not `lsof` — `lsof` only sees the current user's processes and cannot detect ports bound by other users
- **Port detection must happen in the MenubarApp's `__init__`** (Python socket bind), not in the shell scripts — the MenubarApp launches first and needs the correct ports before the `onionpress` script runs
- **Module-level constants in `onion_proxy.py`** (`PROXY_PORT`, `PHP_PROXY_PORT`) are set at import time. The MenubarApp must update these globals after detecting the offset: `onion_proxy.PROXY_PORT = self.proxy_port`
- The `onionpress` shell script also has detection as a fallback (for standalone use), but respects pre-set `ONIONPRESS_PORT_OFFSET` env var from the MenubarApp
- **`LSMultipleInstancesProhibited` must NOT be in any Info.plist** — macOS enforces it across ALL users sharing the same app bundle, not just per-user
- **`pgrep` in the launcher must use `-u $(whoami)`** to restrict to the current user's processes
- **PID lock file** (`~/.onionpress/onionpress.pid`) prevents the same user from double-launching; cleaned up via `trap` on EXIT/INT/TERM/HUP
- **Container-internal ports are NOT offset** — Docker networking (`onionpress-tor:9050`, `onionpress-tor-client:9050`, `wordpress:80`) is isolated per-VM. Only host-side port mappings change.
- **`git add -f OnionPress.app/`** after a build will pick up large downloaded binaries (docker, limactl, docker-compose) — always stage specific paths instead

## Tor Client Container (`onionpress-tor-client`)
- **Independent Arti SOCKS proxy** for true external reachability tests (~20-50MB RAM)
- Same image as `onionpress-tor` (`ghcr.io/brewsterkahle/onionpress-tor:latest`) but runs with `NO_ONION_SERVICE=1` (pure SOCKS, no onion services)
- Has its own Tor circuits and must discover `.onion` addresses through the real Tor network — unlike `onionpress-tor` which resolves its own `.onion` locally via self-connection shortcut
- **No host port mapping** — accessed only via `docker exec` or container-to-container networking (`onionpress-tor-client:9050`)
- Started early alongside WordPress and DB (no dependencies), giving it 60+ seconds to bootstrap while WordPress warms up
- `docker compose down` stops it automatically (no profile needed)
- **Used by**: `torcurl`, `_auto_open_browser_inner()`, `check_tor_reachability()` Check 5, `onion-forward.php` (browser extension PHP proxy)
- **NOT used by**: `src/onionheaven.py` (outbound Tor to external `.onion`), `onionpress-wayback-archive.php` (Wayback Machine), `onionheaven-heartbeat.py` (runs inside container)

## Colima Networking
- **SOCKS proxy DOES work through Colima VM port forwarding** (tested 2026-03-20 — previously documented as broken, now works for both clearnet and .onion)
- `curl --socks5-hostname 127.0.0.1:<host_port>` from the Mac host works fine
- **`docker exec` is still needed for Tor control port** — ControlPort (9051) is not exposed in the main tor container's torrc and not port-mapped in docker-compose
- To enable direct control port access from the host, would need: add `ControlPort 0.0.0.0:9051` + `CookieAuthentication 1` to torrc, expose port in docker-compose, then use Python sockets directly
  - Example: `docker exec onionpress-tor-client curl -s --socks5-hostname 127.0.0.1:9050 http://some-address.onion/`
  - Do NOT use `curl --socks5-hostname 127.0.0.1:9050` from the Mac host — it will fail
- This applies to future mirror system communication (health checks, challenge-response, etc.)
- The tor image is Debian-based and has both `curl` and `wget`
- WordPress container also has `curl`
- Test onion service reachability: `docker exec onionpress-tor-client curl -s --socks5-hostname 127.0.0.1:9050 http://<onion-address>/`
- Test internal WordPress path: `docker exec onionpress-tor curl -s http://wordpress:80/`

## Tor Descriptor Propagation & HSFETCH
- **After ADD_ONION, descriptors take 10-60s to propagate to HSDirs** — clients can't connect until then
- **HSFETCH** forces a client to fetch a descriptor from HSDirs, but if the new descriptor hasn't landed on HSDirs yet, HSFETCH just re-fetches the old one
- **SIGNAL NEWNYM clears the client-side descriptor cache** — must be issued BEFORE HSFETCH, or Tor returns the cached (stale) descriptor instead of fetching fresh
- **Correct sequence**: `SIGNAL NEWNYM` → wait 3s → `HSFETCH <service-id>` → then try connecting. Must be two separate control port connections (can't sleep inside an `nc` pipe)
- **One-shot flush is insufficient** — after a DEL_ONION/ADD_ONION handoff, the descriptor may not be on HSDirs yet when the first flush fires. Flush must be repeated periodically (every 30s) inside polling loops until the client gets the new descriptor
- **NEWNYM is rate-limited** to once per 10 seconds by Tor — don't call it more frequently

## Onion Service Handoff (DEL_ONION/ADD_ONION)
- **Clean handoff = 10-20s transitions. Competing descriptors = minutes of failure.**
- When transferring an onion address (e.g. takeover/recovery), the old holder MUST `DEL_ONION` before the new holder does `ADD_ONION`. If both publish descriptors simultaneously, clients get confused and connections fail for minutes
- ADD_ONION with `ED25519-V3:<key> Flags=Detach` keeps the same .onion address across handoffs
- Detached services survive the control connection closing (`GETINFO onions/detached` to list them)

## Tor Bootstrap Resilience
- **Guard selection is luck-based** — Tor can pick bad/slow guards and get stuck at 5% ("Connecting to a relay") indefinitely
- Deleting `/var/lib/tor/state` and restarting Tor forces fresh guard selection — usually bootstraps in seconds
- Stress test containers have no persistent volumes, so stale state files are from the current session (not old runs)
- **Recommended**: add a watchdog — if Tor doesn't reach 100% bootstrap within 120s, delete state and restart

## Scrub Procedure (backup → reset → restore)
- The "scrub" wipes and rebuilds an OnionPress instance while preserving data
- **Steps**:
  1. Quit OnionPress from menubar (full quit)
  2. `onionpress start` (Colima + containers needed for backup)
  3. `onionpress backup <wp-admin-password> <output.zip>`
  4. `ONIONPRESS_YES=true onionpress reset` (wipes volumes, keys, config, secrets; starts fresh containers)
  5. `onionpress restore <wp-admin-password> <backup.zip>` (imports DB, Tor keys, wp-content, config, re-adds multisite constants, starts Tor)
  6. Relaunch `/Applications/OnionPress.app`, verify purple
- **Restore multisite bug** (partially fixed in v2.4.37): After container recreation, wp-config.php loses multisite constants. The restore flow now force-adds them and starts Tor explicitly. See issue #122 for the proper fix (move backup/restore to Python).
- **Reset vanity key copy bug**: The `log` function's output contaminates `$VANITY_DIR` in the docker mount command. Reset still works; restore overwrites the key anyway.
- The backup password is the WordPress admin password, verified before proceeding

## Docker Image Builds
- **GitHub Actions workflow**: `.github/workflows/docker-publish.yml`
- **Images**: `ghcr.io/brewsterkahle/onionpress-tor:latest` and `ghcr.io/brewsterkahle/onionpress-wordpress:latest`
- **Manual trigger only** (`workflow_dispatch`) — run via `gh workflow run docker-publish.yml`
- **Multi-arch**: amd64 on GitHub runners, arm64 on self-hosted runner, then manifest merge
- **When to trigger**: after changes to `docker/tor/` or `docker/wordpress/` (entrypoint.sh, onionheaven-server.py, onionheaven-heartbeat.py, etc.)

## OnionHeaven Always-On (v2.4.37+)
- The `onionheaven` container is a **core service** — starts with every `docker compose up`, no profile gate
- `ONIONHEAVEN=1` is hardcoded in docker-compose.yml (was `${ONIONHEAVEN:-0}` with a fragile lazy activation watcher)
- The heartbeat monitor starts automatically; takeover workers start when registrations exist
- **op2pi** (Raspberry Pi OnionHeaven): `op2pieoieuwgwkgps4pz25rrh4mi4tolesyiduy7zrol2uh7luaxhwad.onion`

## Bash `wait` Gotcha
- **Bare `wait` (no arguments) waits for ALL background children** — including `tail -f` processes, background log watchers, etc.
- In scripts that spawn background monitoring processes, always capture PIDs and `wait "$pid"` on specific ones
- This caused `flush_client_descriptor_cache` to hang indefinitely (fixed in commit `2c36656`)
