#!/bin/bash
# OnionPress multisite initialization script.
# Runs as part of WordPress container startup to ensure multisite is
# configured from the very first boot. This eliminates the SUNRISE
# chicken-and-egg problem where the launcher would set SUNRISE before
# the wp_site table existed.
#
# Called from the Dockerfile entrypoint wrapper. Runs AFTER WordPress
# and the database are ready.

set -e

# Wait for database to be ready
wait_for_db() {
    local max_wait=60
    local waited=0
    while [ $waited -lt $max_wait ]; do
        if wp db check --allow-root >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done
    echo "WARNING: Database not ready after ${max_wait}s"
    return 1
}

# Only run if WordPress tables exist (wp core install has been done)
if ! wp core is-installed --allow-root >/dev/null 2>&1; then
    # WordPress not installed yet — skip multisite setup
    # The inline setup window or browser setup will handle wp core install
    exit 0
fi

# Check if multisite is already active
if wp core is-installed --network --allow-root >/dev/null 2>&1; then
    # Already multisite — nothing to do
    exit 0
fi

echo "OnionPress: Converting to multisite..."

# Wait for database
wait_for_db || exit 0

# Convert to multisite (subdirectory mode)
wp core multisite-convert --url=http://localhost --allow-root 2>/dev/null || true

# Set multisite constants in wp-config.php
for const_val in \
    "MULTISITE:true" \
    "SUBDOMAIN_INSTALL:false" \
    "DOMAIN_CURRENT_SITE:'localhost'" \
    "PATH_CURRENT_SITE:'/'" \
    "SITE_ID_CURRENT_SITE:1" \
    "BLOG_ID_CURRENT_SITE:1" \
    "SUNRISE:true"; do
    name="${const_val%%:*}"
    value="${const_val#*:}"
    wp config set "$name" "$value" --raw --type=constant --allow-root 2>/dev/null || true
done

echo "OnionPress: Multisite conversion complete"
