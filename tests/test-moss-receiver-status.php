<?php
/**
 * test-moss-receiver-status.php — unit coverage for receiver v1.3's
 * reachability source selection: onionpress_moss_reachability() and
 * onionpress_moss_healing().
 *
 * Why this matters (found live 2026-08-16): on macOS `status.json` is
 * written by exactly one thing, the MenubarApp. A moss-provisioned stack
 * never runs it — moss drives `Contents/MacOS/onionpress start` directly —
 * so /status served a seven-hour-stale `onion_reachable:false` while the
 * onion answered in 7s, and moss's publish=verified-live predicate could
 * never resolve true. v1.3 sources reachability from the tor watchdog's
 * own end-to-end probe first, which needs no GUI and carries its own
 * freshness stamp.
 *
 * Pure functions, no WordPress: `php tests/test-moss-receiver-status.php`.
 * Exit status is 0 only if every assertion passes.
 */

define( 'ABSPATH', __DIR__ );
function add_action( $hook, $callback ) {}
require __DIR__ . '/../app/Resources/plugins/onionpress-moss-receiver.php';

$pass = 0;
$fail = 0;
function ok( $label ) {
    global $pass;
    $pass++;
    printf( "PASS  %s\n", $label );
}
function bad( $label, $detail = '' ) {
    global $fail;
    $fail++;
    printf( "FAIL  %s%s\n", $label, $detail !== '' ? " ($detail)" : '' );
}
function assert_eq( $label, $expected, $actual ) {
    if ( $expected === $actual ) {
        ok( $label );
    } else {
        bad( $label, 'expected ' . var_export( $expected, true )
            . ', got ' . var_export( $actual, true ) );
    }
}

$tmp = sys_get_temp_dir() . '/onionpress-recv-status-' . uniqid( '', true );
mkdir( $tmp );
register_shutdown_function( function () use ( $tmp ) {
    foreach ( glob( $tmp . '/*' ) as $f ) {
        @unlink( $f );
    }
    @rmdir( $tmp );
} );

$NOW = 1786893000;

/** Write $data as JSON to $tmp/$name and return the path. null = no file. */
function jfile( $name, $data ) {
    global $tmp;
    $p = $tmp . '/' . $name;
    file_put_contents( $p, is_string( $data ) ? $data : json_encode( $data ) );
    return $p;
}
function nofile( $name ) {
    global $tmp;
    return $tmp . '/missing-' . $name;
}
function iso( $unix ) {
    return gmdate( 'Y-m-d\TH:i:s\Z', $unix );
}

// --- watchdog source: the primary path ------------------------------------

$w_ok = jfile( 'w-ok.json', array(
    'serving' => true, 'e2e_ok' => true, 'e2e_code' => '200',
    'e2e_verdict' => '', 'e2e_checked_at' => $NOW, 'updated_at' => $NOW,
) );
$r = onionpress_moss_reachability( $w_ok, nofile( 'status' ) );
assert_eq( 'watchdog e2e_ok=true -> reachable true', true, $r['reachable'] );
assert_eq( 'watchdog e2e_code passes through', '200', $r['http_code'] );
assert_eq( 'e2e_checked_at becomes status_updated_at', $NOW, $r['status_updated_at'] );

$w_down = jfile( 'w-down.json', array(
    'e2e_ok' => false, 'e2e_code' => 'timeout', 'e2e_verdict' => 'network',
    'e2e_checked_at' => $NOW, 'updated_at' => $NOW,
) );
$r = onionpress_moss_reachability( $w_down, nofile( 'status' ) );
assert_eq( 'watchdog e2e_ok=false -> reachable false', false, $r['reachable'] );
assert_eq( 'failure code passes through', 'timeout', $r['http_code'] );

