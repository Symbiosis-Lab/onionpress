<?php
/**
 * Minimal QR Code generator — byte mode, EC level L, versions 1-7.
 * Outputs inline SVG. No external dependencies.
 *
 * Usage: echo onionpress_qr_svg('https://example.com', 200);
 */

if (!defined('ABSPATH')) exit;

function onionpress_qr_svg($text, $size = 200) {
    $modules = _qr_encode($text);
    if (!$modules) return '';
    $n = count($modules);
    $quiet = 4; // quiet zone modules
    $total = $n + $quiet * 2;
    $scale = $size / $total;

    $svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' . $size . '" height="' . $size . '" viewBox="0 0 ' . $total . ' ' . $total . '">';
    $svg .= '<rect width="' . $total . '" height="' . $total . '" fill="#fff"/>';
    for ($r = 0; $r < $n; $r++) {
        for ($c = 0; $c < $n; $c++) {
            if ($modules[$r][$c]) {
                $svg .= '<rect x="' . ($c + $quiet) . '" y="' . ($r + $quiet) . '" width="1" height="1" fill="#000"/>';
            }
        }
    }
    $svg .= '</svg>';
    return $svg;
}

/* ── GF(256) arithmetic ─────────────────────────────────────────────── */

function _qr_gf_init() {
    static $tables = null;
    if ($tables) return $tables;
    $exp = array_fill(0, 512, 0);
    $log = array_fill(0, 256, 0);
    $v = 1;
    for ($i = 0; $i < 255; $i++) {
        $exp[$i] = $v;
        $log[$v] = $i;
        $v <<= 1;
        if ($v & 256) $v ^= 0x11d;
    }
    for ($i = 255; $i < 512; $i++) $exp[$i] = $exp[$i - 255];
    $tables = array('exp' => $exp, 'log' => $log);
    return $tables;
}

function _qr_gf_mul($a, $b) {
    if ($a === 0 || $b === 0) return 0;
    $t = _qr_gf_init();
    return $t['exp'][$t['log'][$a] + $t['log'][$b]];
}

/* ── Reed-Solomon ───────────────────────────────────────────────────── */

function _qr_rs_encode($data, $ec_count) {
    $t = _qr_gf_init();
    // Build generator polynomial
    $gen = array(1);
    for ($i = 0; $i < $ec_count; $i++) {
        $new = array_fill(0, count($gen) + 1, 0);
        for ($j = 0; $j < count($gen); $j++) {
            $new[$j] ^= $gen[$j];
            $new[$j + 1] ^= _qr_gf_mul($gen[$j], $t['exp'][$i]);
        }
        $gen = $new;
    }

    $msg = array_merge($data, array_fill(0, $ec_count, 0));
    for ($i = 0; $i < count($data); $i++) {
        $coef = $msg[$i];
        if ($coef === 0) continue;
        for ($j = 0; $j < count($gen); $j++) {
            $msg[$i + $j] ^= _qr_gf_mul($gen[$j], $coef);
        }
    }
    return array_slice($msg, count($data));
}

/* ── Version parameters (EC level L) ────────────────────────────────── */

function _qr_version_params() {
    // [total_codewords, ec_codewords, data_capacity_bytes]
    return array(
        1 => array(26, 7, 19),
        2 => array(44, 10, 34),
        3 => array(70, 15, 55),
        4 => array(100, 20, 80),
        5 => array(134, 26, 108),
        6 => array(172, 18, 154),
        7 => array(196, 20, 176),
    );
}

function _qr_alignment_positions() {
    return array(
        1 => array(),
        2 => array(6, 18),
        3 => array(6, 22),
        4 => array(6, 26),
        5 => array(6, 30),
        6 => array(6, 34),
        7 => array(6, 22, 38),
    );
}

/* ── Data encoding (byte mode) ──────────────────────────────────────── */

function _qr_encode_data($text, $version) {
    $params = _qr_version_params();
    $p = $params[$version];
    $data_cap = $p[2];
    $ec_count = $p[1];

    $bytes = array_values(unpack('C*', $text));
    $len = count($bytes);

    // Character count indicator length for byte mode
    $cc_bits = ($version <= 9) ? 8 : 16;

    // Build bit string: mode (0100) + count + data
    $bits = '0100'; // byte mode
    $bits .= str_pad(decbin($len), $cc_bits, '0', STR_PAD_LEFT);
    foreach ($bytes as $b) {
        $bits .= str_pad(decbin($b), 8, '0', STR_PAD_LEFT);
    }

    // Terminator
    $bits .= str_repeat('0', min(4, $data_cap * 8 - strlen($bits)));
    // Pad to byte boundary
    while (strlen($bits) % 8 !== 0) $bits .= '0';

    // Convert to bytes
    $data = array();
    for ($i = 0; $i < strlen($bits); $i += 8) {
        $data[] = bindec(substr($bits, $i, 8));
    }

    // Pad bytes
    $pad = array(0xEC, 0x11);
    $pi = 0;
    while (count($data) < $data_cap) {
        $data[] = $pad[$pi % 2];
        $pi++;
    }

    // Add error correction
    $ec = _qr_rs_encode($data, $ec_count);
    return array_merge($data, $ec);
}

