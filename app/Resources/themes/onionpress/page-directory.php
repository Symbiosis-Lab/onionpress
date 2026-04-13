<?php
/**
 * Template Name: Directory
 *
 * Search page for looking up onionnames, plus Tor Browser download links.
 * Platform detection via User-Agent puts the most relevant download first.
 */

get_header();

// Detect platform from User-Agent
$ua = strtolower( $_SERVER['HTTP_USER_AGENT'] ?? '' );
if ( strpos( $ua, 'android' ) !== false ) {
    $platform = 'android';
} elseif ( strpos( $ua, 'iphone' ) !== false || strpos( $ua, 'ipad' ) !== false ) {
    $platform = 'ios';
} elseif ( strpos( $ua, 'macintosh' ) !== false || strpos( $ua, 'mac os' ) !== false ) {
    $platform = 'mac';
} elseif ( strpos( $ua, 'windows' ) !== false ) {
    $platform = 'windows';
} elseif ( strpos( $ua, 'linux' ) !== false ) {
    $platform = 'linux';
} else {
    $platform = 'unknown';
}

$downloads = array(
    'mac'     => array( 'label' => 'Mac',     'url' => 'https://www.torproject.org/download/',          'note' => 'macOS 10.15+' ),
    'windows' => array( 'label' => 'Windows', 'url' => 'https://www.torproject.org/download/',          'note' => 'Windows 10+' ),
    'linux'   => array( 'label' => 'Linux',   'url' => 'https://www.torproject.org/download/',          'note' => '64-bit' ),
    'android' => array( 'label' => 'Android', 'url' => 'https://play.google.com/store/apps/details?id=org.torproject.torbrowser', 'note' => 'Google Play' ),
    'ios'     => array( 'label' => 'iOS',     'url' => 'https://apps.apple.com/app/onion-browser/id519296448', 'note' => 'Onion Browser' ),
);

// Put the detected platform first
$ordered = array();
if ( isset( $downloads[ $platform ] ) ) {
    $ordered[ $platform ] = $downloads[ $platform ];
}
foreach ( $downloads as $key => $dl ) {
    if ( $key !== $platform ) {
        $ordered[ $key ] = $dl;
    }
}
?>

<article class="post">
    <h1 class="post-title"><?php the_title(); ?></h1>

    <p>Look up an OnionPress site by name.</p>

    <form action="/" method="get" id="directory-search-form" style="display:flex;gap:8px;max-width:500px;margin:30px 0;">
        <input type="text" id="directory-search-input" name="q"
               placeholder="brewsterkahle"
               autocomplete="off" autocapitalize="none" spellcheck="false"
               style="flex:1;padding:10px 14px;font-size:16px;border:2px solid #d1d5db;border-radius:8px;outline:none;font-family:ui-monospace,monospace;"
               onfocus="this.style.borderColor='#7c3aed'"
               onblur="this.style.borderColor='#d1d5db'">
        <button type="submit"
                style="padding:10px 24px;font-size:16px;background:#7c3aed;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:600;">
            Look up
        </button>
    </form>

    <script>
    document.getElementById('directory-search-form').addEventListener('submit', function(e) {
        e.preventDefault();
        var name = document.getElementById('directory-search-input').value.trim().toLowerCase();
        if (name) {
            window.location.href = '/' + encodeURIComponent(name);
        }
    });
    </script>

    <hr style="margin:40px 0;border:none;border-top:1px solid #e5e5e5;">

    <h2>Get a Tor-enabled browser</h2>
    <p>To visit OnionPress sites, you need a browser that connects to the Tor network.</p>

    <?php $first = true; foreach ( $ordered as $key => $dl ) : ?>
        <div style="<?php if ( $first ) : ?>padding:16px 20px;background:#f5f0ff;border:2px solid #7c3aed;border-radius:10px;margin-bottom:12px;<?php else: ?>padding:12px 20px;background:#f9f9f9;border:1px solid #e5e5e5;border-radius:8px;margin-bottom:8px;<?php endif; ?>display:flex;align-items:center;justify-content:space-between;">
            <div>
                <strong<?php if ( $first ) : ?> style="font-size:18px;"<?php endif; ?>><?php echo esc_html( $dl['label'] ); ?></strong>
                <span style="color:#888;margin-left:8px;"><?php echo esc_html( $dl['note'] ); ?></span>
            </div>
            <a href="<?php echo esc_url( $dl['url'] ); ?>"
               style="padding:8px 18px;background:<?php echo $first ? '#7c3aed' : '#6b7280'; ?>;color:#fff;border-radius:6px;text-decoration:none;font-weight:600;font-size:14px;">
                Download
            </a>
        </div>
    <?php $first = false; endforeach; ?>
</article>

<?php get_sidebar(); ?>
<?php get_footer(); ?>
