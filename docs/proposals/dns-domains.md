# DRAFT — proposal issue: clearnet DNS domains alongside the .onion

> Status: reserve draft. Open only after engagement in the umbrella issue.

**Title: RFC: a clearnet domain alongside the `.onion` for published sites**

onionpress.org itself lives a dual life: a normal DNS domain for everyone,
and an onion address for people who need it. Sites published *through*
OnionPress currently only get the second half. For most authors the .onion
is the resilient mirror, not the front door — they still need a
`example.com` their readers can reach.

Questions:

1. Is dual-homing in scope for OnionPress itself (e.g. Onion-Location
   headers already point clearnet visitors to the onion — should the
   reverse exist: a supported path from a registered domain to a machine
   running OnionPress)?
2. The naming CLI (#__, in the receiver PR) registers memorable onion
   names headlessly. Should the same flow optionally drive DNS (an API
   like a registrar's, or documented A/AAAA + reverse-proxy guidance), or
   is DNS strictly the publisher app's problem?
3. Certificates: for a home machine behind NAT, the realistic clearnet
   path is a small tunnel/edge (user-owned VPS, or a service). Would you
   want OnionPress to document/bless one pattern, or stay onion-only and
   leave clearnet to integrators?

We ask because our publisher (moss) already deploys to clearnet hosts; the
gap is a blessed story for "this OnionPress machine also answers for my
domain" that doesn't compromise the onion service's isolation.
