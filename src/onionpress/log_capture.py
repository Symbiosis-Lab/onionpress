"""Container-log capture cursor: exact reattach without duplicate lines.

The menubar's capture workers follow ``docker logs -f`` and reattach with
``--since`` when the stream breaks (container restart, daemon hiccup).
The original cursor was the *host wall clock at second granularity*, and
``--since`` is inclusive — so every line docker recorded during the
boundary second was re-read verbatim on each reattach. That is what
doubled the LAST RESORT blocks in ``container-tor-2026-08-16-001.log``
during the 2026-08-16 outage (same-second duplicate log forensics).

Fix: run ``docker logs`` with ``--timestamps`` and cursor on docker's own
per-line RFC3339Nano token. Reattach passes that exact token to
``--since``; docker then re-sends only lines *at* that instant, and the
cursor skips exactly the number of token-identical lines it already
wrote. Equality on the literal token needs no timestamp parsing (Go
trims trailing zeros, so lexicographic or datetime comparison would both
be wrong — but docker parses its own token back losslessly).
"""

import re

# docker logs --timestamps prefixes each line with Go's RFC3339Nano,
# e.g. "2026-08-16T02:14:16.123456789Z " (fractional part trimmed of
# trailing zeros, possibly absent).
DOCKER_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")

TAIL_LINES = "100"


class CaptureCursor:
    """Tracks the last docker log timestamp token seen for one container.

    One instance lives as long as one capture-worker thread; it survives
    ``docker logs -f`` reattaches (its whole point) and dies with the
    worker when the container is gone.
    """

    def __init__(self):
        self.last_ts = None        # literal token from docker --timestamps
        self.count_at_last_ts = 0  # lines already written bearing that token
        self._skip_budget = 0      # overlap lines still to skip, this attach

    def attach_args(self):
        """Arguments for the next ``docker logs -f`` invocation.

        First attach tails recent history; reattach resumes from the exact
        docker-recorded instant of the last written line (inclusive — the
        skip budget swallows the re-sent duplicates).
        """
        if self.last_ts:
            self._skip_budget = self.count_at_last_ts
            return ["--timestamps", "--since", self.last_ts]
        self._skip_budget = 0
        return ["--timestamps", "--tail", TAIL_LINES]

    def accept(self, line):
        """Return the de-timestamped text to write, or None for a duplicate.

        Lines without a docker timestamp prefix (e.g. ``docker logs``' own
        error messages) pass through untouched and leave the cursor alone.
        """
        ts, sep, rest = line.partition(" ")
        if not sep or not DOCKER_TS_RE.match(ts):
            self._skip_budget = 0
            return line
        if self._skip_budget:
            if ts == self.last_ts:
                self._skip_budget -= 1
                return None
            # Boundary instant is behind us — nothing left to dedupe.
            self._skip_budget = 0
        if ts == self.last_ts:
            self.count_at_last_ts += 1
        else:
            self.last_ts = ts
            self.count_at_last_ts = 1
        return rest
