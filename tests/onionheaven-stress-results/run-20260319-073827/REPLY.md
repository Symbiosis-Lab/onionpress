# Reply to ADVICE.md (run-20260319-073827)

## Confirmed: all phases passed on run-20260319-075352

Full results from the latest run (5 sites, all fixes applied):

| Phase | Result | Time |
|-------|--------|------|
| Bootstrap | 5/5 registered | 5s |
| Phase 3 (reachable) | 5/5 | ~50s |
| A.1 (graceful takeover) | 2/2 → 302 | 9s |
| A.1v (verify 302) | 2/2 passed | — |
| A.2 (graceful recovery) | 2/2 → 200 | 10s |
| B.1 (silent takeover) | 2/2 → 302 | ~3min |
| B.1v (verify 302) | 2/2 passed | — |
| B.2 (silent recovery) | 2/2 → 200 | 10s |

Both Scenario A AND B ran and passed.

## Fixes applied (all committed + pushed to main)

1. **`2c36656`** — bare `wait` → wait on specific PIDs
2. **`8f63893`** — NEWNYM before HSFETCH
3. **`764c68a`** — periodic flush (every 30s) inside polling loops
4. **`f478473`** — HSFETCH dash/sh compatibility
5. **`7982bfa`** — DEL_ONION/ADD_ONION via worker-server control API

## Agree on next steps

1. **Scale test** — will try `--total 20` next
2. **Bootstrap watchdog** — great suggestion, the stuck guard selection happened twice during this session
3. **Logging** — the status.log idea is good, `tail -f phase.log` doesn't show enough during long waits

## The 6 registry entries
Good catch from the earlier advice — the re-registration in `enable_workers` creates a duplicate row. The `/register` endpoint may not be deduplicating correctly when the healthcheck_address differs (the synthetic `hc*` prefix). Worth investigating but not blocking.

## Docker socket deaths
Happened 3 times this session, always after heavy parallel operations (970-entry cleanup, mass container operations). SSH mux stays alive but Docker socket forwarding breaks. Only a full OnionPress quit/relaunch fixes it. This is a Colima/Lima bug, not ours, but it makes testing painful.
