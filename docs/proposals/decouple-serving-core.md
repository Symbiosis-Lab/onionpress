# DRAFT — proposal issue: a lighter serving core, decoupled from WordPress

> Status: reserve draft. Open on brewsterkahle/onionpress only after Brewster
> engages with this thread in the umbrella issue. Frame as questions, not a
> design — invite his direction.

**Title: RFC: decoupling the serving core so any static-site generator
integrates lightly**

Today, publishing a static site through OnionPress means running the full
stack — WordPress + MariaDB included — even when the publisher never uses
WordPress. The pieces a static publish actually needs are the tor container,
the static receiver, and the Apache serving rules; on a 2 GB VM, WordPress
and MariaDB are most of the idle footprint.

Questions we'd like your read on before proposing anything concrete:

1. Would you want a "serving-only" profile in this repo (compose profile /
   flag that skips WordPress+DB), or is WordPress-always part of the
   product identity, with lighter integrations belonging elsewhere?
2. If a profile: what should own the admin surface that WordPress provides
   today (settings, onion naming, the auto-login door)? The menubar app
   already does most lifecycle work.
3. The receiver currently lives as a WordPress mu-plugin because that gave
   it a REST server and the subsite-collision guard for free. A standalone
   receiver (tiny PHP or a static binary in the tor image's pod) drops the
   WordPress dependency — worth it, or complexity in the wrong place?

Context: the static receiver + static-first serving PRs (#__, #__) work
today with WordPress present; this issue is about the next step for
publishers that don't need it. We're happy to prototype whichever direction
you prefer.