// Mid-streak: the probe has run but has not reached the fail threshold, so
// the watchdog's own verdict is still unknown. Unknown must stay null —
// never coerced to false (the docblock rule moss#917 established).
$w_unknown = jfile( 'w-unknown.json', array(
    'e2e_ok' => null, 'e2e_code' => 'timeout', 'e2e_fail_streak' => 1,
    'e2e_checked_at' => $NOW, 'updated_at' => $NOW,
) );
$r = onionpress_moss_reachability( $w_unknown, nofile( 'status' ) );
assert_eq( 'unstreaked failure stays unknown, not false', null, $r['reachable'] );
assert_eq( 'unknown still carries its freshness stamp', $NOW, $r['status_updated_at'] );

// Takeover: moss keys its diagnosis off the literal sentinel string.
$w_takeover = jfile( 'w-takeover.json', array(
    'e2e_ok' => false, 'e2e_code' => '302', 'e2e_verdict' => 'takeover',
    'e2e_checked_at' => $NOW, 'updated_at' => $NOW,
) );
$r = onionpress_moss_reachability( $w_takeover, nofile( 'status' ) );
assert_eq( 'takeover verdict emits the sentinel', 'takeover', $r['http_code'] );
assert_eq( 'takeover is not reachable', false, $r['reachable'] );

// --- fallback: old tor image / not-yet-probed cold start ------------------

$w_old = jfile( 'w-old-image.json', array(
    'serving' => true, 'bootstrapped' => true, 'updated_at' => $NOW,
) );  // no e2e_* fields at all
$s_fresh = jfile( 's-fresh.json', array(
    'onion_reachable' => true, 'onion_http_code' => '200',
    'updated_at' => iso( $NOW - 30 ),
) );
$r = onionpress_moss_reachability( $w_old, $s_fresh );
assert_eq( 'no e2e evidence -> falls back to status.json', true, $r['reachable'] );
assert_eq( 'fallback carries status.json stamp as unix seconds',
    $NOW - 30, $r['status_updated_at'] );

$r = onionpress_moss_reachability( nofile( 'w' ), $s_fresh );
assert_eq( 'absent watchdog file -> status.json', true, $r['reachable'] );

$r = onionpress_moss_reachability( jfile( 'w-garbage.json', 'not json{' ), $s_fresh );
assert_eq( 'unreadable watchdog file -> status.json', true, $r['reachable'] );

$r = onionpress_moss_reachability( nofile( 'w' ), nofile( 's' ) );
assert_eq( 'neither source -> null reachable', null, $r['reachable'] );
assert_eq( 'neither source -> null code', null, $r['http_code'] );
assert_eq( 'neither source -> null stamp', null, $r['status_updated_at'] );

// --- freshness: prefer the fresher stamped source -------------------------

$s_stale = jfile( 's-stale.json', array(
    'onion_reachable' => false, 'onion_http_code' => '000:rc=28',
    'updated_at' => iso( $NOW - 25200 ),   // the 7h-stale file seen live
) );
$r = onionpress_moss_reachability( $w_ok, $s_stale );
assert_eq( 'fresh watchdog beats a 7h-stale status.json', true, $r['reachable'] );
assert_eq( 'and reports the watchdog stamp', $NOW, $r['status_updated_at'] );

$w_stale = jfile( 'w-stale.json', array(
    'e2e_ok' => false, 'e2e_code' => 'timeout', 'e2e_verdict' => 'network',
    'e2e_checked_at' => $NOW - 7200, 'updated_at' => $NOW - 7200,
) );
$r = onionpress_moss_reachability( $w_stale, $s_fresh );
assert_eq( 'fresher status.json wins over a stale watchdog', true, $r['reachable'] );
assert_eq( 'and reports the status.json stamp', $NOW - 30, $r['status_updated_at'] );

// A source is reported whole: the winner's reachable, code and stamp are
// one coherent observation, never spliced across two files.
$r = onionpress_moss_reachability( $w_stale, $s_fresh );
assert_eq( 'winner supplies the http_code too', '200', $r['http_code'] );

