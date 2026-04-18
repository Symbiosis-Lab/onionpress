"""
Backup and Restore for OnionPress

Creates password-protected zip archives containing Tor keys, WordPress database,
and wp-content (themes, plugins, uploads).
"""

import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone

from onionpress import key_manager
from onionpress.config import DEFAULTS, read_config, write_value


def _default_data_dir() -> str:
    """Resolve the default OnionPress data directory at call time.

    Deliberately not cached: callers (and tests) may mutate $HOME between
    imports and the first use of backup functions.
    """
    return os.path.expanduser("~/.onionpress")


# Multisite constants that WordPress needs in wp-config.php for a network install.
# After restore, the WordPress container generates a fresh wp-config.php without
# these, causing `wp core is-installed --network` to fail and ensure_multisite to
# skip.  We re-add them after every restore so the site works immediately.
_MULTISITE_CONSTANTS = {
    'WP_ALLOW_MULTISITE': 'true',
    'MULTISITE': 'true',
    'SUBDOMAIN_INSTALL': 'false',
    'DOMAIN_CURRENT_SITE': "'localhost'",
    'PATH_CURRENT_SITE': "'/'",
    'SITE_ID_CURRENT_SITE': '1',
    'BLOG_ID_CURRENT_SITE': '1',
}


def verify_wp_admin(username, password):
    """Verify that the given credentials belong to a WordPress administrator.

    Returns (True, None) on success, or (False, error_message) on failure.
    """
    # Verify user exists and has administrator role
    try:
        result = subprocess.run(
            ['docker', 'exec', 'onionpress-wordpress',
             'wp', 'user', 'get', username, '--field=roles', '--allow-root'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if 'Invalid user' in stderr:
                return (False, f"User '{username}' does not exist in WordPress.")
            return (False, f"Could not look up user: {stderr}")

        roles = result.stdout.strip()
        if 'administrator' not in roles:
            return (False, f"User '{username}' is not an administrator (roles: {roles}).")
    except subprocess.TimeoutExpired:
        return (False, "Timed out connecting to WordPress container.")
    except Exception as e:
        return (False, f"Error checking user role: {e}")

    # Verify password by piping username and password via stdin.
    # Never embed user input into the PHP code string (injection risk).
    php_code = (
        "$lines = explode(\"\\n\", trim(file_get_contents('php://stdin')));"
        "$user = $lines[0];"
        "$pw = $lines[1];"
        "$u = wp_authenticate($user, $pw);"
        "if (is_wp_error($u)) { fwrite(STDERR, $u->get_error_message()); exit(1); }"
        "echo 'ok';"
    )
    try:
        result = subprocess.run(
            ['docker', 'exec', '-i', 'onionpress-wordpress',
             'wp', 'eval', php_code, '--allow-root'],
            input=username + "\n" + password,
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15
        )
        if result.returncode != 0:
            return (False, f"Incorrect password for '{username}'.")
    except subprocess.TimeoutExpired:
        return (False, "Timed out verifying password.")
    except Exception as e:
        return (False, f"Error verifying password: {e}")

    return (True, None)


def create_backup(onion_address, username, password, output_path, version, log_func, *, data_dir=None):
    """Create a full OnionPress backup zip.

    Args:
        onion_address: Current .onion address
        username: WP admin username (stored in metadata)
        password: Zip encryption password
        data_dir: OnionPress data directory (defaults to ~/.onionpress). Tests pass a sandbox path.
        output_path: Where to write the .zip file
        version: OnionPress version string
        log_func: Callable for progress logging
    """
    staging = tempfile.mkdtemp(prefix='onionpress-backup-')
    try:
        # 1. Extract Tor keys (Arti OpenSSH keystore format)
        log_func("Backup: extracting Tor keys...")
        tor_dir = os.path.join(staging, 'tor-keys')
        os.makedirs(tor_dir)

        priv = key_manager.extract_private_key()
        pub = key_manager.extract_public_key()
        pem_data = key_manager.build_openssh_key(priv, pub)
        with open(os.path.join(tor_dir, 'ks_hs_id.ed25519_expanded_private'), 'wb') as f:
            f.write(pem_data)

        # 2. Dump WordPress database via mariadb-dump in the db container
        # (wp db export uses mysqldump which isn't in the WordPress container)
        log_func("Backup: exporting database...")
        db_dir = os.path.join(staging, 'database')
        os.makedirs(db_dir)

        db_creds = _get_db_credentials()
        result = subprocess.run(
            ['docker', 'exec', 'onionpress-db',
             'mariadb-dump',
             '-u', db_creds['user'],
             '-p' + db_creds['password'],
             db_creds['name']],
            capture_output=True, timeout=120
        )
        if result.returncode != 0:
            raise Exception(f"Database export failed: {result.stderr.decode(errors='replace')}")
        with open(os.path.join(db_dir, 'wordpress.sql'), 'wb') as f:
            f.write(result.stdout)

        # 3. Copy wp-content from container
        log_func("Backup: copying wp-content (themes, plugins, uploads)...")
        wpcontent_dir = os.path.join(staging, 'wp-content')
        subprocess.run(
            ['docker', 'cp',
             'onionpress-wordpress:/var/www/html/wp-content/.',
             wpcontent_dir],
            capture_output=True, timeout=300, check=True
        )

        # 4. Backup OnionHeaven data if this is OnionHeaven instance
        #    (encrypted keys, master-key.json, registry — NOT the ephemeral unlock file)
        is_onionheaven = False
        onionheaven_check = subprocess.run(
            ['docker', 'exec', 'onionpress-wordpress',
             'test', '-f', '/var/lib/onionpress/onionheaven/master-key.json'],
            capture_output=True, timeout=10
        )
        if onionheaven_check.returncode == 0:
            log_func("Backup: copying OnionHeaven data (encrypted keys, registry)...")
            is_onionheaven = True
            onionheaven_dir = os.path.join(staging, 'onionheaven')
            subprocess.run(
                ['docker', 'cp',
                 'onionpress-wordpress:/var/lib/onionpress/onionheaven/.',
                 onionheaven_dir],
                capture_output=True, timeout=60, check=True
            )
            # Remove the ephemeral unlock file if it was copied
            unlocked_file = os.path.join(onionheaven_dir, '.master-key-unlocked')
            if os.path.exists(unlocked_file):
                os.unlink(unlocked_file)

        # 4b. Backup OnionHome name registry if this is an OnionHome instance.
        #     The DB is tiny (KB) but losing it strands every inbound
        #     /api/name/lookup/NAME that ever pointed here — it's the
        #     canonical record of who's who in the onion directory.
        is_onionhome = False
        onionhome_check = subprocess.run(
            ['docker', 'exec', 'onionpress-wordpress',
             'test', '-f', '/var/lib/onionpress/onionhome/onionnames.db'],
            capture_output=True, timeout=10
        )
        if onionhome_check.returncode == 0:
            log_func("Backup: copying OnionHome name registry...")
            is_onionhome = True
            onionhome_dir = os.path.join(staging, 'onionhome')
            subprocess.run(
                ['docker', 'cp',
                 'onionpress-wordpress:/var/lib/onionpress/onionhome/.',
                 onionhome_dir],
                capture_output=True, timeout=60, check=True
            )

        # 5. Save non-default config values
        _data_dir = data_dir if data_dir is not None else _default_data_dir()
        config_path = os.path.join(_data_dir, 'config')
        if os.path.exists(config_path):
            current = read_config(config_path)
            overrides = {k: v for k, v in current.items()
                         if k in DEFAULTS and v != DEFAULTS[k]}
            if overrides:
                with open(os.path.join(staging, 'config-overrides.json'), 'w') as f:
                    json.dump(overrides, f, indent=2)
                log_func(f"Backup: saved {len(overrides)} config override(s)")

        # 7. Write metadata
        metadata = {
            'onion_address': onion_address,
            'backup_date': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'onionpress_version': version,
            'username': username,
            'is_onionheaven': is_onionheaven,
            'is_onionhome': is_onionhome,
        }
        with open(os.path.join(staging, 'metadata.json'), 'w') as f:
            json.dump(metadata, f, indent=2)

        # 8. Create password-protected zip using macOS system zip
        log_func("Backup: creating encrypted zip archive...")
        # Remove target if it already exists (zip would append otherwise)
        if os.path.exists(output_path):
            os.unlink(output_path)

        result = subprocess.run(
            ['zip', '-r', '-P', password, output_path, '.'],
            cwd=staging,
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600
        )
        if result.returncode != 0:
            raise Exception(f"zip failed: {result.stderr}")

        log_func("Backup: complete")

    finally:
        shutil.rmtree(staging, ignore_errors=True)


def read_backup_metadata(zip_path, password):
    """Read metadata.json from a backup zip.

    Returns the metadata dict.
    Raises on bad password, missing metadata, or invalid zip.
    """
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            # Try to find metadata.json (may be at root or ./metadata.json)
            metadata_name = None
            for name in zf.namelist():
                if name == 'metadata.json' or name == './metadata.json':
                    metadata_name = name
                    break
            if metadata_name is None:
                raise ValueError("Not a valid OnionPress backup (no metadata.json found)")

            data = zf.read(metadata_name, pwd=password.encode())
            return json.loads(data)
    except RuntimeError as e:
        if 'password' in str(e).lower() or 'Bad password' in str(e):
            raise ValueError("Incorrect password for this backup.")
        raise
    except zipfile.BadZipFile:
        raise ValueError("Not a valid zip file.")


def restore_from_backup(zip_path, password, log_func, *, data_dir=None):
    """Restore an OnionPress site from a backup zip.

    Args:
        zip_path: Path to the backup .zip
        password: Zip password
        log_func: Callable for progress logging
        data_dir: OnionPress data directory (defaults to ~/.onionpress). Tests pass a sandbox path.

    Returns:
        metadata dict from the backup
    """
    staging = tempfile.mkdtemp(prefix='onionpress-restore-')
    try:
        # Extract zip
        log_func("Restore: extracting backup archive...")
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(staging, pwd=password.encode())

        # Normalize paths -- zip may have ./ prefix
        metadata_path = os.path.join(staging, 'metadata.json')
        if not os.path.exists(metadata_path):
            metadata_path = os.path.join(staging, '.', 'metadata.json')
        with open(metadata_path) as f:
            metadata = json.load(f)

        # Find extracted content directories
        tor_dir = _find_dir(staging, 'tor-keys')
        db_dir = _find_dir(staging, 'database')
        wpcontent_dir = _find_dir(staging, 'wp-content')

        # 1. Restore Tor keys (Arti OpenSSH keystore format)
        log_func("Restore: writing Tor keys...")
        key_path = os.path.join(tor_dir, 'ks_hs_id.ed25519_expanded_private')
        if not os.path.exists(key_path):
            raise Exception("Backup is missing ks_hs_id.ed25519_expanded_private")

        with open(key_path, 'rb') as f:
            pem_data = f.read()
        priv, pub = key_manager.parse_openssh_key(pem_data)
        key_manager.write_private_key(priv, pub)

        # Remove arti-state volume so it gets recreated from vanity-keys
        # on next launch. This avoids stale key mismatches.
        log_func("Restore: removing arti-state volume for clean restart...")
        subprocess.run(
            ['docker', 'volume', 'rm', 'onionpress-arti-state'],
            capture_output=True, timeout=15
        )

        # Sync vanity-keys directory on host so OnionHeaven detection and
        # prefix mismatch logic can see the restored onion address.
        onion_address = metadata.get('onion_address', '')
        _data_dir = data_dir if data_dir is not None else _default_data_dir()
        if onion_address:
            vanity_dir = os.path.join(_data_dir, 'shared', 'vanity-keys')
            addr_dir = os.path.join(vanity_dir, onion_address)
            # Clear only this address's cache — sibling address dirs (e.g.
            # other vanity prefixes the user has generated) are preserved.
            if os.path.isdir(addr_dir):
                shutil.rmtree(addr_dir)
            os.makedirs(addr_dir, exist_ok=True)
            # Copy the key file so generate_vanity_address isn't needed
            shutil.copy2(key_path, os.path.join(addr_dir, 'ks_hs_id.ed25519_expanded_private'))
            # Write hostname file
            with open(os.path.join(addr_dir, 'hostname'), 'w') as hf:
                hf.write(onion_address + '\n')
            log_func(f"Restore: synced vanity-keys for {onion_address}")

            # Update ADDRESS_PREFIX in config to match restored address
            # so the prefix mismatch detector doesn't regenerate on next start
            addr_base = onion_address.replace('.onion', '')
            config_path = os.path.join(_data_dir, 'config')
            if os.path.exists(config_path):
                with open(config_path, 'r', encoding='utf-8') as cf:
                    lines = cf.readlines()
                found = False
                for i, line in enumerate(lines):
                    if line.strip().startswith('ADDRESS_PREFIX='):
                        old_prefix = line.strip().split('=', 1)[1]
                        if not addr_base.startswith(old_prefix):
                            plen = min(max(len(old_prefix), 3), 6)
                            new_prefix = addr_base[:plen]
                            lines[i] = f'ADDRESS_PREFIX={new_prefix}\n'
                            log_func(f"Restore: updated ADDRESS_PREFIX to {new_prefix}")
                        found = True
                        break
                if not found:
                    lines.append(f'ADDRESS_PREFIX={addr_base[:3]}\n')
                with open(config_path, 'w', encoding='utf-8') as cf:
                    cf.writelines(lines)

        # 2. Restore database via mariadb CLI in the db container
        log_func("Restore: importing database...")
        sql_path = os.path.join(db_dir, 'wordpress.sql')
        if not os.path.exists(sql_path):
            raise Exception("Backup is missing wordpress.sql")

        db_creds = _get_db_credentials()

        # Copy SQL into db container then import
        subprocess.run(
            ['docker', 'cp', sql_path, 'onionpress-db:/tmp/wordpress.sql'],
            capture_output=True, timeout=30, check=True
        )
        result = subprocess.run(
            ['docker', 'exec', 'onionpress-db',
             'mariadb',
             '-u', db_creds['user'],
             '-p' + db_creds['password'],
             db_creds['name'],
             '-e', 'source /tmp/wordpress.sql'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120
        )
        if result.returncode != 0:
            raise Exception(f"Database import failed: {result.stderr}")

        # Clean up SQL file in container
        subprocess.run(
            ['docker', 'exec', 'onionpress-db', 'rm', '-f', '/tmp/wordpress.sql'],
            capture_output=True, timeout=10
        )

        # 3. Restore wp-content
        if wpcontent_dir and os.path.isdir(wpcontent_dir):
            log_func("Restore: copying wp-content (themes, plugins, uploads)...")
            subprocess.run(
                ['docker', 'cp',
                 wpcontent_dir + '/.',
                 'onionpress-wordpress:/var/www/html/wp-content/'],
                capture_output=True, timeout=300, check=True
            )
            subprocess.run(
                ['docker', 'exec', 'onionpress-wordpress',
                 'chown', '-R', 'www-data:www-data', '/var/www/html/wp-content/'],
                capture_output=True, timeout=60
            )

        # 4. Restore OnionHeaven data if present in backup
        onionheaven_dir = _find_dir(staging, 'onionheaven')
        if os.path.isdir(onionheaven_dir) and os.path.exists(os.path.join(onionheaven_dir, 'master-key.json')):
            log_func("Restore: restoring OnionHeaven data (encrypted keys, registry)...")
            # Remove ephemeral unlock file if it somehow exists in the backup
            unlocked_file = os.path.join(onionheaven_dir, '.master-key-unlocked')
            if os.path.exists(unlocked_file):
                os.unlink(unlocked_file)
            # Ensure OnionHeaven directory exists in container
            subprocess.run(
                ['docker', 'exec', 'onionpress-wordpress',
                 'mkdir', '-p', '/var/lib/onionpress/onionheaven'],
                capture_output=True, timeout=10
            )
            subprocess.run(
                ['docker', 'cp',
                 onionheaven_dir + '/.',
                 'onionpress-wordpress:/var/lib/onionpress/onionheaven/'],
                capture_output=True, timeout=60, check=True
            )
            subprocess.run(
                ['docker', 'exec', 'onionpress-wordpress',
                 'chown', '-R', 'www-data:www-data', '/var/lib/onionpress/onionheaven/'],
                capture_output=True, timeout=30
            )
            log_func("Restore: OnionHeaven data restored (OnionHeaven will be locked until admin login)")

        # 4b. Restore OnionHome name registry if present in backup.
        #     Kept root-owned — the tor container reads/writes these files
        #     as root, same as OnionHeaven's tor-container-root files
        #     alongside the www-data restore target above.
        onionhome_dir = _find_dir(staging, 'onionhome')
        if os.path.isdir(onionhome_dir) and os.path.exists(os.path.join(onionhome_dir, 'onionnames.db')):
            log_func("Restore: restoring OnionHome name registry...")
            subprocess.run(
                ['docker', 'exec', 'onionpress-wordpress',
                 'mkdir', '-p', '/var/lib/onionpress/onionhome'],
                capture_output=True, timeout=10
            )
            subprocess.run(
                ['docker', 'cp',
                 onionhome_dir + '/.',
                 'onionpress-wordpress:/var/lib/onionpress/onionhome/'],
                capture_output=True, timeout=60, check=True
            )
            log_func("Restore: OnionHome name registry restored")

        # 5. Re-add multisite constants to wp-config.php
        # WordPress Docker image generates a fresh wp-config.php without multisite
        # constants when the container is recreated. Without these, wp core
        # is-installed --network fails and ensure_multisite skips conversion.
        _ensure_multisite_constants(log_func)

        # 6. Restore config overrides (non-default user preferences)
        overrides_path = os.path.join(staging, 'config-overrides.json')
        if not os.path.exists(overrides_path):
            overrides_path = os.path.join(staging, '.', 'config-overrides.json')
        if os.path.exists(overrides_path):
            with open(overrides_path, 'r') as f:
                overrides = json.load(f)
            config_path = os.path.join(_data_dir, 'config')
            for key, value in overrides.items():
                write_value(config_path, key, value)
            log_func(f"Restore: applied {len(overrides)} config override(s)")

        log_func("Restore: files restored successfully")
        return metadata

    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _ensure_multisite_constants(log_func):
    """Re-add multisite constants to wp-config.php after restore.

    This fixes the bug where WordPress Docker generates a fresh wp-config.php
    without MULTISITE, SUBDOMAIN_INSTALL, etc., causing wp core is-installed
    to fail and ensure_multisite to skip.

    Only adds constants if the database actually has multisite tables (wp_blogs).
    """
    # Check if the database has multisite tables before adding constants
    try:
        result = subprocess.run(
            ['docker', 'exec', 'onionpress-wordpress',
             'wp', 'db', 'query', "SHOW TABLES LIKE 'wp_blogs';", '--allow-root'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15
        )
        if result.returncode != 0 or 'wp_blogs' not in result.stdout:
            return  # Not a multisite install, nothing to do
    except (subprocess.TimeoutExpired, Exception):
        return  # Can't check, skip rather than break single-site installs

    log_func("Restore: re-adding multisite constants to wp-config.php...")
    for name, value in _MULTISITE_CONSTANTS.items():
        subprocess.run(
            ['docker', 'exec', 'onionpress-wordpress',
             'wp', 'config', 'set', name, value,
             '--raw', '--type=constant', '--allow-root'],
            capture_output=True, timeout=15
        )


def _get_db_credentials():
    """Read WordPress database credentials from wp-config.php via WP-CLI."""
    creds = {}
    for field in ('DB_NAME', 'DB_USER', 'DB_PASSWORD'):
        result = subprocess.run(
            ['docker', 'exec', 'onionpress-wordpress',
             'wp', 'config', 'get', field, '--allow-root'],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15
        )
        if result.returncode != 0:
            raise Exception(f"Could not read {field} from WordPress config")
        creds[field] = result.stdout.strip()
    return {
        'name': creds['DB_NAME'],
        'user': creds['DB_USER'],
        'password': creds['DB_PASSWORD'],
    }


def _find_dir(staging, name):
    """Find a directory inside the staging area, handling ./ prefix from zip."""
    path = os.path.join(staging, name)
    if os.path.isdir(path):
        return path
    path = os.path.join(staging, '.', name)
    if os.path.isdir(path):
        return path
    return os.path.join(staging, name)  # return expected path even if missing


def backup_filename(onion_address, username):
    """Generate the default backup filename."""
    # Strip .onion suffix for brevity in filename
    addr_short = onion_address.replace('.onion', '') if onion_address else 'unknown'
    # Use first 8 chars of onion address
    addr_prefix = addr_short[:8]
    ts = datetime.now().strftime('%Y-%m-%d-%H-%M')
    return f"OnionPress-{addr_prefix}-{username}-{ts}.zip"
