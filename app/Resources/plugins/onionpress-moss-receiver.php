<?php
/**
 * Plugin Name: OnionPress moss Receiver
 * Description: Loopback REST endpoints that let the moss editor publish a
 *              pre-rendered static site into OnionPress. moss uploads a tar
 *              of a generated site, the receiver extracts it under
 *              /var/www/html/site-generations/<id>/, and an atomic symlink
 *              flip of /var/www/html/site/current makes it live. Apache then
 *              serves those files ahead of WordPress (see
 *              onionpress-static-site.conf). Trusts only local requests.
 * Version:     1.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * Filesystem layout shared by the three endpoints.
 *
 *   GENERATIONS/<id>/   — one extracted moss generation (static files)
 *   SITE/current        — symlink to the live generation
 *
 * `current` lives next to (not inside) the generations dir so a generation
 * can never contain a name that collides with the `current` symlink.
 */
define( 'ONIONPRESS_MOSS_WEB_ROOT',    '/var/www/html' );
define( 'ONIONPRESS_MOSS_SITE_DIR',    '/var/www/html/site' );
define( 'ONIONPRESS_MOSS_CURRENT',     '/var/www/html/site/current' );
define( 'ONIONPRESS_MOSS_GENERATIONS', '/var/www/html/site-generations' );
define( 'ONIONPRESS_MOSS_KEEP',        3 );

/**
 * Localhost trust, shared by every route.
 *
 * The receiver is only ever driven by the moss app on the same machine,
 * reaching Apache over the host's loopback port map. It must never be
 * reachable by a public onion visitor. Two independent checks:
 *
 *   1. Any `X-Forwarded-*` header means the request was relayed by a proxy.
 *      The onion serving path (tor -> apache) sets none, so their presence
 *      is a spoof or a misconfiguration — deny.
 *   2. If REMOTE_ADDR is the tor or onionheaven container, the request
 *      arrived over the onion service rather than the local port map — deny.
 *
 * Precedent: onionpress-auto-login.php treats the local machine as trusted.
 *
 * @return bool True when the request may proceed.
 */
function onionpress_moss_is_local_request() {
    // 1. Reject anything carrying proxy-forwarding headers.
    foreach ( array_keys( $_SERVER ) as $key ) {
        if ( strpos( $key, 'HTTP_X_FORWARDED_' ) === 0 ) {
            return false;
        }
    }

    // 2. Reject requests whose source is the tor / onionheaven container.
    $remote = isset( $_SERVER['REMOTE_ADDR'] ) ? $_SERVER['REMOTE_ADDR'] : '';
    if ( $remote === '' ) {
        return false;
    }
    foreach ( array( 'onionpress-tor', 'onionheaven' ) as $host ) {
        $ip = gethostbyname( $host );
        // gethostbyname() returns its input unchanged when resolution fails;
        // only a real, differing result is a container IP worth matching.
        if ( $ip !== $host && $remote === $ip ) {
            return false;
        }
    }

    return true;
}

/**
 * Read the current onion address, or '' if not known yet.
 * Primary source is the flat address file; status.json is the fallback.
 */
function onionpress_moss_onion_address() {
    $f = '/var/lib/onionpress/onion_address';
    if ( is_readable( $f ) ) {
        $addr = trim( (string) @file_get_contents( $f ) );
        if ( $addr !== '' ) {
            return $addr;
        }
    }
    $sf = '/var/lib/onionpress/status.json';
    if ( is_readable( $sf ) ) {
        $data = json_decode( (string) @file_get_contents( $sf ), true );
        if ( is_array( $data ) && ! empty( $data['onion_address'] ) ) {
            return (string) $data['onion_address'];
        }
    }
    return '';
}

/**
 * External reachability, as last observed by the health checker (moss#917).
 *
 * Read straight from status.json — the same file onion_address falls back
 * to — rather than re-probing here: the reachability check is a real
 * Tor-routed curl (see health.py's check_external_reachability), too slow
 * to run inline in a REST response. `reachable` is null (not false) until
 * the checker has actually completed Check 5 at least once; a caller must
 * not read null as "confirmed unreachable".
 *
 * @return array{reachable: bool|null, http_code: string|null}
 */
