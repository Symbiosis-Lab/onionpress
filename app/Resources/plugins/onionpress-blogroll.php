<?php
/**
 * Plugin Name: OnionPress Blogroll
 * Description: RSS/Atom feed aggregator for followed sites. Fetches feeds
 *              through Tor (via onionpress-tor-proxy), caches results in a
 *              transient, and exposes onionpress_get_aggregated_feed_items()
 *              for the theme's blogroll page and sidebar.
 * Version: 1.0.0
 * Author: OnionPress
 * Network: true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

// Re-enable the WordPress Links Manager (hidden since 3.5)
add_filter( 'pre_option_link_manager_enabled', '__return_true' );

// Override SimplePie cache lifetime: 1 hour instead of default 12 hours.
add_filter( 'wp_feed_cache_transient_lifetime', function () {
    return HOUR_IN_SECONDS;
} );

// Invalidate the aggregated cache when the following list changes.
add_action( 'update_option_onionpress_following', function () {
    delete_transient( 'onionpress_blogroll_items' );
} );

/**
 * Try to discover the RSS feed URL for a followed .onion address.
 *
 * For OnionPress multisite: http://<addr>/<onionname>/feed/
 * Fallback:                 http://<addr>/feed/
 *
 * @param string $addr      56-char .onion address
 * @param string $onionname Optional onionname (path prefix)
 * @param int    $timeout   Per-candidate HEAD timeout in seconds.
 * @return string Feed URL or empty string on failure
 */
function onionpress_discover_feed_url( $addr, $onionname = '', $timeout = 15 ) {
    $candidates = array();
    if ( $onionname ) {
        $candidates[] = 'http://' . $addr . '/' . $onionname . '/feed/';
    }
    $candidates[] = 'http://' . $addr . '/feed/';

    foreach ( $candidates as $url ) {
        $response = wp_remote_head( $url, array( 'timeout' => $timeout ) );
        if ( ! is_wp_error( $response ) ) {
            $code = wp_remote_retrieve_response_code( $response );
            if ( $code >= 200 && $code < 400 ) {
                return $url;
            }
        }
    }
    return '';
}

/**
 * Fetch and aggregate feed items from all followed sites.
 *
 * Returns a cached array of items sorted by date (newest first).
 * Each item: title, permalink, date (unix), author, source, source_addr, excerpt.
 *
 * @param int $max_items Maximum items to return.
 * @return array
 */
