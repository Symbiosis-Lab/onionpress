#!/bin/bash
# Refresh the pinned digests for the GHCR images we control. Run at each
# release after `docker pull` has fetched the new images and you have
# verified them.
#
# Updates six files, which must all stay in sync:
#   - app/Resources/docker/docker-compose.yml        (tor, onionheaven, wordpress)
#   - app/MacOS/onionpress                           (takeover worker + pre-pull list)
#   - app/Resources/docker/tor/onionheaven_common.py (TOR_IMAGE_DEFAULT)
#   - linux/onionpress                               (ONIONPRESS_TOR_IMAGE, wordpress pre-pull)
#   - src/onionpress/containers.py                   (ONIONHEAVEN_IMAGE)
#   - src/onionpress/launcher_ops.py                 (DEFAULT_TOR_IMAGE)
#
# TWO REGISTRIES ARE IN PLAY AND THEY ARE NOT INTERCHANGEABLE. The tor image
# this fork runs is ghcr.io/symbiosis-lab/onionpress-tor — it carries
# obfs4proxy and snowflake-client, which upstream's tor image does not, so a
# ref that names one registry must never be re-pinned with the other's digest.
# The wordpress image still comes from upstream. Each ref below is looked up
# and rewritten under its own name.
#
# An image is only looked up if some file actually references it, so dropping
# one from the tree doesn't force a pointless `docker pull` at release time.
# Conversely, a ref this script does NOT know about is a hard error rather
# than a silent skip: that was the old failure mode — the matcher named
# upstream's registry only, so once the fork pinned its own image the pins
# that mattered most stopped being maintained and nothing said so.
#
# mariadb and willfarrell/autoheal stay un-pinned by design — we trust
# their upstream registries to ship security patches without us having
# to track digests.
#
# Usage:
#   docker pull ghcr.io/symbiosis-lab/onionpress-tor:latest
#   docker pull ghcr.io/brewsterkahle/onionpress-wordpress:latest
#   build/refresh-image-digests.sh
#   git diff   # eyeball the changes
#   git commit -am "Bump image digests for v2.4.97"

set -euo pipefail
cd "$(dirname "$0")/.."

# Use python to avoid BSD-vs-GNU sed `-i` incompatibility: BSD sed (macOS)
# requires `-i ''` while GNU sed (Linux/CI) does not. Python is portable
# and we already require a system python3 for the build.
python3 - <<'PY'
import re
import pathlib
import subprocess
import sys

# Repo:tag of every image we pin. Order doesn't matter; each is rewritten
# under its own name so a symbiosis-lab ref can never pick up a
# brewsterkahle digest or vice versa.
IMAGES = [
    "ghcr.io/symbiosis-lab/onionpress-tor:latest",
    "ghcr.io/brewsterkahle/onionpress-tor:latest",
    "ghcr.io/brewsterkahle/onionpress-wordpress:latest",
]

FILES = [
    "app/Resources/docker/docker-compose.yml",
    "app/MacOS/onionpress",
    "app/Resources/docker/tor/onionheaven_common.py",
    "linux/onionpress",
    "src/onionpress/containers.py",
    "src/onionpress/launcher_ops.py",
]

# Lookahead `(?=["}\n])` ensures we only rewrite refs in a pin context
# (a python/shell quoted string, a `${...:-...}` shell or yaml default, or
# end of line) — never an unquoted shell occurrence like
#   `if docker image inspect ghcr.io/...:latest >/dev/null`
# which is just a presence check and should stay unpinned.
#
# `seam` is the python string-concatenation break some callers use to keep
# the ref under a sane line length:
#     ("ghcr.io/…/onionpress-tor:latest"
#      "@sha256:…")
# Without matching it we'd pin the tag half and leave the old digest half
# dangling, producing `…@sha256:new@sha256:old`. The group is optional and
# the engine backtracks out of it, so a ref followed by an unrelated string
# literal still rewrites the way it always did.
SEAM = r'(?P<seam>"\s*\n\s*")?'
DIGEST = r'(?:@sha256:[a-f0-9]+)?'
TAIL = r'(?=["}\n])'


def ref_re(image):
    return re.compile(re.escape(image) + SEAM + DIGEST + TAIL)


# Any onionpress image ref in a pin context, whichever registry publishes it.
# What IMAGES doesn't cover, this catches — loudly.
ANY_REF = re.compile(
    r'ghcr\.io/[A-Za-z0-9._-]+/onionpress-[A-Za-z0-9._-]+:latest'
    + SEAM + DIGEST + TAIL)


def digest_of(image):
    """Digest from `docker images --digests` for an exact repo:tag match.

    Fails loudly if the image isn't cached locally — we'd rather noise out
    than write an empty/wrong digest.
    """
    try:
        out = subprocess.run(
            ["docker", "images", "--digests", "--format",
             "{{.Repository}}:{{.Tag}} {{.Digest}}"],
            capture_output=True, text=True, check=True,
        ).stdout
    except FileNotFoundError:
        sys.exit("ERROR: docker not found on PATH")
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == image and parts[1] != "<none>":
            return parts[1]
    sys.exit(f"ERROR: no digest for {image} — run 'docker pull {image}' first")


sources = {}
for path in FILES:
    sources[path] = pathlib.Path(path).read_text()

pins = {}
for image in IMAGES:
    matcher = ref_re(image)
    if not any(matcher.search(text) for text in sources.values()):
        print(f"  not referenced, skipping: {image}")
        continue
    digest = digest_of(image)
    print(f"  {image} -> {digest}")
    pins[image] = (matcher, digest)

for path, text in sources.items():
    new = text
    for image, (matcher, digest) in pins.items():
        new = matcher.sub(
            lambda m, i=image, d=digest: i + (m.group("seam") or "") + "@" + d,
            new,
        )
    if new != text:
        pathlib.Path(path).write_text(new)
        sources[path] = new
        print(f"  updated: {path}")

# Every ref in a pin context must now carry a digest. One that doesn't is a
# ref this script doesn't know how to maintain — add it to IMAGES rather
# than letting the release ship an unpinned image.
stray = []
for path, text in sources.items():
    for match in ANY_REF.finditer(text):
        if "@sha256:" in match.group(0):
            continue
        line = text.count("\n", 0, match.start()) + 1
        stray.append(f"  {path}:{line}: {match.group(0).splitlines()[0]}")
if stray:
    sys.exit("ERROR: pinned-image refs this script does not maintain:\n"
             + "\n".join(stray)
             + "\nAdd the image to IMAGES in build/refresh-image-digests.sh.")
PY

echo
echo "Updated. Review with: git diff"
