"""
Backward-compatibility shim — real implementation is in onionpress.backup.

The MenubarApp (py2app) and build script import ``backup_manager`` as a
top-level module.  This shim re-exports everything from the canonical
location so existing callers keep working without changes.
"""

from onionpress.backup import (          # noqa: F401
    verify_wp_admin,
    create_backup,
    read_backup_metadata,
    restore_from_backup,
    backup_filename,
    _get_db_credentials,
    _find_dir,
)