function onionpress_get_aggregated_feed_items( $max_items = 20 ) {
    $cached = get_transient( 'onionpress_blogroll_items' );
    if ( false !== $cached ) {
        return array_slice( $cached, 0, $max_items );
    }

    $following  = get_option( 'onionpress_following', array() );
    $feeds_map  = get_option( 'onionpress_following_feeds', array() );
    $names_map  = get_option( 'onionpress_following_names', array() );
    if ( ! is_array( $following ) ) { $following = array(); }
    if ( ! is_array( $feeds_map ) ) { $feeds_map = array(); }
    if ( ! is_array( $names_map ) ) { $names_map = array(); }

    $titles_map = get_option( 'onionpress_following_titles', array() );
    if ( ! is_array( $titles_map ) ) { $titles_map = array(); }

    // Per-follow stats: last_success (unix), last_post (unix), fail_count (int)
    $stats_map = get_option( 'onionpress_following_stats', array() );
    if ( ! is_array( $stats_map ) ) { $stats_map = array(); }

    $all_items  = array();
    $start_time = time();
    $now        = time();
    $feeds_changed  = false;
    $titles_changed = false;
    $stats_changed  = false;

    foreach ( $following as $addr ) {
        // Bail if we've spent too long fetching feeds (Tor can be slow).
        if ( time() - $start_time > 60 ) {
            break;
        }

        // Back off on repeated failures: skip if not enough time has passed.
        // 0 failures = always try, 1 = 1h, 2 = 2h, 3 = 4h, ... max 24h
        $st = isset( $stats_map[ $addr ] ) ? $stats_map[ $addr ] : array();
        $fail_count  = isset( $st['fail_count'] ) ? (int) $st['fail_count'] : 0;
        $last_success = isset( $st['last_success'] ) ? (int) $st['last_success'] : 0;
        if ( $fail_count > 0 && $last_success > 0 ) {
            $backoff_hours = min( pow( 2, $fail_count - 1 ), 24 );
            if ( $now - $last_success < $backoff_hours * HOUR_IN_SECONDS ) {
                // Use cached items if available, skip fetching
                continue;
            }
        }

        $feed_url = isset( $feeds_map[ $addr ] ) ? $feeds_map[ $addr ] : '';

        // Auto-discover feed URL if we don't have one yet.
        if ( ! $feed_url ) {
            // Direct feed URLs (http/https) stored in following array
            if ( preg_match( '#^https?://#', $addr ) ) {
                $feed_url = $addr;
            } else {
                $onionname = isset( $names_map[ $addr ] ) ? $names_map[ $addr ] : '';
                $feed_url = onionpress_discover_feed_url( $addr, $onionname );
            }
            if ( $feed_url ) {
                $feeds_map[ $addr ] = $feed_url;
                $feeds_changed = true;
            } else {
                // Discovery failed — count as a failure
                $st['fail_count'] = $fail_count + 1;
                if ( ! $last_success ) { $st['last_success'] = $now; } // seed so backoff works
                $stats_map[ $addr ] = $st;
                $stats_changed = true;
                continue;
            }
        }

        $feed = fetch_feed( $feed_url );
        if ( is_wp_error( $feed ) ) {
            $st['fail_count'] = $fail_count + 1;
            if ( ! $last_success ) { $st['last_success'] = $now; }
            $stats_map[ $addr ] = $st;
            $stats_changed = true;
            continue;
        }

        // Success — reset failure count, record contact time
        $st['last_success'] = $now;
        $st['fail_count']   = 0;

        $feed_title = $feed->get_title();
        // Save the feed title for display in settings/sidebar
        if ( $feed_title && ( ! isset( $titles_map[ $addr ] ) || $titles_map[ $addr ] !== $feed_title ) ) {
            $titles_map[ $addr ] = $feed_title;
            $titles_changed = true;
        }
        $items = $feed->get_items( 0, 10 );
        $newest_post = 0;
        foreach ( $items as $item ) {
            $item_date = (int) $item->get_date( 'U' );
            if ( $item_date > $newest_post ) {
                $newest_post = $item_date;
            }
            $all_items[] = array(
                'title'       => $item->get_title(),
                'permalink'   => $item->get_permalink(),
                'date'        => $item_date,
                'author'      => $item->get_author() ? $item->get_author()->get_name() : '',
                'source'      => $feed_title,
                'source_addr' => $addr,
                'excerpt'     => wp_trim_words( wp_strip_all_tags( $item->get_description() ), 30 ),
            );
        }
        if ( $newest_post ) {
            $st['last_post'] = $newest_post;
        }
        $stats_map[ $addr ] = $st;
        $stats_changed = true;
    }

    // Persist any newly discovered feed URLs, titles, or stats.
    if ( $feeds_changed ) {
        update_option( 'onionpress_following_feeds', $feeds_map );
    }
    if ( $titles_changed ) {
        update_option( 'onionpress_following_titles', $titles_map );
    }
    if ( $stats_changed ) {
        update_option( 'onionpress_following_stats', $stats_map );
    }

    // Sort newest first.
    usort( $all_items, function ( $a, $b ) {
        return $b['date'] - $a['date'];
    } );

    // Store up to 50 items in the cache.
    $to_cache = array_slice( $all_items, 0, 50 );
    set_transient( 'onionpress_blogroll_items', $to_cache, HOUR_IN_SECONDS );

    return array_slice( $to_cache, 0, $max_items );
}
