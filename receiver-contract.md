# OnionPress moss-receiver ↔ moss plugin — v1 wire contract (2026-07-21, updated for v1.2 2026-08-16)

Both sides implement EXACTLY this. Prototype defaults chosen to unblock a real end-to-end publish with no moss release and no new host-fns.

## Transport
- Plugin → receiver over plain HTTP on loopback. The plugin uses `execute_binary` to run `tar` and `curl` (the sanctioned escape hatch; the github plugin already uses execute_binary for git). NO moss-api HTTP host-fn is used for the tar upload — `execute_binary` shells out to `curl` directly instead.
- Base URL: `http://127.0.0.1:<port>/wp-json/onionpress/v1`
- Port discovery: the plugin probes `GET /status` on ports **8080, 18080, 28080, 38080, 48080** (OnionPress multi-user +10000 offsets). First port whose `/status` returns a JSON body containing `receiver_version` wins. No match → deploy fails with a "Start OnionPress first" toast.

## Generation id
- Plugin-generated: `moss-<unix_seconds>` (monotonic enough for v1; the receiver treats it as an opaque dir name and MUST reject path-traversal).

## Endpoints

### GET /status  (no body)
200 →
```json
{
  "onion_address": "<addr>.onion",
  "current_generation": "moss-1699999999" | null,
  "receiver_version": "1.2",
  "onion_reachable": true | false | null,
  "onion_http_code": "301" | "takeover" | "000:rc=28" | null
}
```
`onion_address` read from `/var/lib/onionpress/onion_address` (fallback: `status.json`.onion_address). `current_generation` = `basename(readlink(/var/www/html/site/current))` or null.

`onion_reachable`/`onion_http_code` (receiver_version 1.1+, moss#917) mirror `status.json`'s tri-state external-reachability check: `null` means the receiver hasn't completed a dual-probe Tor-network check since coming up (or is a pre-1.1 build that never sends the fields at all) — the plugin's `waitForReachability` gates on `receiver_version` before polling for exactly this reason. Only an explicit `false` means "checked and unreachable." Both are strings, never numbers — `onion_http_code` carries an HTTP status, the `"takeover"` sentinel (OnionHeaven hub takeover response), or a `"000:rc=<curl exit code>"` transport-failure sentinel.

### POST /generation?id=<genid>
Carrier depends on the receiver's advertised `receiver_version` (from `/status`):

- **`receiver_version >= 1.2` (current): `multipart/form-data`, tar (plain, not gzipped) of the generation dir CONTENTS at tar root in a part named `tar` — `curl -F tar=@<path> '<base>/generation?id=<genid>'`. Do NOT set `Content-Type` manually; curl generates the multipart boundary itself, and overriding it breaks the receiver's parser.
  - Why: WordPress's REST server calls `set_body(self::get_raw_data())` — `file_get_contents('php://input')` — for EVERY request, before our route callback (or even the permission callback) runs. For a large raw body that buffers the whole tar into a PHP string and can exhaust `memory_limit` before we ever get a chance to reject it. With `multipart/form-data`, PHP's `rfc1867.c` registers a NULL post-reader instead, so `php://input`/`get_raw_data()` is empty and the file streams straight to `upload_tmp_dir`. Symmetric win on the client: `curl -F` streams from disk at constant memory, `curl --data-binary` buffers roughly 2x the file into curl's own RSS.
  - Receiver: reads `$request->get_file_params()['tar']`, checks `$f['error'] === UPLOAD_ERR_OK` FIRST (rfc1867.c cancels an oversize part mid-write and reports `UPLOAD_ERR_INI_SIZE` rather than failing the request outright — an unchecked `is_uploaded_file()` would silently accept a truncated tar), then `is_uploaded_file()`, then `move_uploaded_file()` (or a checked streaming `fopen`/`stream_copy_to_stream`/`fclose` fallback for a cross-device tmp dir) into `/var/www/html/site-generations/<genid>.tar`.
  - `UPLOAD_ERR_*` → HTTP status: `UPLOAD_ERR_INI_SIZE`/`UPLOAD_ERR_FORM_SIZE` → 413 (names `upload_max_filesize`/`MAX_FILE_SIZE` in the message); `UPLOAD_ERR_PARTIAL`/`UPLOAD_ERR_NO_FILE`/unrecognised code → 400; `UPLOAD_ERR_NO_TMP_DIR`/`UPLOAD_ERR_CANT_WRITE`/`UPLOAD_ERR_EXTENSION` → 500.
- **`receiver_version < 1.2` (legacy fallback, still accepted by the receiver): `Content-Type: application/x-tar`, body = the raw tar.** Receiver writes the body (from `$request->get_body()`, since `WP_REST_Server` has already consumed `php://input` by the time our callback runs) to the same `<genid>.tar` path. Kept only so an old moss client talking to any receiver gets the SAME route and the receiver's existing loud 400 on an empty body, not a 404 mid-publish — do not remove until the raw-body branch itself is retired.

Either way, the tar is then extracted into `<genid>.tmp/`, then atomically `rename()`d to `<genid>/`; the tar is deleted after.
  - **Extraction: streaming tar reader, NOT `PharData`.** `PharData::extractTo` errors "Cannot extract '.'" on the `.` self-entry that `tar -cf x -C <dir> .` always produces — it fails on every real upload (empirically proven Track A). The receiver uses a hardened streaming reader that skips the `.` entry and runs the traversal/link guards inline, failing closed on anything not positively a regular file or directory. Per-file writes fail closed too: a short/failed `fread`, a short/failed `fwrite`, or a failed `fclose` (e.g. `ENOSPC`) fails the whole extraction rather than landing a silently truncated file.
- Path-traversal guard: reject any entry whose name contains `..`, starts with `/`, or is a symlink/hardlink. Reject if `<genid>` itself contains `/` or `..`.
- 200 → `{ "ok": true, "generation": "<genid>" }` ; 4xx/413/500 → `{ "ok": false, "error": "…" }`

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
