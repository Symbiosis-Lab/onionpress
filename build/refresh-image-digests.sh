#!/bin/bash
# Refresh the pinned digests for the GHCR images we control. Run at each
# release after `docker pull` has fetched the new images and you have
# verified them.
#
# Updates four files, which must all stay in sync:
#   - app/Resources/docker/docker-compose.yml  (two image: lines)
#   - linux/onionpress                          (images=() array)
#   - src/onionpress/containers.py              (ONIONHEAVEN_IMAGE)
#   - src/onionpress/launcher_ops.py            (DEFAULT_TOR_IMAGE)
#
# mariadb and willfarrell/autoheal stay un-pinned by design — we trust
# their upstream registries to ship security patches without us having
# to track digests.
#
# Usage:
#   docker pull ghcr.io/brewsterkahle/onionpress-tor:latest
#   docker pull ghcr.io/brewsterkahle/onionpress-wordpress:latest
#   build/refresh-image-digests.sh
#   git diff   # eyeball the changes
#   git commit -am "Bump image digests for v2.4.97"

set -euo pipefail
cd "$(dirname "$0")/.."

TOR_IMG="ghcr.io/brewsterkahle/onionpress-tor:latest"
WP_IMG="ghcr.io/brewsterkahle/onionpress-wordpress:latest"

digest_of() {
    # Read digest from `docker images --digests` output for an exact
    # repo:tag match. Fail loudly if the image isn't cached locally —
    # we'd rather noise out than write an empty/wrong digest.
    local img="$1"
    local d
    d=$(docker images --digests --format '{{.Repository}}:{{.Tag}} {{.Digest}}' \
        | awk -v i="$img" '$1==i {print $2; exit}')
    if [ -z "$d" ] || [ "$d" = "<none>" ]; then
        echo "ERROR: no digest for $img — run 'docker pull $img' first" >&2
        exit 1
    fi
    echo "$d"
}

TOR_DIGEST=$(digest_of "$TOR_IMG")
WP_DIGEST=$(digest_of "$WP_IMG")
echo "tor:       $TOR_DIGEST"
echo "wordpress: $WP_DIGEST"

TOR_PIN="${TOR_IMG}@${TOR_DIGEST}"
WP_PIN="${WP_IMG}@${WP_DIGEST}"

# 1. docker-compose.yml — replace any existing pin or unpinned variant.
#    The matcher tolerates "@sha256:..." being present or absent so the
#    same script works on a fresh checkout or one already pinned to an
#    older digest.
sed -i -E \
    -e "s|ghcr\\.io/brewsterkahle/onionpress-tor:latest(@sha256:[a-f0-9]+)?|${TOR_PIN}|g" \
    -e "s|ghcr\\.io/brewsterkahle/onionpress-wordpress:latest(@sha256:[a-f0-9]+)?|${WP_PIN}|g" \
    app/Resources/docker/docker-compose.yml \
    linux/onionpress \
    src/onionpress/containers.py \
    src/onionpress/launcher_ops.py

echo
echo "Updated. Review with: git diff"