function onionpress_moss_reachability() {
    $sf = '/var/lib/onionpress/status.json';
    if ( is_readable( $sf ) ) {
        $data = json_decode( (string) @file_get_contents( $sf ), true );
        if ( is_array( $data ) ) {
            $reachable = array_key_exists( 'onion_reachable', $data ) ? $data['onion_reachable'] : null;
            $http_code = array_key_exists( 'onion_http_code', $data ) ? $data['onion_http_code'] : null;
            return array(
                'reachable' => is_bool( $reachable ) ? $reachable : null,
                'http_code' => is_string( $http_code ) ? $http_code : null,
            );
        }
    }
    return array( 'reachable' => null, 'http_code' => null );
}

/**
 * The id of the live generation (basename of the `current` symlink target),
 * or null when nothing has been committed yet.
 */
function onionpress_moss_current_generation() {
    if ( is_link( ONIONPRESS_MOSS_CURRENT ) ) {
        $target = @readlink( ONIONPRESS_MOSS_CURRENT );
        if ( is_string( $target ) && $target !== '' ) {
            return basename( $target );
        }
    }
    return null;
}

/**
 * A generation id is an opaque directory name. Reject anything that could
 * escape the generations dir. moss sends `moss-<unix_seconds>`.
 */
function onionpress_moss_valid_id( $id ) {
    return is_string( $id )
        && $id !== ''
        && strpos( $id, "\0" ) === false
        && strpos( $id, '/' ) === false
        && strpos( $id, '..' ) === false
        && $id === basename( $id );
}

/** Uniform error response: 4xx/5xx JSON `{ ok:false, error:… }`. */
function onionpress_moss_error( $message, $status ) {
    return new WP_REST_Response(
        array( 'ok' => false, 'error' => $message ),
        $status
    );
}

/**
 * Recursively delete a path. Never follows symlinks (a symlink is unlinked,
 * not descended into).
 */
function onionpress_moss_rmrf( $path ) {
    if ( is_link( $path ) || ! file_exists( $path ) ) {
        @unlink( $path );
        return;
    }
    if ( is_dir( $path ) ) {
        $items = new RecursiveIteratorIterator(
            new RecursiveDirectoryIterator( $path, FilesystemIterator::SKIP_DOTS ),
            RecursiveIteratorIterator::CHILD_FIRST
        );
        foreach ( $items as $item ) {
            if ( $item->isLink() || ! $item->isDir() ) {
                @unlink( $item->getPathname() );
            } else {
                @rmdir( $item->getPathname() );
            }
        }
        @rmdir( $path );
    } else {
        @unlink( $path );
    }
}

/**
 * Read exactly $size bytes of tar payload, then consume padding up to the
 * next 512-byte boundary ($padded). Returns the bytes read.
 */
function onionpress_moss_read_n( $fh, $size, $padded ) {
    $buf       = '';
    $remaining = $size;
    while ( $remaining > 0 ) {
        $chunk = fread( $fh, $remaining > 524288 ? 524288 : $remaining );
        if ( $chunk === '' || $chunk === false ) {
            break;
        }
        $buf       .= $chunk;
        $remaining -= strlen( $chunk );
    }
    $pad = $padded - $size;
    if ( $pad > 0 ) {
        fread( $fh, $pad );
    }
    return $buf;
}

/**
 * Streaming, security-hardened extractor for a plain (uncompressed) tar.
 *
 * Why not PharData: the contract's `tar -cf x.tar -C <gendir> .` emits a `.`
 * self-entry that PharData::extractTo cannot handle ("Cannot extract '.'"),
 * so it fails on every real moss upload. This reader also lets the traversal
 * and link guards run inline, in one pass, failing closed on anything it does
 * not positively recognise as a regular file or directory.
 *
 * Rejects: hard/symlinks (typeflag 1/2), devices/FIFOs (3/4/6), absolute
 * paths, `..` traversal, embedded NUL, and non-ustar headers.
 *
 * @return array{0:bool,1:?string} [ok, error message]
 */
