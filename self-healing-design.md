# Self-healing on end-to-end onion reachability — investigation + design

**Date:** 2026-08-16
**Origin:** investigation + external prior-art research, commissioned after the 2026-08-16 incident: the onion was end-to-end dead for ~9 hours while every watchdog signal read green (`serving=True` through 11 tor restarts); a `launcher restart` then fixed it in 22 seconds.
**Coordination note:** step 5's receiver v1.3 must ship together with the `status_updated_at` passthrough required by moss's publish=verified-live design (moss repo, `docs/archive/2026-08-16-publish-verified-live-design.md`, §3 step 9) — one 1.3 release carrying both fields.

All citations are to files in this tree at merged main `da0b41f4` unless noted. The design honors the constraints in the ops memory for this deployment: launcher-only stack operations, no hand-built compose env, stack updates replace the app wholesale, `172.19.0.0/16` is not reserved for the tunnel.

---

## Part 1 — Established facts

### 1.1 The tor-watchdog (`app/Resources/docker/tor/tor-watchdog.py`, runs inside `onionpress-tor`)

**What the four signals actually measure — none of them is end-to-end:**

| Signal | Source | What it proves |
|---|---|---|
| `bootstrapped` | `BOOTSTRAP`/`Bootstrapped` events + `GETINFO status/bootstrap-phase` (tor-watchdog.py:810-823, 1061-1069) | tor once reached 100% (latched; known-stale after sleep, per the file's own comment at :57-62) |
| `circuit-established` | `GETINFO status/circuit-established` per pass (tor-watchdog.py:876-877) | tor *believes* it has ≥1 open circuit |
| `services_active` | ADD_ONION returned 250 (tor-watchdog.py:318-350, set at :1073-1075) | the control command succeeded — nothing about the network |
| `descriptor_published` | a `650 HS_DESC UPLOADED` event arrived since the last recovery arming (tor-watchdog.py:828-839) | **one HSDir accepted one POST** — not that clients can fetch it, and not that the intro points in it are alive |

`is_serving()` (tor-watchdog.py:532-549) is a pure conjunction of those four. There is no fetch of anything, ever. That is exactly why `serving=True` held while the onion was dead: during the incident tor kept bootstrapping to 100% through obfs4/Veee, kept `circuit-established=1`, ADD_ONION succeeded, and HS_DESC UPLOADED events fired — while real rendezvous was impossible. Verified in the captured log `~/.onionpress/logs/container-tor-2026-08-16-001.log`: 18 heartbeat lines `alive — bootstrapped=True, circuit-established=1, services_active=True, serving=True` during the dead window, the last at 07:54:47Z — four minutes before the launcher restart that actually fixed it.

**The healthcheck onion (px4f…)** is the second service in `/etc/tor/onion-services.json` (entrypoint.sh:425-430), mapping onion:80 → 127.0.0.1:8081, served by `healthcheck-server.sh`. Its GET handler (healthcheck-server.sh:55-142) verifies only `wget http://wordpress:80/` **over the Docker bridge** plus local files. It measures WordPress-behind-Apache, never the Tor path. Nothing local fetches it *through Tor* on a health-gating cadence — its Tor-side consumers are the OnionHeaven hub's post-takeover audits. So "the healthcheck onion passed" means only that its ADD_ONION succeeded and WP answered on the LAN; it shares every blind spot of the wordpress onion.

**Today's escalation ladder** (all in tor-watchdog.py):

- Event-driven: `DROPGUARDS`+`NEWNYM` on guard failures/exhaustion (:472-499, :785-799); HS_DESC-stall → DEL_ONION+ADD_ONION republish 60s after a recovery arming with no UPLOADED (:916-968).
- Time-driven off `not_serving_since` (`next_escalation`, :552-587): PT kill+RELOAD at 180s (:634-667), `SIGNAL HALT` → docker restart-policy revival at 420s with a 900s cooldown (:670-689), and **DEGRADED** — stop climbing — after `DEGRADED_AFTER_RESTARTS = 3` restarts inside a **sliding 3600s window** (:79-80, :567-569).
- **Where it stops:** `do_degrade` (:691-704) writes a log line and the state file `/var/lib/onionpress/watchdog-state.json` (:707-735) — and that is the end. `grep -rn watchdog-state` across the repo finds **no reader**; the escalation terminates into a file nobody consumes.

**Why 11 restarts instead of 3 — the ledger reset bug.** When `is_serving()` flips true, the watchdog clears the anti-flap ledger: `state.degraded = False; state.tor_restarts = []` (:899-901). Because the serving verdict is the lying local one, every post-restart bootstrap produced a false "Serving again" (log: `Serving again after 289s`, 00:25:49Z) that wiped the restart stamps. Additionally, restarts spaced by the 900s cooldown plus long climb times land ~45 min apart, so the *sliding* 3600s window rarely accumulated 3. The log shows 9 `LAST RESORT … SIGNAL HALT` events (02:14 → 06:54Z) before three finally landed within one hour and DEGRADED latched: `06:54:20 DEGRADED: not serving for 0s after 3 Tor restarts` ("0s" because the freshly restarted watchdog resumed with 3 persisted stamps via `load_restart_history` (:738-762) and degraded on its first not-serving pass).

**Recurring warn decoded:** `Rejecting SOCKS request for anonymous connection to private address` is tor's private-address guard tripping on *outbound* WordPress traffic — `onionpress-tor-proxy.php` forces the whole WP HTTP API through `socks5h://onionpress-tor:9050` (app/Resources/plugins/onionpress-tor-proxy.php:64-110), and wp-cron periodically requests a host resolving privately. 29 occurrences today. It is a red herring for reachability.

### 1.2 The receiver's "end-to-end check" — it isn't the receiver's

`onionpress-moss-receiver.php` computes nothing. `/status` (app/Resources/plugins/onionpress-moss-receiver.php:405-416) returns `onion_reachable`/`onion_http_code` read from `/var/lib/onionpress/status.json` (:110-124), which the **Mac menubar app** writes each status cycle via `docker exec … tee` (src/menubar.py:4773-4915, reachability tuple at :4818, 4892-4893). The real probe is `HealthChecker.check_external_reachability` (src/onionpress/health.py:347-397):

- **Dual probe**: first via the `onionheaven` container's *independent* tor (`_probe_onion_via`, health.py:277-319 — `curl -s --max-time 30 --socks5-hostname 127.0.0.1:9050 http://<onion>/`, one docker exec, 45s exec timeout); on failure, self-probe via `onionpress-tor`'s own SOCKS; on self✓/ext✗ disagreement, disambiguate by fetching the OnionHeaven hub onion through onionheaven's tor (:331-345, cached 60s).
- 200/301 = reachable; **302 or an `X-OnionHeaven-Takeover: 1` header = NOT reachable** (`http_code="takeover"`, :313-318) — so a takeover serving Wayback for our address correctly reads unreachable. Transport failure → `"000:rc=<curlexit>"` (rc=28 = 30s timeout; map at health.py:23-32).
- **Cadence**: every check_status cycle — 30s when green, 10s offline, 5s starting/stuck (src/menubar.py:2660-2678). Cleared to `(None, None)` on stop/offline so it never reports stale (:1704, 2019). Tri-state: `null` until Check 5 has actually run (health.py:59-68).
- Self-fetch validity is already confirmed in-repo: "C Tor routes self-connections through real circuits rather than a local shortcut" (health.py:321-329).

During the incident this probe **had the truth for hours** (`onion_reachable:false`, `000:rc=28`) — and it feeds only status.json for display. No actor consumes it for recovery.

### 1.3 The Mac-side supervisor — observes everything, restarts nothing

`check_status` (src/menubar.py:1596-2035) runs container status (`launcher status` → `docker compose ps`, app/MacOS/onionpress:1592-1596), the reachability check, and writes status.json. Its automatic actions today: **none for tor**. The auto-restart that used to exist was removed — the comment remains: *"Watchdog inside tor container handles recovery via control port"* (src/menubar.py:1865-1871). What exists:

- A one-shot **log-only** wedge warning after 10 min yellow (:1606-1623).
- The autoheal sidecar restarts **onionpress-wordpress only**, on its Docker healthcheck (`/wp-login.php`, 60s×15 ≈ 15 min) — docker-compose.yml:59-77, 111-132. The tor container has no Docker healthcheck at all.
- `HealthMonitor.should_restart_tor` (health.py:685-711) exists and is wired **only on Linux** (linux/onionpress-service.py:504-506 → `docker restart onionpress-tor`), and its gate `tor_container_unhealthy()` (health.py:487-519) returns False whenever "Bootstrapped 100%" appears in the last 50 log lines — the incident's permanent condition. So even the Linux path would have sat out this outage.
- Manual "Restart" menu item → `launcher restart` (src/menubar.py:3752-3794). The launcher's `restart` (app/MacOS/onionpress:2330-2363) is adaptive: if the Docker daemon is unreachable it force-restarts the Lima VM first, then `update_images; stop_containers; start_containers; wait_for_services`. **This is the code path that fixed a 9-hour outage in 22 seconds** — it recreates containers *and* networks (today's recreate also re-triggered the VM socat bind: `~/.onionpress/veee-tunnel.log` shows `bind … 172.19.0.1:15235: Cannot assign requested address` at 15:58 until `docker_default` came back up, plus all-day `Broken pipe` errors through the tunnel — evidence the launcher-level recreate cycles state that in-container tor restarts never touch). `onionpress-network` currently sits on `172.18.0.0/16`; the tunnel binds the `docker_default` gateway at 172.19.0.1, exactly as the ops memory warns.