/* ── Matrix construction ────────────────────────────────────────────── */

function _qr_encode($text) {
    $params = _qr_version_params();
    $version = 0;
    foreach ($params as $v => $p) {
        if (strlen($text) <= $p[2] - 2) { // -2 for mode + length overhead
            $version = $v;
            break;
        }
    }
    if (!$version) return null; // text too long

    $n = 17 + $version * 4; // module count
    $modules = array_fill(0, $n, array_fill(0, $n, 0));
    $reserved = array_fill(0, $n, array_fill(0, $n, false));

    // Place finder patterns
    _qr_place_finder($modules, $reserved, $n, 0, 0);
    _qr_place_finder($modules, $reserved, $n, 0, $n - 7);
    _qr_place_finder($modules, $reserved, $n, $n - 7, 0);

    // Timing patterns
    for ($i = 8; $i < $n - 8; $i++) {
        $modules[$i][6] = ($i % 2 === 0) ? 1 : 0;
        $reserved[$i][6] = true;
        $modules[6][$i] = ($i % 2 === 0) ? 1 : 0;
        $reserved[6][$i] = true;
    }

    // Alignment patterns
    $align = _qr_alignment_positions();
    $positions = $align[$version];
    if (!empty($positions)) {
        for ($i = 0; $i < count($positions); $i++) {
            for ($j = 0; $j < count($positions); $j++) {
                $r = $positions[$i];
                $c = $positions[$j];
                // Skip if overlapping finder
                if ($reserved[$r][$c]) continue;
                _qr_place_alignment($modules, $reserved, $r, $c);
            }
        }
    }

    // Dark module
    $modules[4 * $version + 9][8] = 1;
    $reserved[4 * $version + 9][8] = true;

    // Reserve format info areas
    for ($i = 0; $i < 8; $i++) {
        $reserved[$i][8] = true;
        $reserved[8][$i] = true;
    }
    $reserved[8][8] = true;
    for ($i = 0; $i < 7; $i++) {
        $reserved[$n - 1 - $i][8] = true;
        $reserved[8][$n - 1 - $i] = true;
    }
    $reserved[8][$n - 8] = true;

    // Encode and place data
    $codewords = _qr_encode_data($text, $version);
    _qr_place_data($modules, $reserved, $codewords, $n);

    // Apply masking — try all 8, pick best
    $best_mask = 0;
    $best_penalty = PHP_INT_MAX;
    $best_modules = null;

    for ($mask = 0; $mask < 8; $mask++) {
        $trial = _qr_apply_mask($modules, $reserved, $n, $mask);
        _qr_place_format($trial, $n, $mask);
        $penalty = _qr_penalty($trial, $n);
        if ($penalty < $best_penalty) {
            $best_penalty = $penalty;
            $best_mask = $mask;
            $best_modules = $trial;
        }
    }

    return $best_modules;
}

function _qr_place_finder(&$mod, &$res, $n, $row, $col) {
    for ($r = -1; $r <= 7; $r++) {
        for ($c = -1; $c <= 7; $c++) {
            $rr = $row + $r;
            $cc = $col + $c;
            if ($rr < 0 || $rr >= $n || $cc < 0 || $cc >= $n) continue;
            $res[$rr][$cc] = true;
            if ($r >= 0 && $r <= 6 && $c >= 0 && $c <= 6) {
                if ($r === 0 || $r === 6 || $c === 0 || $c === 6 ||
                    ($r >= 2 && $r <= 4 && $c >= 2 && $c <= 4)) {
                    $mod[$rr][$cc] = 1;
                } else {
                    $mod[$rr][$cc] = 0;
                }
            } else {
                $mod[$rr][$cc] = 0; // separator
            }
        }
    }
}

function _qr_place_alignment(&$mod, &$res, $row, $col) {
    for ($r = -2; $r <= 2; $r++) {
        for ($c = -2; $c <= 2; $c++) {
            $mod[$row + $r][$col + $c] =
                (abs($r) === 2 || abs($c) === 2 || ($r === 0 && $c === 0)) ? 1 : 0;
            $res[$row + $r][$col + $c] = true;
        }
    }
}