function onionpress_moss_extract_tar( $tar_path, $dest_dir ) {
    $fh = @fopen( $tar_path, 'rb' );
    if ( ! $fh ) {
        return array( false, 'cannot open tar' );
    }
    if ( ! is_dir( $dest_dir ) && ! @mkdir( $dest_dir, 0755, true ) ) {
        fclose( $fh );
        return array( false, 'cannot create destination' );
    }
    $dest_real = realpath( $dest_dir );
    if ( $dest_real === false ) {
        fclose( $fh );
        return array( false, 'destination missing' );
    }

    $pending_longname = null; // carried from a GNU 'L' entry
    $pending_paxpath  = null; // carried from a pax 'x' path= record

    while ( ! feof( $fh ) ) {
        $header = fread( $fh, 512 );
        if ( $header === '' || $header === false ) {
            break; // clean EOF
        }
        if ( strlen( $header ) < 512 ) {
            fclose( $fh );
            return array( false, 'truncated tar header' );
        }
        // A zero block marks the end of the archive.
        if ( trim( $header, "\0" ) === '' ) {
            break;
        }

        $name     = rtrim( substr( $header, 0, 100 ), "\0" );
        $size_fld = trim( substr( $header, 124, 12 ), " \0" );
        $typeflag = substr( $header, 156, 1 );
        $magic    = substr( $header, 257, 5 );
        $prefix   = rtrim( substr( $header, 345, 155 ), "\0" );

        // Fail closed on anything that is not a POSIX ustar/pax header.
        if ( $magic !== 'ustar' ) {
            fclose( $fh );
            return array( false, 'unrecognised tar header (not ustar/pax)' );
        }

        // GNU base-256 large-file size (high bit set) is out of scope.
        if ( $size_fld !== '' && ( ord( $size_fld[0] ) & 0x80 ) ) {
            fclose( $fh );
            return array( false, 'unsupported large-file size field' );
        }
        $size        = $size_fld === '' ? 0 : (int) octdec( $size_fld );
        $data_padded = (int) ( ceil( $size / 512 ) * 512 );

        // GNU long-name / long-link carry entries: payload names the *next*
        // entry. Links are rejected below, so a long link name is discarded.
        if ( $typeflag === 'L' ) {
            $pending_longname = rtrim(
                onionpress_moss_read_n( $fh, $size, $data_padded ), "\0" );
            continue;
        }
        if ( $typeflag === 'K' ) {
            onionpress_moss_read_n( $fh, $size, $data_padded );
            continue;
        }
        // pax extended header: pull a `path=` record if present.
        if ( $typeflag === 'x' || $typeflag === 'g' ) {
            $paxdata = onionpress_moss_read_n( $fh, $size, $data_padded );
            if ( preg_match( '/^\d+ path=([^\n]*)\n/m', $paxdata, $m ) ) {
                $pending_paxpath = $m[1];
            }
            continue;
        }

        // Resolve the effective name: pax > GNU longname > ustar name+prefix.
        if ( $pending_paxpath !== null ) {
            $full = $pending_paxpath;
        } elseif ( $pending_longname !== null ) {
            $full = $pending_longname;
        } else {
            $full = $prefix !== '' ? $prefix . '/' . $name : $name;
        }
        $pending_longname = null;
        $pending_paxpath  = null;

        // SECURITY: reject links and special files.
        if ( $typeflag === '1' || $typeflag === '2' ) {
            fclose( $fh );
            return array( false, 'archive contains a hard/symbolic link' );
        }
        if ( $typeflag === '3' || $typeflag === '4' || $typeflag === '6' ) {
            fclose( $fh );
            return array( false, 'archive contains a device or FIFO entry' );
        }

        // SECURITY: reject NUL, absolute paths, and `..` traversal.
        if ( strpos( $full, "\0" ) !== false ) {
            fclose( $fh );
            return array( false, 'archive entry name contains NUL' );
        }
        $full = str_replace( '\\', '/', $full );
        if ( isset( $full[0] ) && $full[0] === '/' ) {
            fclose( $fh );
            return array( false, 'archive entry uses an absolute path' );
        }
        $segments = array();
        foreach ( explode( '/', $full ) as $seg ) {
            if ( $seg === '' || $seg === '.' ) {
                continue;
            }
            if ( $seg === '..' ) {
                fclose( $fh );
                return array( false, 'archive entry escapes with ..' );
            }
            $segments[] = $seg;
        }
        $rel = implode( '/', $segments );

        $is_dir = ( $typeflag === '5' );

        // The `.` self-entry from `tar -C <dir> .` normalises to empty.
        if ( $rel === '' ) {
            if ( ! $is_dir && $size > 0 ) {
                onionpress_moss_read_n( $fh, $size, $data_padded );
            }
            continue;
        }

        $target = $dest_real . '/' . $rel;

        if ( $is_dir ) {
            if ( ! is_dir( $target ) && ! @mkdir( $target, 0755, true ) ) {
                fclose( $fh );
                return array( false, 'cannot create directory ' . $rel );
            }
            continue;
        }

        // Regular file ('0', "\0", or '7' contiguous): stream its bytes out.
        $parent = dirname( $target );
        if ( ! is_dir( $parent ) && ! @mkdir( $parent, 0755, true ) ) {
            fclose( $fh );
            return array( false, 'cannot create parent for ' . $rel );
        }
        $out = @fopen( $target, 'wb' );
        if ( ! $out ) {
            fclose( $fh );
            return array( false, 'cannot write ' . $rel );
        }
        $remaining = $size;
        while ( $remaining > 0 ) {
            $chunk = fread( $fh, $remaining > 524288 ? 524288 : $remaining );
            if ( $chunk === '' || $chunk === false ) {
                break;
            }
            fwrite( $out, $chunk );
            $remaining -= strlen( $chunk );
        }
        fclose( $out );
        // Consume padding to the next 512-byte boundary.
        $pad = $data_padded - $size;
        if ( $pad > 0 ) {
            fread( $fh, $pad );
        }
    }

    fclose( $fh );
    return array( true, null );
}