### 1.4 onionheaven / hub interplay

The hub runs on this same Mac (`onionheaven` container + `onionheaven-takeover-*` workers). Takeover is **heartbeat-absence driven**, not probe driven: instances POST `/online` every 60s (src/onionpress/onionheaven.py:216-277); the monitor (app/Resources/docker/tor/onionheaven-heartbeat.py:381-415) takes over when `last_healthy` is older than `PROPAGATION_DELAY` = 180s (onionheaven_common.py:33; compose default `ONIONHEAVEN_PROPAGATION_DELAY:-180`, docker-compose.yml:155). Safety valves that matter for restarts:

- **Post-takeover audit**: 10–300s after takeover the hub probes the *healthcheck* onion; alive → `false_positive` → automatic release (onionheaven-heartbeat.py:459-467). Past 300s → `confirmed_dead`.
- **Reclaim**: the next `/online` heartbeat from the origin releases the takeover; an online sibling row supersedes a stale takeover (:437-445). The menubar deliberately starts heartbeats as soon as tor bootstraps, before "purple", because "the heartbeat IS the reclaim mechanism" (src/menubar.py:1943-1957).
- **Hub restart** runs `startup_reconciliation` which resets every online entry's grace to now (onionheaven-heartbeat.py:95-217) — no thundering-herd takeover from bouncing the hub.