function _qr_place_data(&$mod, &$res, $codewords, $n) {
    $bit_idx = 0;
    $total_bits = count($codewords) * 8;
    $col = $n - 1;
    $going_up = true;

    while ($col >= 0) {
        if ($col === 6) $col--; // skip vertical timing
        for ($i = 0; $i < $n; $i++) {
            $row = $going_up ? ($n - 1 - $i) : $i;
            for ($dc = 0; $dc <= 1; $dc++) {
                $c = $col - $dc;
                if ($c < 0) continue;
                if ($res[$row][$c]) continue;
                if ($bit_idx < $total_bits) {
                    $byte_idx = intval($bit_idx / 8);
                    $bit_pos = 7 - ($bit_idx % 8);
                    $mod[$row][$c] = ($codewords[$byte_idx] >> $bit_pos) & 1;
                    $bit_idx++;
                }
            }
        }
        $going_up = !$going_up;
        $col -= 2;
    }
}

function _qr_mask_fn($mask, $r, $c) {
    switch ($mask) {
        case 0: return ($r + $c) % 2 === 0;
        case 1: return $r % 2 === 0;
        case 2: return $c % 3 === 0;
        case 3: return ($r + $c) % 3 === 0;
        case 4: return (intval($r / 2) + intval($c / 3)) % 2 === 0;
        case 5: return ($r * $c) % 2 + ($r * $c) % 3 === 0;
        case 6: return (($r * $c) % 2 + ($r * $c) % 3) % 2 === 0;
        case 7: return (($r + $c) % 2 + ($r * $c) % 3) % 2 === 0;
    }
    return false;
}

function _qr_apply_mask($mod, $res, $n, $mask) {
    $result = $mod;
    for ($r = 0; $r < $n; $r++) {
        for ($c = 0; $c < $n; $c++) {
            if (!$res[$r][$c] && _qr_mask_fn($mask, $r, $c)) {
                $result[$r][$c] ^= 1;
            }
        }
    }
    return $result;
}

function _qr_place_format(&$mod, $n, $mask) {
    // EC level L = 01, 5-bit format: 01 + 3-bit mask
    $format_data = (1 << 3) | $mask; // EC L = 01

    // BCH(15,5) encoding
    $bch = $format_data << 10;
    $gen = 0x537; // generator polynomial
    $tmp = $bch;
    for ($i = 4; $i >= 0; $i--) {
        if ($tmp & (1 << ($i + 10))) {
            $tmp ^= $gen << $i;
        }
    }
    $bch |= $tmp;
    $bch ^= 0x5412; // XOR mask

    // Place format bits
    $bits = array();
    for ($i = 14; $i >= 0; $i--) {
        $bits[] = ($bch >> $i) & 1;
    }

    // Around top-left finder
    $positions_h = array(
        array(8, 0), array(8, 1), array(8, 2), array(8, 3), array(8, 4), array(8, 5),
        array(8, 7), array(8, 8), array(7, 8), array(5, 8), array(4, 8), array(3, 8),
        array(2, 8), array(1, 8), array(0, 8)
    );
    for ($i = 0; $i < 15; $i++) {
        $mod[$positions_h[$i][0]][$positions_h[$i][1]] = $bits[$i];
    }

    // Around bottom-left and top-right finders
    $positions_v = array(
        array($n - 1, 8), array($n - 2, 8), array($n - 3, 8), array($n - 4, 8),
        array($n - 5, 8), array($n - 6, 8), array($n - 7, 8),
        array(8, $n - 8), array(8, $n - 7), array(8, $n - 6), array(8, $n - 5),
        array(8, $n - 4), array(8, $n - 3), array(8, $n - 2), array(8, $n - 1)
    );
    for ($i = 0; $i < 15; $i++) {
        $mod[$positions_v[$i][0]][$positions_v[$i][1]] = $bits[$i];
    }
}

/* ── Penalty scoring (simplified) ───────────────────────────────────── */

function _qr_penalty($mod, $n) {
    $penalty = 0;

    // Rule 1: runs of same color
    for ($r = 0; $r < $n; $r++) {
        $run = 1;
        for ($c = 1; $c < $n; $c++) {
            if ($mod[$r][$c] === $mod[$r][$c - 1]) {
                $run++;
            } else {
                if ($run >= 5) $penalty += $run - 2;
                $run = 1;
            }
        }
        if ($run >= 5) $penalty += $run - 2;
    }
    for ($c = 0; $c < $n; $c++) {
        $run = 1;
        for ($r = 1; $r < $n; $r++) {
            if ($mod[$r][$c] === $mod[$r - 1][$c]) {
                $run++;
            } else {
                if ($run >= 5) $penalty += $run - 2;
                $run = 1;
            }
        }
        if ($run >= 5) $penalty += $run - 2;
    }

    // Rule 4: proportion of dark modules
    $dark = 0;
    for ($r = 0; $r < $n; $r++)
        for ($c = 0; $c < $n; $c++)
            if ($mod[$r][$c]) $dark++;
    $pct = $dark * 100 / ($n * $n);
    $penalty += intval(abs($pct - 50) / 5) * 10;

    return $penalty;
}