/**
 * GET /status — advertise the receiver so moss can find the right port.
 */
function onionpress_moss_route_status() {
    $reachability = onionpress_moss_reachability();
    return new WP_REST_Response( array(
        'onion_address'      => onionpress_moss_onion_address(),
        'current_generation' => onionpress_moss_current_generation(),
        'receiver_version'   => '1.1',
        'onion_reachable'    => $reachability['reachable'],
        'onion_http_code'    => $reachability['http_code'],
    ), 200 );
}

/**
 * POST /generation?id=<id> — accept a raw tar of one moss generation.
 *
 * The body is written to <id>.tar, extracted into <id>.tmp/, then atomically
 * renamed to <id>/. WordPress has already consumed php://input into the
 * request body, so we read it back via $request->get_body().
 */
function onionpress_moss_route_generation( $request ) {
    $id = $request->get_param( 'id' );
    if ( ! onionpress_moss_valid_id( $id ) ) {
        return onionpress_moss_error( 'invalid generation id', 400 );
    }

    if ( ! is_dir( ONIONPRESS_MOSS_GENERATIONS )
        && ! @mkdir( ONIONPRESS_MOSS_GENERATIONS, 0755, true ) ) {
        return onionpress_moss_error( 'cannot create generations dir', 500 );
    }

    $tar_path  = ONIONPRESS_MOSS_GENERATIONS . '/' . $id . '.tar';
    $tmp_dir   = ONIONPRESS_MOSS_GENERATIONS . '/' . $id . '.tmp';
    $final_dir = ONIONPRESS_MOSS_GENERATIONS . '/' . $id;

    // Write the uploaded tar to disk.
    $body = $request->get_body();
    if ( $body === '' || $body === null ) {
        return onionpress_moss_error( 'empty request body', 400 );
    }
    if ( @file_put_contents( $tar_path, $body ) === false ) {
        return onionpress_moss_error( 'cannot write upload', 500 );
    }

    // Fresh extraction target.
    onionpress_moss_rmrf( $tmp_dir );

    list( $ok, $err ) = onionpress_moss_extract_tar( $tar_path, $tmp_dir );
    if ( ! $ok ) {
        onionpress_moss_rmrf( $tmp_dir );
        @unlink( $tar_path );
        return onionpress_moss_error( 'extract failed: ' . $err, 400 );
    }

    // A generation id is single-use; refuse to clobber an existing one.
    if ( file_exists( $final_dir ) ) {
        onionpress_moss_rmrf( $tmp_dir );
        @unlink( $tar_path );
        return onionpress_moss_error( 'generation already exists', 409 );
    }

    if ( ! @rename( $tmp_dir, $final_dir ) ) {
        onionpress_moss_rmrf( $tmp_dir );
        @unlink( $tar_path );
        return onionpress_moss_error( 'cannot finalise generation', 500 );
    }

    @unlink( $tar_path );

    return new WP_REST_Response(
        array( 'ok' => true, 'generation' => $id ), 200 );
}

/**
 * POST /commit {generation:"<id>"} — flip the live site to a generation.
 *
 * Guards against a generation whose top-level names would shadow WordPress
 * itself or an existing subsite, flips `site/current` atomically, then GCs
 * old generations (keeping the newest few and always the live one).
 */
