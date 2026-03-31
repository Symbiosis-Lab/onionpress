"""Browser detection, selection, and launching for OnionPress.

Consolidates browser detection logic that was duplicated across
open_tor_browser, _auto_open_browser_inner, and show_browser_install_dialog.
"""

import json
import os
import subprocess
import time

TOR_BROWSER_PATH = "/Applications/Tor Browser.app"
BRAVE_BROWSER_PATH = "/Applications/Brave Browser.app"

# Browsers we trust for open -a / osascript activate
ALLOWED_BROWSERS = {"Firefox", "Google Chrome", "Brave Browser", "Microsoft Edge", "Safari"}

# Browsers that support the OnionPress extension
EXTENSION_BROWSERS = {
    "Firefox": "/Applications/Firefox.app",
    "Google Chrome": "/Applications/Google Chrome.app",
    "Brave Browser": "/Applications/Brave Browser.app",
    "Microsoft Edge": "/Applications/Microsoft Edge.app",
}


def is_tor_browser_installed():
    """Check if Tor Browser is installed in /Applications."""
    return os.path.isdir(TOR_BROWSER_PATH)


def is_brave_installed():
    """Check if Brave Browser is installed in /Applications."""
    return os.path.isdir(BRAVE_BROWSER_PATH)


def extension_connected_recently(app_support, allowed=ALLOWED_BROWSERS):
    """Check if a browser extension is actively connected.

    Returns the browser app name (e.g. "Firefox") if connected in the
    last 10 seconds, or None.
    """
    marker = os.path.join(app_support, "extension-connected")
    try:
        if os.path.exists(marker):
            with open(marker, 'r') as f:
                data = json.loads(f.read().strip())
            if (time.time() - data["timestamp"]) < 10:
                browser = data.get("browser")
                if browser in allowed:
                    return browser
    except Exception:
        pass
    return None


def detect_best_browser(app_support):
    """Detect the best available browser for .onion sites.

    Returns (browser_type, browser_name) where browser_type is one of:
    'tor_browser', 'extension', 'brave', or None.
    """
    if is_tor_browser_installed():
        return ('tor_browser', 'Tor Browser')

    ext = extension_connected_recently(app_support)
    if ext:
        return ('extension', ext)

    if is_brave_installed():
        return ('brave', 'Brave Browser')

    return (None, None)


def browser_menu_title(app_support):
    """Return the appropriate menu title based on available browsers."""
    btype, bname = detect_best_browser(app_support)
    if btype == 'tor_browser':
        return "Open in Tor Browser"
    if btype == 'extension':
        return f"Open in {bname}"
    if btype == 'brave':
        return "Open in Brave Browser"
    return "Open in Browser"


def open_onion_url(url, app_support, log_func):
    """Open a .onion URL in the best available browser.

    Returns True if a browser was found and opened, False if no browser available.
    """
    btype, bname = detect_best_browser(app_support)

    if btype == 'extension':
        subprocess.run(["open", "-a", bname, url])
        log_func(f"Opened {url} in {bname} (extension)")
        return True
    if btype == 'brave':
        brave_exe = os.path.join(BRAVE_BROWSER_PATH, "Contents", "MacOS", "Brave Browser")
        subprocess.run([brave_exe, "--tor", url])
        log_func(f"Opened {url} in Brave Browser (Tor mode)")
        return True
    if btype == 'tor_browser':
        subprocess.run(["open", "-a", "Tor Browser", url])
        log_func(f"Opened {url} in Tor Browser")
        return True

    return False


def installed_extension_browsers():
    """Return list of installed browsers that support the OnionPress extension."""
    return [name for name, path in EXTENSION_BROWSERS.items()
            if os.path.exists(path)]


def wait_for_app_install(app_path, executable_subpath, timeout=600,
                         check_interval=3, cancel_check=None):
    """Poll for an app to appear in /Applications.

    Args:
        app_path: Full path to the .app bundle.
        executable_subpath: Relative path to the main executable inside the bundle.
        timeout: Max seconds to wait.
        check_interval: Seconds between checks.
        cancel_check: Optional callable returning True to abort early.

    Returns True if app was detected, False on timeout/cancel.
    """
    elapsed = 0
    while elapsed < timeout:
        if cancel_check and cancel_check():
            return False
        time.sleep(check_interval)
        elapsed += check_interval

        if not os.path.isdir(app_path):
            continue

        real_path = os.path.realpath(app_path)
        if not real_path.startswith("/Applications/"):
            continue

        if not os.path.exists(os.path.join(app_path, executable_subpath)):
            continue

        return True

    return False


def wait_for_extension_active(app_support, timeout=30):
    """Wait for the browser extension to register as active.

    Returns True if extension became active within timeout.
    """
    marker = os.path.join(app_support, "extension-connected")
    for _ in range(timeout):
        try:
            with open(marker, 'r') as f:
                data = json.loads(f.read().strip())
            if (time.time() - data["timestamp"]) < 5:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False
