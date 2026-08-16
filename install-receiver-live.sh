#!/usr/bin/env bash
# One-shot: install the moss receiver into an ALREADY-RUNNING OnionPress
# (fast path, no image rebuild). Run from this repo root after OnionPress
# is up and its WordPress is provisioned.
set -euo pipefail
WP="${WP_CONTAINER:-onionpress-wordpress}"

if ! docker ps --format '{{.Names}}' | grep -qx "$WP"; then
  echo "ERROR: container '$WP' not running. Set WP_CONTAINER=<name> (see: docker ps)." >&2
  exit 1
fi

echo "→ mu-plugin (receiver endpoints)"
docker cp app/Resources/plugins/onionpress-static-receiver.php \
  "$WP":/var/www/html/wp-content/mu-plugins/onionpress-static-receiver.php
docker exec "$WP" chown www-data:www-data \
  /var/www/html/wp-content/mu-plugins/onionpress-static-receiver.php

echo "→ Apache static-first conf"
docker cp app/Resources/docker/wordpress/onionpress-static-site.conf \
  "$WP":/etc/apache2/conf-available/onionpress-static-site.conf
docker exec "$WP" a2enmod rewrite   >/dev/null
docker exec "$WP" a2enconf onionpress-static-site >/dev/null
docker exec "$WP" apache2ctl graceful

echo "✓ receiver installed. Smoke-test:  ./test-receiver.sh"