function onionpress_moss_route_commit( $request ) {
    $id = $request->get_param( 'generation' );
    if ( ! onionpress_moss_valid_id( $id ) ) {
        return onionpress_moss_error( 'invalid generation id', 400 );
    }

    $gen_dir = ONIONPRESS_MOSS_GENERATIONS . '/' . $id;
    if ( ! is_dir( $gen_dir ) ) {
        return onionpress_moss_error( 'unknown generation', 404 );
    }

    // Collision guard: a static file served at the site root must never
    // shadow WordPress's own paths or an existing subsite.
    $reserved = array(
        'wp-admin', 'wp-content', 'wp-includes', 'wp-json',
        'wp-login.php', 'wp-cron.php', 'xmlrpc.php',
        'site', 'site-generations',
    );
    global $wpdb;
    if ( isset( $wpdb->blogs ) ) {
        $rows = $wpdb->get_col( "SELECT path FROM {$wpdb->blogs}" );
        foreach ( (array) $rows as $p ) {
            $seg = strtok( trim( (string) $p, '/' ), '/' );
            if ( $seg !== false && $seg !== '' ) {
                $reserved[] = $seg;
            }
        }
    }
    $reserved = array_unique( $reserved );

    foreach ( scandir( $gen_dir ) as $entry ) {
        if ( $entry === '.' || $entry === '..' ) {
            continue;
        }
        if ( in_array( $entry, $reserved, true ) ) {
            return onionpress_moss_error(
                'generation collides with reserved path: ' . $entry, 409 );
        }
    }

    // Atomic flip: build the new symlink beside `current`, then rename over
    // it. rename() of a symlink is atomic on the same filesystem.
    if ( ! is_dir( ONIONPRESS_MOSS_SITE_DIR )
        && ! @mkdir( ONIONPRESS_MOSS_SITE_DIR, 0755, true ) ) {
        return onionpress_moss_error( 'cannot create site dir', 500 );
    }
    $tmp_link = ONIONPRESS_MOSS_SITE_DIR . '/current.tmp-' . uniqid( '', true );
    @unlink( $tmp_link );
    if ( ! @symlink( $gen_dir, $tmp_link ) ) {
        return onionpress_moss_error( 'cannot create symlink', 500 );
    }
    if ( ! @rename( $tmp_link, ONIONPRESS_MOSS_CURRENT ) ) {
        @unlink( $tmp_link );
        return onionpress_moss_error( 'cannot activate generation', 500 );
    }

    onionpress_moss_gc_generations();

    $addr = onionpress_moss_onion_address();
    $url  = $addr !== '' ? 'http://' . $addr . '/' : '';
    return new WP_REST_Response(
        array( 'ok' => true, 'url' => $url ), 200 );
}

/**
 * Keep only the newest ONIONPRESS_MOSS_KEEP generations, never deleting the
 * one `current` points at.
 */
function onionpress_moss_gc_generations() {
    $current = onionpress_moss_current_generation();
    $dirs    = array();
    foreach ( scandir( ONIONPRESS_MOSS_GENERATIONS ) as $entry ) {
        if ( $entry === '.' || $entry === '..' ) {
            continue;
        }
        $p = ONIONPRESS_MOSS_GENERATIONS . '/' . $entry;
        if ( is_dir( $p ) && ! is_link( $p ) ) {
            $dirs[ $entry ] = @filemtime( $p );
        }
    }
    arsort( $dirs ); // newest first

    $i = 0;
    foreach ( $dirs as $name => $mtime ) {
        $i++;
        if ( $i <= ONIONPRESS_MOSS_KEEP ) {
            continue;
        }
        if ( $name === $current ) {
            continue; // never GC the live generation
        }
        onionpress_moss_rmrf( ONIONPRESS_MOSS_GENERATIONS . '/' . $name );
    }
}

/**
 * Register the three routes under /wp-json/onionpress/v1/*. All share the
 * localhost-trust permission callback.
 */
add_action( 'rest_api_init', function () {
    $local = 'onionpress_moss_is_local_request';

    register_rest_route( 'onionpress/v1', '/status', array(
        'methods'             => 'GET',
        'permission_callback' => $local,
        'callback'            => 'onionpress_moss_route_status',
    ) );

    register_rest_route( 'onionpress/v1', '/generation', array(
        'methods'             => 'POST',
        'permission_callback' => $local,
        'callback'            => 'onionpress_moss_route_generation',
    ) );

    register_rest_route( 'onionpress/v1', '/commit', array(
        'methods'             => 'POST',
        'permission_callback' => $local,
        'callback'            => 'onionpress_moss_route_commit',
    ) );
} );
