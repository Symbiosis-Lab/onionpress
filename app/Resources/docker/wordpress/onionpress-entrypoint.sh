#!/bin/bash
# OnionPress WordPress entrypoint wrapper.
# Runs the multisite init in the background (after a delay for DB to be ready),
# then hands off to the standard WordPress entrypoint.

# Run multisite init in background after WordPress and DB are up
(
    sleep 15  # Wait for WordPress + MariaDB to be ready
    /usr/local/bin/onionpress-multisite-init.sh 2>&1 | while read -r line; do
        echo "[multisite-init] $line"
    done
) &

# Hand off to the standard WordPress entrypoint
exec docker-entrypoint.sh "$@"
