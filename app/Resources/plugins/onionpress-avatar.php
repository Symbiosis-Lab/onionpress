<?php
/**
 * Plugin Name: OnionPress Local Avatar
 * Description: Adds a profile photo upload to the user profile page and
 *              serves it locally instead of leaking email hashes to Gravatar.
 * Version:     1.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * Show the "Profile Photo" upload field on the user profile page.
 */
function onionpress_avatar_field( $user ) {
    if ( ! current_user_can( 'upload_files' ) ) {
        return;
    }
    $avatar_id = get_user_meta( $user->ID, 'onionpress_avatar_id', true );
    $avatar_url = $avatar_id ? wp_get_attachment_image_url( $avatar_id, 'thumbnail' ) : '';
    ?>
    <h3>Profile Photo</h3>
    <table class="form-table">
        <tr>
            <th><label for="onionpress-avatar">Photo</label></th>
            <td>
                <?php if ( $avatar_url ) : ?>
                    <img src="<?php echo esc_url( $avatar_url ); ?>"
                         style="width:96px;height:96px;border-radius:50%;object-fit:cover;display:block;margin-bottom:8px;">
                <?php endif; ?>
                <input type="file" name="onionpress_avatar" id="onionpress-avatar" accept="image/*">
                <?php if ( $avatar_id ) : ?>
                    <p>
                        <label>
                            <input type="checkbox" name="onionpress_avatar_remove" value="1">
                            Remove photo
                        </label>
                    </p>
                <?php endif; ?>
            </td>
        </tr>
    </table>
    <?php
}
add_action( 'show_user_profile', 'onionpress_avatar_field' );
add_action( 'edit_user_profile', 'onionpress_avatar_field' );

/**
 * Ensure the profile form uses multipart encoding for file uploads.
 */
function onionpress_avatar_form_enctype() {
    echo ' enctype="multipart/form-data"';
}
add_action( 'user_edit_form_tag', 'onionpress_avatar_form_enctype' );

/**
 * Save the uploaded profile photo.
 */
function onionpress_avatar_save( $user_id ) {
    if ( ! current_user_can( 'upload_files' ) ) {
        return;
    }

    // Handle removal
    if ( ! empty( $_POST['onionpress_avatar_remove'] ) ) {
        $old_id = get_user_meta( $user_id, 'onionpress_avatar_id', true );
        if ( $old_id ) {
            wp_delete_attachment( $old_id, true );
        }
        delete_user_meta( $user_id, 'onionpress_avatar_id' );
        return;
    }

    // Handle upload
    if ( empty( $_FILES['onionpress_avatar'] ) || $_FILES['onionpress_avatar']['error'] !== UPLOAD_ERR_OK ) {
        return;
    }

    require_once ABSPATH . 'wp-admin/includes/image.php';
    require_once ABSPATH . 'wp-admin/includes/file.php';
    require_once ABSPATH . 'wp-admin/includes/media.php';

    $attachment_id = media_handle_upload( 'onionpress_avatar', 0 );
    if ( is_wp_error( $attachment_id ) ) {
        return;
    }

    // Delete the old avatar attachment
    $old_id = get_user_meta( $user_id, 'onionpress_avatar_id', true );
    if ( $old_id && $old_id != $attachment_id ) {
        wp_delete_attachment( $old_id, true );
    }

    update_user_meta( $user_id, 'onionpress_avatar_id', $attachment_id );
}
add_action( 'personal_options_update', 'onionpress_avatar_save' );
add_action( 'edit_user_profile_update', 'onionpress_avatar_save' );

/**
 * Override WordPress avatars with the local upload.
 * Falls back to the default if no local avatar is set.
 */
function onionpress_avatar_override( $avatar, $id_or_email, $size, $default, $alt, $args ) {
    $user = false;

    if ( is_numeric( $id_or_email ) ) {
        $user = get_user_by( 'id', (int) $id_or_email );
    } elseif ( is_string( $id_or_email ) ) {
        $user = get_user_by( 'email', $id_or_email );
    } elseif ( $id_or_email instanceof WP_User ) {
        $user = $id_or_email;
    } elseif ( $id_or_email instanceof WP_Comment ) {
        $user = get_user_by( 'id', $id_or_email->user_id );
    }

    if ( ! $user ) {
        return $avatar;
    }

    $avatar_id = get_user_meta( $user->ID, 'onionpress_avatar_id', true );
    if ( ! $avatar_id ) {
        return $avatar;
    }

    $url = wp_get_attachment_image_url( $avatar_id, array( $size, $size ) );
    if ( ! $url ) {
        return $avatar;
    }

    return sprintf(
        '<img alt="%s" src="%s" class="avatar avatar-%d photo" height="%d" width="%d" loading="lazy">',
        esc_attr( $alt ),
        esc_url( $url ),
        (int) $size,
        (int) $size,
        (int) $size
    );
}
add_filter( 'get_avatar', 'onionpress_avatar_override', 10, 6 );

/**
 * Override avatar URL too (used by the theme's onionpress_get_avatar_url).
 */
function onionpress_avatar_url_override( $url, $id_or_email, $args ) {
    $user = false;

    if ( is_numeric( $id_or_email ) ) {
        $user = get_user_by( 'id', (int) $id_or_email );
    } elseif ( is_string( $id_or_email ) ) {
        $user = get_user_by( 'email', $id_or_email );
    } elseif ( $id_or_email instanceof WP_User ) {
        $user = $id_or_email;
    } elseif ( $id_or_email instanceof WP_Comment ) {
        $user = get_user_by( 'id', $id_or_email->user_id );
    }

    if ( ! $user ) {
        return $url;
    }

    $avatar_id = get_user_meta( $user->ID, 'onionpress_avatar_id', true );
    if ( ! $avatar_id ) {
        return $url;
    }

    $size = isset( $args['size'] ) ? (int) $args['size'] : 96;
    $local_url = wp_get_attachment_image_url( $avatar_id, array( $size, $size ) );

    return $local_url ? $local_url : $url;
}
add_filter( 'get_avatar_url', 'onionpress_avatar_url_override', 10, 3 );