**Verdict on bouncing the origin during a takeover window:** safe. A quick (<180s) origin restart doesn't trigger a takeover; if one is already active, the takeover keeps serving visitors (Wayback fallback proven end-to-end 2026-08-14) while the origin restarts, and reclaim is automatic on the next delivered heartbeat. The real cost is a *descriptor race* — takeover workers publish descriptors for our address, so after reclaim the origin must republish (the DEL+ADD path already exists) and clients may hold the takeover descriptor until their caches expire or NEWNYM. Conclusion: healing restarts must not be vetoed by takeover; they must (a) not *count* the takeover-serving window as "still dead" and (b) force a republish after reclaim.

### 1.5 Ship-path realities

- **Watchdog change** = tor image rebuild: `tor-watchdog.py` is baked in (Dockerfile:64). The fork builds it via `.github/workflows/fork-tor-image.yml` — triggers only on push to `feat/fork-tor-image` or `workflow_dispatch` (:20-36), builds amd64+arm64 natively, merges the manifest, and prints the digest to pin (:110-127). The pin lives in `docker-compose.yml:3` (tor) **and** `:135` (onionheaven default) — both must be bumped. The compose file ships inside the app bundle, so a pin bump also requires a DMG release.
- **Menubar/launcher change** = DMG only: `build/build-dmg-simple.sh` → `build/release.sh` (BUILD-FORK.md:78-128). (BUILD-FORK.md:8's "you do not build or host any Docker images" predates fork-tor-image.yml and is stale for watchdog work.)
- **Receiver change** = DMG (provision-time injection) + `install-receiver-live.sh` for the running stack.
- **Reaching an installed stack durably**: moss pins the release in `plugins/onionpress/stack-manifest.json` (currently `v2.4.110-moss.1`). A stack update replaces `OnionPress.app` wholesale (2026-08-14 regression), so the fix is real only once it's in a tagged fork release **and** the manifest bump. Repo version is currently `2.4.110` (src/menubar.py:234).

---

## Part 2 — External prior art (researched before designing)

- **onionprobe** (Tor Project) is the canonical answer to exactly this failure mode: per target it does a client-side descriptor fetch (Stem `HSFETCH` + `HS_DESC RECEIVED/FAILED` events) *and* a real HTTP fetch through SOCKS, exporting **separate** Prometheus metrics for descriptor reachability vs HTTP reachability (`onion_service_descriptor_reachable` vs `onion_service_reachable`, plus a failure taxonomy: connection_error/timeout/http_error/…). Defaults: 60s intervals, 30s timeouts, 5 descriptor / 3 HTTP retries; runs its own tor by default or attaches to an existing one; deployable as a container with a Prometheus/Alertmanager stack. Directly reusable: the two-layer probe recipe and the taxonomy. (docs: https://onionservices.torproject.org/apps/web/onionprobe/)
- **onionbalance v3** judges backend health purely by **descriptor freshness from the HSDir hashring** (`INSTANCE_DESCRIPTOR_TOO_OLD = 3600` etc. in `hs_v3/params.py`) and never dials intro points — it would *not* catch our wedge. Reusable idea: "descriptor observable in the DHT and recent" as a distinct health layer. (design: https://onionservices.torproject.org/apps/base/onionbalance/design/)
- **vanguards** (bandguards) subscribes to `CIRC CIRC_MINOR ORCONN NETWORK_LIVENESS GUARD …` and WARNs when live guard ORCONNs hit zero for `CONN_MAX_DISCONNECTED_SECS=15` or all circuits fail for 30s — cheap transport-death detection that fires during upstream-proxy/bridge path death, but nothing end-to-end. Reusable: the ORCONN-liveness alarm as a fast *leading* indicator. (repo: https://github.com/mikeperry-tor/vanguards)
- **EOTK/Onionspray**: the production toolkit's monitoring guide is tor `MetricsPort` + logs for internals and — for reachability — "run onionprobe externally". Nobody in EOTK-land trusts local green lights either. (guide: https://tpo.pages.torproject.net/onion-services/onionspray/guides/monitoring/)
- **Tor daemon signals**: `HS_DESC UPLOADED` proves one HSDir accepted one POST — client fetchability additionally requires consensus agreement on the hashring (the Jan-2021 network-wide "services upload fine, clients can't reach anything" outage) and *live intro points*; a descriptor full of dead intro circuits uploads perfectly. Service intro health is visible via `GETINFO circuit-status` lines `PURPOSE=HS_SERVICE_INTRO HS_STATE=HSSI_ESTABLISHED` — but `HSSI_ESTABLISHED` is only C-Tor's belief, maintained by the absence of a close event; when the path dies uncleanly (NAT/proxy/bridge — our Veee topology), tor holds "established" intro circuits no INTRODUCE2 will ever traverse. This is a documented C-Tor failure class (tor#19522, trac#8864) whose accepted remediation is **restart/rebuild the service**. `SIGNAL NEWNYM` is client-scoped (purges client HS descriptor cache + circuits; never touches service intro circuits). **SIGHUP provably does not help**: `hs_service.c` *moves* descriptors and intro state across a reload. DEL_ONION+ADD_ONION destroys and recreates the service — fresh intro circuits + republish — making it the cheapest service-side rebuild, one rung below a tor restart.
- **Self-fetch validity**: C-Tor has no self-shortcut — client and service subsystems are disjoint and the rendezvous point is always a remote relay, so a SOCKS self-fetch is a genuine full rendezvous (matches health.py:321-329's timing evidence). Caveats: same guards/bridges/proxy and same consensus for both legs (good for detecting our wedge, useless for *attributing* it — hence the control-onion discriminator), and the client descriptor cache must be busted (NEWNYM or HSFETCH) after a republish. Independent-daemon probing is the ecosystem norm — which we already have for free in the `onionheaven` container.

---

## Part 3 — The design

**One sentence:** make the serving verdict a *measured* one (a real Tor-routed self-fetch inside the watchdog), stop the false-green from resetting the anti-flap ledger, and extend the ladder past `degraded` into the actor that owns the proven fix — the Mac-side supervisor running `launcher restart` — with an explicit handoff protocol so the two actors never fight.

### 3.1 Detection

**Probe A — in-container end-to-end self-fetch (the new serving input).** In `tor-watchdog.py`, a pure-stdlib SOCKS5h fetch (raw socket handshake to `127.0.0.1:9050`, hostname-mode CONNECT to the wordpress onion, `GET / HTTP/1.0`, read status line + headers) — no curl subprocess, consistent with the watchdog's no-external-deps rule (:609-615). Parameters (all env-overridable for tests):

- `E2E_PROBE_INTERVAL_OK = 180s`, `E2E_PROBE_INTERVAL_BAD = 60s`, `E2E_PROBE_TIMEOUT = 45s` (cold rendezvous through obfs4+Veee measured 8–16s; 30s timeouts were the incident's failure signature, so 45 avoids flapping on slow-but-alive).
- Gated on: services discovered, `services_active`, not `sleeping`, local signals green (when local signals are already red the existing ladder is running; the probe adds nothing but load).
- `E2E_FAIL_THRESHOLD = 3` consecutive failures ⇒ `e2e_ok=False`. One success ⇒ `e2e_ok=True` (asymmetric on purpose: a verified 200 is conclusive). Tri-state `None` until the first probe completes — never blocks serving before evidence exists (mirrors the moss#917 convention, health.py:59-68).

**Verdict taxonomy on failure** (runs once per confirmed-down transition, then per bad-interval pass):

| Test | Result | Verdict | Meaning |
|---|---|---|---|
| control-onion fetch (hub address, already known in-code health.py:269-271; fallback DDG onion) through same SOCKS | fails | `network` | our tor can't reach *any* onion — transport/tunnel path dead; HS-layer rungs are pointless |
| control fetch OK, `HSFETCH <own addr>` → `HS_DESC FAILED REASON=NOT_FOUND…` | | `descriptor` | descriptor not fetchable from the DHT — republish rung |
| control OK, HSFETCH → `RECEIVED`, self-fetch still fails | | `intro-wedge` | the tor#8864/#19522 class: descriptor fine, intro circuits dead — rebuild rung |
| HTTP response arrives with `X-OnionHeaven-Takeover: 1` or 302 | | `takeover` | someone (our hub) is serving our address — do **not** restart; reclaim path |

Probe B — the existing Mac-side dual probe (`check_external_reachability`) stays exactly as is; it becomes the **escalation authority's independent evidence** (it uses onionheaven's separate tor daemon, so it distinguishes "our tor lies" from "network down" — prior-art's independent-prober norm, already built).

### 3.2 Escalation ladder, two actors, anti-flap

**Actor 1 — watchdog (in-container), amended ladder:**

- `is_serving()` gains the e2e input: `serving = local_green AND e2e_ok is not False`.
- **Ledger fix (the 11-restart killer):** clear `tor_restarts`/`degraded` (:899-901) only when serving is *probe-confirmed* — i.e. at least one `e2e_ok=True` observed since the last restart action. Local-green alone logs `locally green, awaiting end-to-end confirmation` and clears nothing.
- **Degraded accounting fix:** count restarts **per outage** (stamps ≥ `not_serving_since`), not only the sliding hour: `restarts_this_outage >= 3 ⇒ handoff`. Keep the 3600s window as an additional cap. (Today's 45-min-spaced restarts slid past the window for 4.5 hours.)
- Verdict-directed rungs: `takeover` → no restart at all; ensure `services_active`, DEL+ADD republish once, keep heartbeating (reclaim is the cure), report state `reclaiming`. `descriptor`/`intro-wedge` → DEL+ADD rebuild first (destroys/recreates the ephemeral service: fresh intro circuits + republish — cheapest fix for the wedge class), then the existing PT-restart / `SIGNAL HALT` rungs. `network` → skip DEL+ADD (useless), go PT-restart → HALT. After any DEL+ADD, `SIGNAL NEWNYM` before the next probe (client cache holds the pre-republish descriptor — the 2026-08-14 lesson).
- **Handoff instead of dead end:** `do_degrade` now also writes `escalate_to_host: true` + `handoff_reason` into watchdog-state.json and *stays quiescent* (no further restarts) until either probe-confirmed serving or a fresh container start. The persisted stamps already make a freshly-recreated watchdog defer within one pass (today's `not serving for 0s after 3 Tor restarts` line becomes the designed behavior: the container instance the *host* just restarted immediately yields authority back).

**Actor 2 — Mac-side supervisor (new `src/onionpress/self_heal.py`, driven from `check_status`):** acts only when **all** hold:

1. Its own independent verdict agrees: `onion_reachable == False` for ≥2 consecutive cycles (the existing debounce, src/menubar.py:1824-1849) with `http_code != "takeover"` — takeover means visitors are being served; healing is reclaim, not restart.
2. The watchdog has yielded: watchdog-state.json (read via one `docker exec cat` per cycle) has `escalate_to_host: true`, **or** is stale >120s / unreadable / container not running (watchdog dead — the host is the only actor left).
3. No publish in flight (receiver busy check), not `_stopping`/`_quitting`, outside the settle window.

Host rungs, in order:

- **H1 — tunnel triage (fork-only, diagnosis-first):** probe the two proxy legs — host: TCP+SOCKS5 handshake `127.0.0.1:15235` → a bridge IP from `TOR_BRIDGE_LINES`; container leg: `docker exec onionpress-tor` stdlib probe via the *configured* `TOR_UPSTREAM_PROXY` value (172.19 is not guaranteed). Host-leg OK + container-leg dead ⇒ `launchctl kickstart -k gui/$UID/com.onionpress.veee-tunnel`, wait 20s, re-probe (this is exactly the relaunch the plist's `KeepAlive` already performs on crash). Host-leg dead ⇒ **Veee itself is down — nothing automated can fix a GUI VPN**; skip all restart rungs, report `verdict: tunnel-upstream-down`, and notify. This rung is what would have turned today's 9 hours of blind restarts into a correct diagnosis: the tunnel was the sick layer (all-day `Broken pipe` in veee-tunnel.log).
- **H2 — full launcher restart:** `subprocess.run([launcher, "restart"])` — the proven 22-second fix, already adaptive to a wedged VM (app/MacOS/onionpress:2330-2363). Reuses the menubar's own restart plumbing (src/menubar.py:3759-3793).
- **Give-up:** after the budget, state `given_up` with the last verdict; probing continues; the first probe-confirmed green clears everything. Visitors are meanwhile covered by the hub takeover → Wayback chain (proven end-to-end 2026-08-14). Emit a one-shot user notification (pattern of the wedge warning, src/menubar.py:1611-1623).

**Anti-flap rules (explicit):**

- Probe: 3 consecutive failures spanning ≥3 min to declare down; 1 success to declare up.
- Watchdog: existing cooldowns unchanged; ≤3 restarts per outage, then quiescent handoff. No rung ever climbs back down (existing invariant, tor-watchdog.py:576-580, kept).
- Host: min spacing between H2 actions **45 min + uniform 0–10 min jitter**; budget **2 launcher restarts per rolling 6 h**, persisted in `app_support` so an app relaunch can't reset it (the watchdog's own persistence lesson, :738-762). H1 tunnel kick: max 1 per 30 min.
- **Settle window:** 15 min after *any* restart action by *either* actor (host reads `tor_restart_stamps` from watchdog-state to see the watchdog's; writes its own action stamps into status.json) during which neither actor takes a heavier action — post-restart descriptor propagation is minutes, and restarting into it is how self-healing becomes self-harm.
- Mutual exclusion is structural: the host acts only when the watchdog has yielded or died; a fresh watchdog defers back within one pass via persisted stamps. Worst case per 6 h: 3 watchdog restarts + 1 tunnel kick + 2 launcher restarts, then honest quiescence — today's 11 can never become 22.

### 3.3 Observability

- **Watchdog log lines** (structured, greppable): `e2e-probe ok=1 code=200 ms=8542`; `e2e-probe ok=0 stage=rendezvous code=timeout streak=2/3`; `verdict=network (control-onion fetch failed)`; `HANDOFF: 3 restarts this outage changed nothing — requesting host escalation (verdict=network)`; `serving CONFIRMED end-to-end (200 in 8.5s)` replacing the trust-based `Serving again`.
- **watchdog-state.json additions** (existing writer :707-735): `e2e_ok` (bool|null), `e2e_code`, `e2e_verdict`, `e2e_checked_at`, `e2e_fail_streak`, `restarts_this_outage`, `escalate_to_host`, `handoff_reason`.
- **status.json additions** (menubar `write_status_to_volume`, src/menubar.py:4884-4901): a `healing` object — `{state: ok|watchdog_recovering|awaiting_host|tunnel_kicked|host_restarting|reclaiming|given_up, verdict, watchdog_rung, host_attempts_6h, last_action, last_action_at, next_eligible_at}` — alongside the existing `onion_reachable`.
- **Receiver `/status`**: bump `receiver_version` to `1.3` and pass `healing` through from status.json (same read pattern as reachability, onionpress-moss-receiver.php:110-124). moss's plugin already polls `/status`, so the publish UI can truthfully render "your service is recovering — automatic restart 2 of 2 scheduled at HH:MM" or "recovering via archive takeover; reclaim in progress". Field additions are cheap and tri-state-null for old app versions.

### 3.4 Upstreamability

- **Upstream-clean** (mergeable to brewsterkahle/onionpress): the entire watchdog probe/verdict/ledger/handoff work (pure stdlib, localhost SOCKS + control port — no fork assumptions); the host supervisor module and its wiring (menubar + `linux/onionpress-service.py`, which shares `health.py` and gets H2 as `systemctl`/`docker compose` restart); the receiver `healing` field; the `tor_container_unhealthy` gate fix (its "Bootstrapped 100% ⇒ healthy" assumption is disproven by this incident and should defer to the e2e verdict).
- **Fork-only, kept separate**: the Veee tunnel rung — own module (`src/onionpress/tunnel_fork.py`) activated only by config keys (`TUNNEL_LAUNCHD_LABEL`, `TUNNEL_PROXY_ADDR`); absent config = rung silently skipped. Never in the upstream PR. The control-onion default (hub address) is already generic in-repo.

### 3.5 Test plan

- **Python unittest (runs everywhere):** extend `tests/test_tor_watchdog.py` (existing import-by-path + `FakeSock` harness, :17-49): probe function against an in-process fake SOCKS5 server (thread; scripted success / connect-refused / timeout / 302 / takeover-header responses); verdict matrix (self × control × HSFETCH outcomes); `is_serving` tri-state; **the incident as a regression test** — all four local signals green + probe failing ⇒ serving False, ladder climbs, ledger survives local-green, handoff after 3 restarts, `escalate_to_host` written; per-outage vs sliding-window counting. Extend `tests/test_onionpress_health.py`: host decision table (agreement gate, stale/degraded/absent watchdog state, budget + spacing + jitter bounds, settle window, publish-veto, takeover-veto, tunnel-triage branches with mocked subprocess). Prune superseded assertions per the standing prune rule (e.g. any test asserting `Serving again` clears stamps unconditionally).
- **Real stack (this Mac, guarded):** the local-green/end-to-end-dead simulator needs no network breakage — set `E2E_PROBE_ADDRESS_OVERRIDE=<random valid-format .onion>` in `~/.onionpress/config`: the watchdog probes a nonexistent service while the stack is genuinely healthy, reproducing the incident's signal pattern exactly. With test-shortened env timings, walk the full journey: 3 fails → verdicts (`descriptor` — HSFETCH NOT_FOUND) → DEL+ADD → restarts → handoff → host agreement (the host's independent probe of the *real* address stays green, so also test the host side with the override mirrored) → budget → give-up → remove override → confirmed recovery. Takeover verdict: reuse the proven throwaway-identity takeover drill (2026-08-14) and point the probe at the taken-over address. Tunnel triage: probe functions against the live 15235 legs (read-only), one supervised `kickstart -k` verification.
- **Not e2e-able on hel**: the stack is Mac-bound (colima, launchd, Veee). The Mac drill above is the e2e gate for this feature; document it as a checklist.

### 3.6 Implementation plan (ordered, agent-sized)

1. **Watchdog** (`app/Resources/docker/tor/tor-watchdog.py` + `tests/test_tor_watchdog.py`): env-tunable constants; SOCKS5h probe + control-onion + HSFETCH taxonomy; `is_serving` e2e input; ledger + per-outage degraded fixes; verdict-directed rungs incl. takeover no-restart; handoff fields; state-file additions. Also check the doubled `LAST RESORT` log lines seen today (same-second duplicates — likely duplicate log capture, but rule out a double `check_stalls` path at :1161/:1198 while in there). *Largest piece; pure Python; fully unit-testable.*
2. **Tor image release**: push the tor/ change → `workflow_dispatch` fork-tor-image.yml → pin the printed digest at docker-compose.yml:3 **and** :135.
3. **Host supervisor** (`src/onionpress/self_heal.py`, wiring in `src/menubar.py` check_status + `write_status_to_volume`, `src/onionpress/health.py` gate fix, `linux/onionpress-service.py` H2 wiring; `tests/test_onionpress_health.py`): decision engine, budget persistence, healing object.
4. **Fork-only tunnel module** (`src/onionpress/tunnel_fork.py` + config keys) — separate commit, excluded from the upstream PR branch.
5. **Receiver v1.3** (`app/Resources/plugins/onionpress-moss-receiver.php`): `healing` passthrough **plus** the `status_updated_at` passthrough required by moss's publish=verified-live design — one coordinated 1.3 bump; update moss's `plugins/onionpress/receiver-contract.md` + types + UI copy (moss repo, separate task).
6. **Release + rollout**: `build/bump-version.sh 2.4.111` → `build/release.sh` → moss `stack-manifest.json` bump to the new `-moss.` tag. The fix **must ride the next -moss DMG** — the deployed app is replaced wholesale on stack update, so nothing hand-patched survives (2026-08-14). Live verification on this Mac afterwards via the launcher only, using the §3.5 override drill.
7. **Upstream PR** to brewsterkahle/onionpress: steps 1+3+5 minus step 4, once soaked locally.

**What today would have looked like with this design:** ~00:20 probe declares down (3×45s failures); verdict `network` (control onion unreachable through the broken tunnel); watchdog spends ≤3 restarts by ~01:30 and hands off; host agrees (its independent probe had `rc=28` all along), tunnel triage finds the container leg dead through half-alive socat, kicks the launchd tunnel; if still dead, one launcher restart — the action that empirically fixes it — lands before 02:00 instead of 15:58, with `/status` truthfully reporting `healing` the whole way.
