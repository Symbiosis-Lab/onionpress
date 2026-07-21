# OnionPress moss-receiver ↔ moss plugin — v1 wire contract (2026-07-21)

Both sides implement EXACTLY this. Prototype defaults chosen to unblock a real end-to-end publish with no moss release and no new host-fns.

## Transport
- Plugin → receiver over plain HTTP on loopback. The plugin uses `execute_binary` to run `tar` and `curl` (the sanctioned escape hatch; the github plugin already uses execute_binary for git). NO moss-api HTTP host-fn is used for the binary upload (moss-api has only multipart; raw `php://input` tar is simpler and matches the plan).
- Base URL: `http://127.0.0.1:<port>/wp-json/onionpress/v1`
- Port discovery: the plugin probes `GET /status` on ports **8080, 18080, 28080, 38080, 48080** (OnionPress multi-user +10000 offsets). First port whose `/status` returns a JSON body containing `receiver_version` wins. No match → deploy fails with a "Start OnionPress first" toast.

## Generation id
- Plugin-generated: `moss-<unix_seconds>` (monotonic enough for v1; the receiver treats it as an opaque dir name and MUST reject path-traversal).

## Endpoints

### GET /status  (no body)
200 →
```json
{ "onion_address": "<addr>.onion", "current_generation": "moss-1699999999" | null, "receiver_version": "1" }
```
`onion_address` read from `/var/lib/onionpress/onion_address` (fallback: `status.json`.onion_address). `current_generation` = `basename(readlink(/var/www/html/site/current))` or null.

### POST /generation?id=<genid>   Content-Type: application/x-tar
- Body = raw tar (plain, not gzipped) of the generation dir CONTENTS at tar root (files like `index.html`, `assets/…` at top level — created with `tar -cf x.tar -C <gendir> .`).
- Receiver writes body from `php://input` to `/var/www/html/site-generations/<genid>.tar`, extracts via `PharData` into `<genid>.tmp/`, then atomically `rename()`s to `<genid>/`. Deletes the tar.
- Path-traversal guard: reject any entry whose name contains `..`, starts with `/`, or is a symlink/hardlink. Reject if `<genid>` itself contains `/` or `..`.
- 200 → `{ "ok": true, "generation": "<genid>" }` ; 4xx `{ "ok": false, "error": "…" }`

### POST /commit   Content-Type: application/json
- Body `{ "generation": "<genid>" }`.
- Collision guard: reject if any top-level name in the generation collides with a reserved name {`wp-admin`,`wp-content`,`wp-includes`,`wp-json`,`wp-login.php`,`wp-cron.php`,`xmlrpc.php`,`site`,`site-generations`} or with an existing subsite path (from `$wpdb->blogs`).
- Atomic flip: `symlink(site-generations/<genid>, site/current.tmp-<uniqid>)` then `rename()` over `site/current` (atomic on same fs).
- GC: keep the newest 3 generations, never deleting the current target.
- 200 → `{ "ok": true, "url": "http://<onion_address>/" }`

## Localhost trust (all three endpoints, shared permission_callback)
- Deny if `REMOTE_ADDR` resolves to the tor container (`gethostbyname('onionpress-tor')`) or `onionheaven`.
- Deny if any `HTTP_X_FORWARDED_*` header is present (the onion serving path sets none; presence = spoof/misconfig).
- Precedent: `onionpress-auto-login.php` (local machine = trusted).

## Deferred to ship (NOT in v1 prototype) — surfaced as forks in the report
- Multi-site: v1 serves ONE moss site at the network root. Per-subsite namespacing changes the /generation API and is a separate decision.
- Shipped tar transport: an `archive_dir` host-fn would be the fix-at-root replacement for execute_binary(tar+curl); needs a moss release.
- Port via `onionpress status` CLI JSON instead of probing (upstream change).
- REST vs the repo's raw parse_request style (upstream review preference).
