"""
py2app setup script for OnionPress menubar application
"""
import re
import sys
import os
from setuptools import setup

# Read version from src/menubar.py (the canonical source)
_version_match = re.search(
    r'self\.version\s*=\s*"([^"]+)"',
    open(os.path.join(os.path.dirname(__file__), 'src', 'menubar.py')).read(),
)
VERSION = _version_match.group(1) if _version_match else 'unknown'

# Default build directories if not specified on command line
# (avoid conflicts with build/ scripts directory)
if '--dist-dir' not in ' '.join(sys.argv):
    sys.argv.extend(['--dist-dir=py2app_dist'])
if '--bdist-base' not in ' '.join(sys.argv):
    sys.argv.extend(['--bdist-base=py2app_build'])

APP = ['src/menubar.py']

# Wordlists for local onionname suggestions — bundled from the same source
# the tor container uses, so adding/editing a language only happens in one
# place.
_WORDLISTS_DIR = 'app/Resources/docker/tor/wordlists'
_WORDLIST_FILES = [
    os.path.join(_WORDLISTS_DIR, fname)
    for fname in ('__init__.py', 'en.py', 'fr.py', 'es.py', 'de.py',
                  'nl.py', 'pt.py', 'ja.py', 'zh.py', 'ar.py')
]

DATA_FILES = [
    ('', [
        'app/Resources/app-icon.png',
        'app/Resources/menubar-icon-stopped.png',
        'app/Resources/menubar-icon-starting.png',
        'app/Resources/menubar-icon-running.png',
        'src/onion-forward.php',
    ]),
    ('assets/branding', [
        'assets/branding/noun-computer-5963091.svg',
        'assets/branding/logo.png',
    ]),
    ('wordlists', _WORDLIST_FILES),
]

OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'app/Resources/AppIcon.icns',
    'plist': {
        'CFBundleName': 'OnionPress',
        'CFBundleDisplayName': 'OnionPress',
        'CFBundleIdentifier': 'press.onion.app',
        'CFBundleVersion': VERSION,
        'CFBundleShortVersionString': VERSION,
        'LSUIElement': True,  # Run as menu bar app (no dock icon)
        'NSHighResolutionCapable': True,
        'NSRequiresAquaSystemAppearance': False,
        'LSApplicationCategoryType': 'public.app-category.utilities',
        'NSLocalNetworkUsageDescription': 'OnionPress uses localhost connections to manage Docker containers and serve a local proxy. It does not access other devices on your network.',
        'NSDocumentsFolderUsageDescription': 'OnionPress saves backups and Creations in ~/Documents/OnionPress/ so they\'re easy to find and share. Access is limited to that subfolder.',
    },
    'packages': ['rumps', 'objc', 'AppKit'],
    # CRITICAL: Local modules that menubar.py imports at runtime.
    # py2app cannot auto-detect these because it runs menubar.py via exec(),
    # not import. If you add a new local .py module, ADD IT HERE or the build
    # will appear to succeed but the app will crash at launch with
    # "ModuleNotFoundError".
    'includes': ['subprocess', 'threading', 'os', 'time', 'json', 'key_manager', 'backup_manager',
                 'onion_proxy', 'install_native_messaging', 'onionheaven', 'onion_auth', 'updater',
                 'onionpress', 'onionpress.backup', 'onionpress.platform', 'onionpress.docker',
                 'onionpress.config', 'onionpress.health', 'onionpress.containers',
                 'onionpress.tor', 'onionpress.colima',
                 'onionpress.ui_helpers', 'onionpress.settings_ui',
                 'onionpress.browser', 'onionpress.log_rotation',
                 'onionpress.analytics_sharing',
                 'onionpress.onionnames_client',
                 'onionpress.onionnames_registrar', 'setup_window'],
    'excludes': ['tkinter', 'test', 'unittest'],
    'arch': 'universal2',  # Build for both Intel and Apple Silicon
    'strip': True,  # Strip debug symbols to reduce size
    'optimize': 2,  # Optimize Python bytecode
}

setup(
    name='OnionPress',
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