// Unparseable status.json timestamp: it must not out-rank a stamped
// watchdog, and must not crash.
$s_badstamp = jfile( 's-badstamp.json', array(
    'onion_reachable' => false, 'onion_http_code' => '000:rc=28',
    'updated_at' => 'nonsense',
) );
$r = onionpress_moss_reachability( $w_down, $s_badstamp );
assert_eq( 'unstamped status.json never out-ranks the watchdog',
    'timeout', $r['http_code'] );
$r = onionpress_moss_reachability( nofile( 'w' ), $s_badstamp );
assert_eq( 'unstamped status.json is still usable alone', false, $r['reachable'] );
assert_eq( 'unstamped status.json yields a null stamp', null, $r['status_updated_at'] );

// status.json's own tri-state must survive the trip.
$s_null = jfile( 's-null.json', array(
    'onion_reachable' => null, 'onion_http_code' => null,
    'updated_at' => iso( $NOW ),
) );
$r = onionpress_moss_reachability( nofile( 'w' ), $s_null );
assert_eq( 'status.json null stays null', null, $r['reachable'] );

// --- healing object -------------------------------------------------------

$s_healing = jfile( 's-healing.json', array(
    'onion_reachable' => false, 'updated_at' => iso( $NOW ),
    'healing' => array(
        'state' => 'host_restarting', 'verdict' => 'network',
        'watchdog_rung' => 'degraded', 'host_attempts_6h' => 1,
        'last_action' => 'restart_stack', 'last_action_at' => $NOW - 60,
        'next_eligible_at' => $NOW + 2700,
    ),
) );
$h = onionpress_moss_healing( $s_healing, nofile( 'w' ) );
assert_eq( 'host supervisor healing passes through',
    'host_restarting', $h['state'] );
assert_eq( 'host attempt count passes through', 1, $h['host_attempts_6h'] );

// A moss-provisioned stack has no MenubarApp, so nobody writes `healing`
// into status.json — derive the observable half from the watchdog instead,
// or the field is permanently null exactly where it is needed.
$h = onionpress_moss_healing( nofile( 's' ), $w_takeover );
assert_eq( 'takeover derives the reclaiming state', 'reclaiming', $h['state'] );
assert_eq( 'derived healing carries the verdict', 'takeover', $h['verdict'] );
assert_eq( 'no host supervisor -> null attempt count',
    null, $h['host_attempts_6h'] );

$w_handoff = jfile( 'w-handoff.json', array(
    'e2e_ok' => false, 'e2e_verdict' => 'network', 'e2e_checked_at' => $NOW,
    'degraded' => true, 'escalate_to_host' => true,
    'restarts_this_outage' => 3, 'updated_at' => $NOW,
) );
$h = onionpress_moss_healing( nofile( 's' ), $w_handoff );
assert_eq( 'handoff derives awaiting_host', 'awaiting_host', $h['state'] );
assert_eq( 'handoff derives the degraded rung', 'degraded', $h['watchdog_rung'] );

$h = onionpress_moss_healing( nofile( 's' ), $w_down );
assert_eq( 'down but still climbing derives watchdog_recovering',
    'watchdog_recovering', $h['state'] );

$h = onionpress_moss_healing( nofile( 's' ), $w_ok );
assert_eq( 'serving derives ok', 'ok', $h['state'] );

assert_eq( 'no evidence at all -> null healing',
    null, onionpress_moss_healing( nofile( 's' ), nofile( 'w' ) ) );

// A pre-1.3 status.json (no healing key) with an old tor image must not
// invent a healing object out of nothing.
assert_eq( 'legacy files -> null healing',
    null, onionpress_moss_healing( $s_fresh, $w_old ) );

echo "\n";
printf( "RESULT: %d passed, %d failed\n", $pass, $fail );
exit( $fail === 0 ? 0 : 1 );
