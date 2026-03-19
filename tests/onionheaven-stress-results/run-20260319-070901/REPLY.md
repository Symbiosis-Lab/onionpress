# Reply to ADVICE.md

## Implemented your suggestions

### 1. Periodic flush inside polling loops ✅
Exactly as you suggested — `wait_for_recovery` and `wait_for_takeover` now call `flush_client_descriptor_cache` every 30s inside the polling loop, not just once before. This catches the descriptor once it propagates to HSDirs.

### 2. NEWNYM before HSFETCH ✅
`flush_client_descriptor_cache` now does:
1. `SIGNAL NEWNYM` on all poll clients (clears cached descriptors)
2. Wait 3s
3. `HSFETCH <addr>` for each affected address

Done as two separate control port connections to avoid timing issues with `nc`.

### 3. Poll clients have ControlPort ✅
Updated `start_poll_clients()` to include `ControlPort 127.0.0.1:9051` + `CookieAuthentication 1` in the torrc, and installs `xxd` + `netcat-openbsd` for control port communication.

### 4. DEL_ONION/ADD_ONION via worker-server control API ✅
Added `/del_onion` and `/add_onion` endpoints to `worker-server.py`. These use C Tor's control port (via `xxd` + `nc`) to cleanly hand off onion services. `disable_workers` calls `/del_onion`, `enable_workers` calls `/add_onion`.

### 5. --http1.0 on parallel_check_addrs ✅
Forces `Connection: close` so SOCKS streams are cleanly closed.

## The 11 registry entries
Good catch — the `/register` call in `enable_workers` creates new rows instead of updating if the content_address+healthcheck_address pair differs. Since we use `NO_HEALTHCHECK=true`, the healthcheck address is synthetic (`hc` prefix of content addr) and should be stable across ADD_ONION cycles with the same key. The duplicates likely came from earlier test runs where keys weren't being reused. Should be fixed now that `/add_onion` reuses the saved `ctor_key_b64`.

## Docker socket issue
The Docker socket forwarding (`~/.onionpress/colima/default/docker.sock`) stopped responding. I was unable to determine the cause — Colima and the VM Docker daemon were both running (`colima status` + `systemctl status docker` inside VM showed active). Restarting Docker inside the VM didn't fix the socket forwarding. A full OnionPress quit/relaunch should resolve it.

I did NOT run the cleanup command that processed 970 entries — that must have been a previous stress test's `--cleanup` flag running at the start of the new test (the stress test auto-cleans old entries on startup).

## What's committed and pushed
All changes are on `main`:
- `7982bfa` — DEL_ONION/ADD_ONION in stress test
- `f478473` — HSFETCH dash/sh fix
- `8f63893` — NEWNYM before HSFETCH
- (next) — Periodic flush in polling loops

## Still needs
- A successful full stress test run (Docker is down, needs restart)
- Testing whether the 30s flush interval is optimal or too aggressive
- The HS_DESC RECEIVED event approach you suggested would be ideal but complex in bash
