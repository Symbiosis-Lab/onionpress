#!/usr/bin/env python3
"""
onionpress Menu Bar Application
Provides a simple menu bar interface to control the WordPress + Tor onion service
"""

import rumps
import subprocess
import os
import threading
import time
import json
import plistlib
import sys
from datetime import datetime
import AppKit
import signal
import socket
import atexit
import re

# Add scripts directory to path for imports
script_dir = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, script_dir)

import key_manager
import backup_manager
import onion_proxy
import install_native_messaging
import onionheaven
try:
    import setup_window
except ImportError:
    setup_window = None
from onionpress.platform import resolve_paths
from onionpress.docker import Docker
from onionpress.health import HealthChecker
from onionpress import config as op_config
from onionpress.ui_helpers import (
    HelpButtonTarget as _HelpButtonTarget,
    parse_version,
    main_thread as _main_thread,
    set_main_thread_logger as _set_main_thread_logger,
    BackupProgressWindow as _BackupProgressWindow,
    LogViewerActions as _LogViewerActions,
    LogViewerWindow as _LogViewerWindow,
)
from onionpress import browser as op_browser
from onionpress.log_rotation import RotatingLog
from onionpress import analytics_sharing


class OnionPressApp(rumps.App):
    def __init__(self):
        # Get paths first (fast - no I/O)
        self.app_support = os.path.expanduser("~/.onionpress")
        self.script_dir = os.path.dirname(os.path.realpath(__file__))

        # Single-instance safety net via PID file
        self.pid_file = os.path.join(self.app_support, "menubar.pid")
        os.makedirs(self.app_support, exist_ok=True)
        if os.path.exists(self.pid_file):
            try:
                with open(self.pid_file) as f:
                    old_pid = int(f.read().strip())
                # Check if that PID is still alive
                os.kill(old_pid, 0)
                # Process is alive — signal reopen and exit
                reopen_file = os.path.join(self.app_support, ".reopen")
                with open(reopen_file, 'w') as f:
                    f.write(str(os.getpid()))
                sys.exit(0)
            except (ProcessLookupError, ValueError, OSError):
                # Stale PID file — continue launching
                pass
        # Write our PID
        with open(self.pid_file, 'w') as f:
            f.write(str(os.getpid()))
        # Register cleanup for normal exit
        atexit.register(self._remove_pid_file)
        # Register signal handlers for clean removal on SIGTERM/SIGINT
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        # SIGUSR1 not used — py2app/NSApplication overrides signal handlers.
        # Instead, upload-analytics uses a file-based trigger (see .upload-analytics).

        # When running as py2app bundle, __file__ is in Contents/Resources/
        # so we need to use that as resources_dir, not the parent
        if getattr(sys, 'frozen', False):
            # Running as py2app bundle
            # __file__ is like: .../MenubarApp/Contents/Resources/menubar.py (in zip)
            # MenubarApp is nested inside OnionPress.app
            # Structure: OnionPress.app/Contents/Resources/MenubarApp/Contents/Resources/menubar.py
            menubar_resources_dir = os.path.join(os.environ.get('RESOURCEPATH', ''))
            if not menubar_resources_dir:
                # Fallback: get from bundle structure
                bundle_contents = os.path.dirname(os.path.dirname(self.script_dir))
                menubar_resources_dir = os.path.join(bundle_contents, 'Resources')

            # Keep menubar resources for icons
            self.resources_dir = menubar_resources_dir

            # Navigate to parent OnionPress.app bundle for launcher script and bin dir
            # MenubarApp/Contents/Resources -> MenubarApp/Contents -> MenubarApp -> OnionPress.app/Resources -> OnionPress.app/Contents
            menubar_contents = os.path.dirname(menubar_resources_dir)  # MenubarApp/Contents
            menubar_app = os.path.dirname(menubar_contents)  # MenubarApp
            parent_resources = os.path.dirname(menubar_app)  # OnionPress.app/Contents/Resources
            self.parent_resources_dir = parent_resources  # Store for accessing docker/ and other parent resources
            self.contents_dir = os.path.dirname(parent_resources)  # OnionPress.app/Contents
            self.macos_dir = os.path.join(self.contents_dir, "MacOS")
            self.launcher_script = os.path.join(self.macos_dir, "onionpress")
            self.bin_dir = os.path.join(parent_resources, "bin")
        else:
            # Running as regular Python script
            self.resources_dir = os.path.dirname(self.script_dir)
            self.parent_resources_dir = self.resources_dir  # Same as resources_dir when not bundled
            self.contents_dir = os.path.dirname(self.resources_dir)
            self.macos_dir = os.path.join(self.contents_dir, "MacOS")
            self.launcher_script = os.path.join(self.macos_dir, "onionpress")
            self.bin_dir = os.path.join(self.resources_dir, "bin")
        self.colima_home = os.path.join(self.app_support, "colima")
        self.info_plist = os.path.join(self.contents_dir, "Info.plist")
        logs_dir = os.path.join(self.app_support, "logs")
        self._onionpress_log = RotatingLog(logs_dir, "onionpress")
        self._wp_access_log = RotatingLog(logs_dir, "wordpress-access")
        self._wp_visitors_log = RotatingLog(logs_dir, "wordpress-visitors")
        self._tor_log = RotatingLog(logs_dir, "container-tor")
        self._onionheaven_log = RotatingLog(logs_dir, "container-onionheaven")
        self._clearnet_log = RotatingLog(logs_dir, "clearnet")
        self._clearnet_last_offset = 0  # track dmesg position
        self._container_log_processes = {}  # name -> (process, thread)
        self.log_file = self._onionpress_log.current_path()  # backward compat
        self.config_file = os.path.join(self.app_support, "config")

        # Create OnionPressPaths and Docker/HealthChecker for module interop
        if getattr(sys, 'frozen', False):
            _app_bundle = os.path.dirname(self.contents_dir)
        else:
            _app_bundle = None
        self._paths = resolve_paths(data_dir=self.app_support, app_bundle=_app_bundle)
        self._docker = Docker(self._paths, log_func=self.log)
        self._health_checker = HealthChecker(self._docker, log_func=self.log)

        # Initialize rumps WITHOUT icon first (fastest possible)
        super(OnionPressApp, self).__init__("", quit_button=None, template=False)

        # Detect first-run early so we can show the right window.
        # Use .setup_complete marker (written by Python after setup finishes)
        # instead of secrets (which the launcher recreates before Python starts).
        self._is_first_run = False
        setup_marker = os.path.join(self.app_support, ".setup_complete")
        if not os.path.exists(setup_marker):
            self._is_first_run = True
        # Check FORCE_SETUP_WINDOW in config
        config_file = os.path.join(self.app_support, "config")
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        if line.strip() == "FORCE_SETUP_WINDOW=yes":
                            self._is_first_run = True
                            break
            except Exception:
                pass

        # Always show splash first — setup window replaces it from auto_start()
        self.launch_splash = None
        self.launch_splash_time_field = None
        self.show_launch_splash()

        # Now load icon files (this does I/O but splash is already showing)
        self.icon_running = os.path.join(self.resources_dir, "menubar-icon-running.png")
        self.icon_stopped = os.path.join(self.resources_dir, "menubar-icon-stopped.png")
        self.icon_starting = os.path.join(self.resources_dir, "menubar-icon-starting.png")

        # Set the stopped icon
        self.icon = self.icon_stopped

        # Set version to placeholder (will be updated in background)
        self.version = "2.4.44"

        # Set up environment variables (fast - no I/O)
        docker_config_dir = os.path.join(self.app_support, "docker-config")
        os.environ["PATH"] = f"{self.bin_dir}:{os.environ.get('PATH', '')}"
        os.environ["COLIMA_HOME"] = self.colima_home
        os.environ["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")
        os.environ["LIMA_INSTANCE"] = "onionpress"
        os.environ["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
        os.environ["DOCKER_CONFIG"] = docker_config_dir

        # Stop any orphaned Colima VM from a previous crash before port detection
        colima_bin = os.path.join(self.bin_dir, "colima")
        op_config.stop_stale_colima(colima_bin, self.colima_home, self.pid_file)

        # Detect port offset for multi-user support
        _port_config = op_config.detect_port_offset()
        self.wp_port = _port_config.wp_port
        self.socks_port = _port_config.socks_port
        self.proxy_port = _port_config.proxy_port
        os.environ["ONIONPRESS_PORT_OFFSET"] = str(_port_config.offset)
        os.environ["ONIONPRESS_WP_PORT"] = str(self.wp_port)
        os.environ["ONIONPRESS_SOCKS_PORT"] = str(self.socks_port)
        os.environ["ONIONPRESS_PROXY_PORT"] = str(self.proxy_port)
        # Update onion_proxy module globals (already imported with defaults)
        onion_proxy.PROXY_PORT = self.proxy_port
        onion_proxy.PHP_PROXY_PORT = self.wp_port

        # Update OnionHeaven hub address from config
        oh_addr = self._read_config_value(
            "ONIONHEAVEN_ADDRESS",
            "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion")
        if oh_addr:
            onionheaven.ONIONHEAVEN_ADDRESS = oh_addr

        # Do slow I/O operations in background after icon appears
        def background_init():
            # Hook up the UI crash logger so main_thread() exceptions
            # get written to the onionpress log (and uploaded via analytics)
            _set_main_thread_logger(self.log)

            # Session separator and debug info via rotating log
            self.log("=" * 60)
            self.log("=== New session starting ===")
            self.log("=" * 60)
            self.log(f"DEBUG: frozen={getattr(sys, 'frozen', False)}")
            self.log(f"DEBUG: resources_dir={self.resources_dir}")
            self.log(f"DEBUG: bin_dir={self.bin_dir}")
            self.log(f"DEBUG: launcher_script={self.launcher_script}")
            self.log(f"DEBUG: icon_stopped exists={os.path.exists(self.icon_stopped)}")
            self.log(f"DEBUG: icon_stopped path={self.icon_stopped}")
            self.log(f"DEBUG: rumps initialized successfully")

            # Create Docker config without credential store (avoids docker-credential-osxkeychain errors)
            os.makedirs(docker_config_dir, exist_ok=True)
            docker_config_file = os.path.join(docker_config_dir, "config.json")
            if not os.path.exists(docker_config_file):
                with open(docker_config_file, 'w') as f:
                    f.write('{\n\t"auths": {},\n\t"currentContext": "colima"\n}\n')

            # Install docker-compose plugin: prefer bundled, fall back to system
            cli_plugins_dir = os.path.join(docker_config_dir, "cli-plugins")
            os.makedirs(cli_plugins_dir, exist_ok=True)
            compose_plugin_dest = os.path.join(cli_plugins_dir, "docker-compose")
            bundled_compose = os.path.join(self.bin_dir, "docker-compose")
            system_compose = os.path.expanduser("~/.docker/cli-plugins/docker-compose")
            if os.path.isfile(bundled_compose) and not os.path.exists(compose_plugin_dest):
                try:
                    os.symlink(bundled_compose, compose_plugin_dest)
                except Exception:
                    pass
            elif os.path.islink(system_compose) and not os.path.exists(compose_plugin_dest):
                try:
                    os.symlink(system_compose, compose_plugin_dest)
                except Exception:
                    pass

            # Get actual version from Info.plist
            self.version = self.get_version()

            # Log version information at startup
            self.log_version_info()

            # Update browser menu title after checking filesystem
            self.update_browser_menu_title()

            # Install native messaging manifests for browser extension support
            try:
                install_native_messaging.install(log_func=self.log)
            except Exception as e:
                self.log(f"Native messaging install failed: {e}")

            # Sync login item LaunchAgent with config
            launch_on_login = self._read_config_value("LAUNCH_ON_LOGIN", "yes")
            if launch_on_login == "yes" and not self._is_login_item_installed():
                self.add_login_item()
            elif launch_on_login != "yes" and self._is_login_item_installed():
                self.remove_login_item()

            # Check if Cloudflare Tunnel is configured
            cf_token = self._read_config_value("CLOUDFLARE_TUNNEL_TOKEN")
            if cf_token:
                self.cloudflare_tunnel_enabled = True
                self.log("Cloudflare Tunnel configured")

        # Start background initialization
        threading.Thread(target=background_init, daemon=True).start()

        # State — load cached onion address from previous run if available
        cached_addr_file = os.path.join(self.app_support, "onion_address")
        try:
            with open(cached_addr_file) as f:
                cached = f.read().strip()
            if cached and cached.endswith('.onion'):
                self.onion_address = cached
            else:
                self.onion_address = "Starting..."
        except (OSError, IOError):
            self.onion_address = "Starting..."
        self.is_running = False
        self.is_ready = False  # WordPress is ready to serve requests
        self._sleeping = False  # True between sleep/wake events — suppresses heartbeats
        self.checking = False
        self._checking_lock = threading.Lock()  # Protect self.checking from race conditions
        self.web_log_process = None  # Background process for web logs
        self.web_log_file_handle = None  # File handle for web log capture
        self.last_status_logged = None  # Track last logged status to avoid spam
        self.auto_opened_browser = False  # Track if we've auto-opened browser this session
        self.setup_dialog_showing = False  # Track if setup dialog is currently showing
        self.setup_alert = None  # Reference to NSAlert for programmatic dismissal
        self.monitoring_tor_install = False  # Track if we're monitoring for Tor Browser installation
        self.caffeinate_process = None  # Process handle for caffeinate to prevent sleep
        self.proxy_server = None  # Onion proxy HTTP server instance
        self.proxy_thread = None  # Thread running the proxy server
        self._wp_installed = None  # None = unknown, True/False = checked
        self._wp_not_installed_count = 0  # Consecutive "not installed" results
        self._setup_page_opened = False  # Track if we've opened the setup page
        self._port_conflict = False  # True if ports are in use by another instance
        self._ports_checked = False  # True after port conflict check completes
        self._has_internet = True          # Host-level internet connectivity
        self._last_bootstrap_pct = 0       # Last observed Tor bootstrap percentage
        self._bootstrap_stall_count = 0    # Consecutive checks with no bootstrap progress
        self._yellow_since = None          # Timestamp when entered yellow state
        self._was_ready = False            # Were we ever ready this session?
        self._tor_internally_ready = False # Checks 1-4 passed (Arti+WordPress up)
        # Reclaim fields kept for compatibility (notify_onionheaven_online still sets them)
        self._onionheaven_reclaim_succeeded = False
        self._onionheaven_reclaim_in_flight = False
        self._onionheaven_reclaim_last_attempt = 0
        self._wordpress_confirmed = False  # WordPress responded at least once (stays up reliably)
        self.healthcheck_address = None    # Healthcheck .onion address
        self.onionheaven_messages = []          # Messages received from OnionHeaven
        self._onionheaven_alert_shown = False   # Whether we've shown OnionHeaven alert icon
        self.is_onionheaven = False             # True if this instance is OnionHeaven
        self._onionheaven_checked = False       # Whether onionheaven mode has been checked
        self._onionheaven_registration_succeeded = False  # Whether registration succeeded
        self._onionheaven_heartbeat_succeeded = False     # Suppresses repeat heartbeat logs
        self._onionheaven_registration_in_flight = False  # Whether registration thread is running
        self.cloudflare_tunnel_enabled = False  # True when CLOUDFLARE_TUNNEL_TOKEN is set
        self._quitting = False                 # True once quit cleanup has started
        self._stopping = False                 # True while Stop button is in progress
        self._run_generation = 0               # Incremented on stop/start; stale threads check this

        # Wayback queue state
        self._wayback_queue = []
        self._wayback_queue_lock = threading.Lock()
        self._wayback_last_drain = 0
        self._wayback_queue_item = rumps.MenuItem("", callback=None)

        # Menu items
        # Store reference to browser menu item so we can update its title
        self.browser_menu_item = rumps.MenuItem("Open in Tor Browser", callback=self.open_tor_browser)
        self.local_site_item = rumps.MenuItem("Open Local Site", callback=self.open_local_site)
        self.onionheaven_alert_item = rumps.MenuItem("OnionHeaven Alerts", callback=self.view_onionheaven_alerts)
        self.clearnet_status_item = rumps.MenuItem("", callback=None)

        self.menu = [
            rumps.MenuItem("Starting...", callback=None),
            rumps.separator,
            rumps.MenuItem("Copy Onion Address", callback=self.copy_address),
            self.browser_menu_item,
            self.local_site_item,
            rumps.separator,
            rumps.MenuItem("Start", callback=self.start_service),
            rumps.MenuItem("Stop", callback=self.stop_service),
            rumps.MenuItem("Restart", callback=self.restart_service),
            rumps.separator,
            rumps.MenuItem("View Logs", callback=self.view_logs),
            rumps.MenuItem("View Web Usage Log", callback=self.view_web_log),
            rumps.MenuItem("Settings...", callback=self.open_settings),
            rumps.separator,
            rumps.MenuItem("Backup...", callback=self.backup),
            rumps.MenuItem("Restore...", callback=self.restore),
            rumps.separator,
            rumps.MenuItem("Check for Updates...", callback=self.check_for_updates),
            rumps.MenuItem("About OnionPress", callback=self.show_about),
            rumps.MenuItem("Uninstall...", callback=self.uninstall),
            rumps.separator,
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

        # Ensure Docker is available
        threading.Thread(target=self.ensure_docker_available, daemon=True).start()

        # Listen for system wake to immediately mark Tor as reconnecting
        self.register_wake_notification()

        # Start status checker
        self.start_status_checker()

        # Auto-start on launch
        threading.Thread(target=self.auto_start, daemon=True).start()

    def show_launch_splash(self):
        """Show launch splash with logo synchronously (called from __init__ on main thread)"""
        try:
            # Create window - taller for buttons and time estimate
            window = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                AppKit.NSMakeRect(0, 0, 320, 300),
                AppKit.NSWindowStyleMaskTitled,  # No close button - dismisses automatically when ready
                AppKit.NSBackingStoreBuffered,
                False
            )
            window.setTitle_("OnionPress")
            window.setLevel_(AppKit.NSFloatingWindowLevel)
            window.center()
            window.setReleasedWhenClosed_(False)  # Keep window object alive
            window.setHidesOnDeactivate_(False)  # Stay visible when clicking other windows

            # Create content view
            content_view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 320, 300))

            # Add "Launching..." text
            text_field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(60, 120, 200, 30))
            text_field.setStringValue_("Launching OnionPress...")
            text_field.setBezeled_(False)
            text_field.setDrawsBackground_(False)
            text_field.setEditable_(False)
            text_field.setSelectable_(False)
            text_field.setAlignment_(AppKit.NSTextAlignmentCenter)
            font = AppKit.NSFont.systemFontOfSize_(16)
            text_field.setFont_(font)
            content_view.addSubview_(text_field)

            # Add estimated time text
            time_field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(40, 90, 240, 20))
            time_field.setStringValue_("Estimated time: ~3 minutes")
            time_field.setBezeled_(False)
            time_field.setDrawsBackground_(False)
            time_field.setEditable_(False)
            time_field.setSelectable_(False)
            time_field.setAlignment_(AppKit.NSTextAlignmentCenter)
            time_field.setTextColor_(AppKit.NSColor.secondaryLabelColor())
            small_font = AppKit.NSFont.systemFontOfSize_(12)
            time_field.setFont_(small_font)
            content_view.addSubview_(time_field)

            # Add View Log button
            view_log_button = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(20, 20, 130, 32))
            view_log_button.setTitle_("View Log")
            view_log_button.setBezelStyle_(AppKit.NSBezelStyleRounded)
            view_log_button.setTarget_(self)
            view_log_button.setAction_("openLogFile:")
            content_view.addSubview_(view_log_button)

            # Add Dismiss button
            dismiss_button = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(170, 20, 130, 32))
            dismiss_button.setTitle_("Dismiss")
            dismiss_button.setBezelStyle_(AppKit.NSBezelStyleRounded)
            dismiss_button.setTarget_(self)
            dismiss_button.setAction_("dismissSplashButton:")
            content_view.addSubview_(dismiss_button)

            # Add logo (fast local PNG load)
            icon_path = os.path.join(self.resources_dir, "app-icon.png")
            if os.path.exists(icon_path):
                image_view = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(110, 180, 100, 100))
                image = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
                if image:
                    image_view.setImage_(image)
                    content_view.addSubview_(image_view)

            window.setContentView_(content_view)
            window.makeKeyAndOrderFront_(None)

            self.launch_splash = window
            self.launch_splash_time_field = time_field  # Store reference for updates

            # Log splash creation
            try:
                self.log("DEBUG: Launch splash created and shown")
            except Exception:
                pass

        except Exception as e:
            pass  # Don't crash on splash failure

    def dismiss_launch_splash(self):
        """Dismiss the launch splash window"""
        def dismiss():
            if self.launch_splash:
                try:
                    self.log("Dismissing launch splash")
                    self.launch_splash.orderOut_(None)
                    self.launch_splash.close()
                    self.launch_splash = None
                except Exception as e:
                    self.log(f"Error dismissing launch splash: {e}")

        # Dismiss on main thread
        _main_thread(dismiss)

    def update_splash_status(self, message):
        """Update the launch splash status text from any thread."""
        def _update():
            if self.launch_splash_time_field:
                self.launch_splash_time_field.setStringValue_(message)
        _main_thread(_update)

    def openLogFile_(self, sender):
        """Action handler for View Log button — open in built-in log viewer"""
        try:
            _LogViewerWindow.show_for_file(self.log_file, "OnionPress Log")
        except Exception as e:
            self.log(f"Error opening log file: {e}")

    def dismissSplashButton_(self, sender):
        """Action handler for Dismiss button"""
        self.dismiss_launch_splash()

    def log(self, message):
        """Write log message to onionpress.log file"""
        try:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            log_message = f"[{timestamp}] {message}\n"
            self._onionpress_log.write(log_message)
            self.log_file = self._onionpress_log.current_path()
        except Exception as e:
            print(f"Error writing to log: {e}")

    def _caffeinate_pid_file(self):
        """Path to the file tracking our caffeinate PID."""
        return os.path.join(self.app_support, "caffeinate.pid")

    def _cleanup_stale_caffeinate(self):
        """Kill any orphaned caffeinate process from a previous OnionPress run."""
        pid_file = self._caffeinate_pid_file()
        if not os.path.exists(pid_file):
            return
        try:
            with open(pid_file) as f:
                old_pid = int(f.read().strip())
            # Verify it's actually a caffeinate process before killing
            result = subprocess.run(
                ["ps", "-p", str(old_pid), "-o", "comm="],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5
            )
            if result.returncode == 0 and "caffeinate" in result.stdout:
                os.kill(old_pid, 15)  # SIGTERM
                self.log(f"Cleaned up orphaned caffeinate (PID {old_pid}) from previous run")
            os.remove(pid_file)
        except (ValueError, OSError, subprocess.TimeoutExpired):
            try:
                os.remove(pid_file)
            except OSError:
                pass

    def start_caffeinate(self):
        """Start caffeinate to prevent Mac from sleeping based on config mode"""
        # Check if already running
        if self.caffeinate_process is not None:
            try:
                # Check if process is still alive
                if self.caffeinate_process.poll() is None:
                    return  # Already running
            except Exception:
                pass

        # Clean up any orphaned caffeinate from a previous crash/force-quit
        self._cleanup_stale_caffeinate()

        # Read config — 3 modes: normal, on-battery, never
        mode = self.read_config_value("PREVENT_SLEEP", "normal").lower()
        # Backward compat: yes→on-battery, no→normal
        if mode == "yes":
            mode = "on-battery"
        elif mode == "no":
            mode = "normal"

        if mode == "on-battery":
            caff_args = ["caffeinate", "-s"]
            caff_msg = "staying awake on AC power"
        elif mode == "never":
            caff_args = ["caffeinate", "-i"]
            caff_msg = "never sleeping while OnionPress runs"
        else:
            # "normal" or unknown — no caffeinate
            return

        try:
            self.caffeinate_process = subprocess.Popen(
                caff_args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            # Write PID file so we can clean up if we crash
            try:
                with open(self._caffeinate_pid_file(), 'w') as f:
                    f.write(str(self.caffeinate_process.pid))
            except OSError:
                pass
            self.log(f"Started caffeinate (PID {self.caffeinate_process.pid}) - {caff_msg}")
        except Exception as e:
            self.log(f"Failed to start caffeinate: {e}")

    def stop_caffeinate(self):
        """Stop caffeinate to allow Mac to sleep normally"""
        if self.caffeinate_process is not None:
            try:
                self.caffeinate_process.terminate()
                self.caffeinate_process.wait(timeout=2)
                self.log("Stopped caffeinate - Mac can sleep normally")
            except Exception as e:
                # Force kill if terminate doesn't work
                try:
                    self.caffeinate_process.kill()
                    self.log("Force killed caffeinate process")
                except Exception:
                    pass
            finally:
                self.caffeinate_process = None
                # Remove PID file
                try:
                    os.remove(self._caffeinate_pid_file())
                except OSError:
                    pass

    def start_onion_proxy(self):
        """Start the local .onion proxy server in a background thread."""
        if self.proxy_server is not None:
            return  # already running

        docker_bin = os.path.join(self.bin_dir, "docker")
        docker_env = os.environ.copy()
        docker_env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
        docker_env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")

        # Install the PHP proxy script into the WordPress container
        php_script = os.path.join(self.script_dir, "onion-forward.php")
        if not os.path.exists(php_script):
            # Fallback: check parent resources dir
            php_script = os.path.join(self.parent_resources_dir, "onion-forward.php")
        onion_proxy.install_php_proxy(docker_bin, docker_env, php_script, log_func=self.log)

        def run_proxy():
            try:
                server = onion_proxy.ThreadingHTTPServer(
                    ("127.0.0.1", self.proxy_port),
                    onion_proxy.OnionProxyHandler
                )
                server.docker_bin = docker_bin
                server.docker_env = docker_env
                server.onion_address = self.onion_address
                server.healthcheck_address = self.healthcheck_address
                server.version = self.version
                server.data_dir = self.app_support
                server.log_func = self.log
                server.launcher_script = self.launcher_script
                self.proxy_server = server
                self.log(f"Onion proxy listening on http://127.0.0.1:{self.proxy_port}")
                server.serve_forever()
            except Exception as e:
                self.log(f"Onion proxy failed to start: {e}")
                self.proxy_server = None

        self.proxy_thread = threading.Thread(target=run_proxy, daemon=True)
        self.proxy_thread.start()

    def stop_onion_proxy(self):
        """Stop the local .onion proxy server."""
        if self.proxy_server is not None:
            try:
                self.proxy_server.shutdown()
                self.log("Onion proxy stopped")
            except Exception as e:
                self.log(f"Error stopping onion proxy: {e}")
            finally:
                self.proxy_server = None
                self.proxy_thread = None

    def check_wp_installed(self):
        """Check if WordPress core is installed via wp-cli.

        Returns True (installed), False (not installed), or None (container not ready).
        """
        try:
            docker_bin = os.path.join(self.bin_dir, "docker")
            env = os.environ.copy()
            env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
            env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")
            result = subprocess.run(
                [docker_bin, "exec", "onionpress-wordpress",
                 "wp", "core", "is-installed", "--allow-root"],
                env=env, capture_output=True, timeout=10
            )
            return result.returncode == 0
        except Exception:
            return None

    def show_native_alert(self, title, message, buttons=["OK"], default_button=0, cancel_button=None, style="informational"):
        """Show a native macOS alert dialog using AppKit (no permission prompts, shows custom icon)

        Args:
            title: Dialog title
            message: Dialog message text
            buttons: List of button labels (default: ["OK"])
            default_button: Index of default button (default: 0)
            cancel_button: Index of cancel button or None (default: None)
            style: "informational", "warning", or "critical" (default: "informational")

        Returns:
            Index of clicked button (0-based), or None if dialog dismissed
        """
        def show_dialog():
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_(title)
            alert.setInformativeText_(message)

            # Set alert style
            if style == "warning":
                alert.setAlertStyle_(AppKit.NSAlertStyleWarning)
            elif style == "critical":
                alert.setAlertStyle_(AppKit.NSAlertStyleCritical)
            else:
                alert.setAlertStyle_(AppKit.NSAlertStyleInformational)

            # Add buttons (first button is default)
            for i, button_text in enumerate(buttons):
                btn = alert.addButtonWithTitle_(button_text)
                if i == default_button:
                    btn.setKeyEquivalent_("\r")  # Return key
                elif cancel_button is not None and i == cancel_button:
                    btn.setKeyEquivalent_("\x1b")  # Escape key

            # Set app icon if available
            icon_path = os.path.join(self.resources_dir, "app-icon.png")
            if os.path.exists(icon_path):
                icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
                if icon:
                    alert.setIcon_(icon)

            # Show modal dialog and get response
            response = alert.runModal()

            # Convert response to button index
            # NSAlertFirstButtonReturn = 1000, second = 1001, etc.
            button_index = response - 1000
            return button_index if button_index >= 0 else None

        # Must run on main thread
        # Check if we're already on the main thread to avoid deadlock
        if AppKit.NSThread.isMainThread():
            # Already on main thread, run directly
            return show_dialog()
        else:
            # Not on main thread, dispatch to main thread and wait
            result_container = [None]
            def run_on_main():
                result_container[0] = show_dialog()

            _main_thread(run_on_main)

            # Wait for result (with timeout)
            max_wait = 300  # 5 minutes
            waited = 0
            while result_container[0] is None and waited < max_wait:
                time.sleep(0.1)
                waited += 0.1

            return result_container[0]

    def log_version_info(self):
        """Log version information for all components at startup"""
        self.log("=" * 60)
        self.log(f"OnionPress v{self.version} starting up")
        self.startup_time = time.time()
        self.log("=" * 60)

        # macOS version
        try:
            result = subprocess.run(["sw_vers", "-productVersion"], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
            macos_version = result.stdout.strip() if result.returncode == 0 else "Unknown"
            self.log(f"macOS version: {macos_version}")
        except Exception:
            pass

        # Colima version
        try:
            colima_bin = os.path.join(self.bin_dir, "colima")
            if os.path.exists(colima_bin):
                result = subprocess.run([colima_bin, "version"], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                colima_version = result.stdout.strip().split('\n')[0] if result.returncode == 0 else "Unknown"
                self.log(f"Colima version: {colima_version}")
        except Exception:
            pass

        # Docker version
        try:
            docker_bin = os.path.join(self.bin_dir, "docker")
            if os.path.exists(docker_bin):
                result = subprocess.run([docker_bin, "--version"], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                docker_version = result.stdout.strip() if result.returncode == 0 else "Unknown"
                self.log(f"Docker version: {docker_version}")
        except Exception:
            pass

        # Docker Compose version
        try:
            compose_bin = os.path.join(self.bin_dir, "docker-compose")
            if os.path.exists(compose_bin):
                result = subprocess.run([compose_bin, "version"], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                compose_version = result.stdout.strip().split('\n')[0] if result.returncode == 0 else "Unknown"
                self.log(f"Docker Compose version: {compose_version}")
        except Exception:
            pass

        # Log cached onion address from previous run if available
        try:
            cached_addr_file = os.path.join(self.app_support, "onion_address")
            with open(cached_addr_file) as f:
                cached = f.read().strip()
            if cached and cached.endswith('.onion'):
                self.log(f"Onion address: {cached}")
        except (OSError, IOError):
            pass

        self.log("=" * 60)

    def _web_log_reader_thread(self, process):
        """Read docker logs and write to both raw and filtered rotating logs"""
        try:
            for line in process.stdout:
                self._wp_access_log.write(line)
                if "OnionPress-HealthCheck" not in line:
                    self._wp_visitors_log.write(line)
        except Exception:
            pass

    def start_web_log_capture(self):
        """Start capturing WordPress logs to rotating log files"""
        if self.web_log_process is not None:
            return  # Already running

        try:
            docker_bin = os.path.join(self.bin_dir, "docker")

            # Start docker logs process in background, capture stdout as text
            self.web_log_process = subprocess.Popen(
                [docker_bin, "logs", "-f", "--tail", "100", "onionpress-wordpress"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace',
                env={
                    "DOCKER_HOST": f"unix://{self.colima_home}/default/docker.sock"
                }
            )

            # Start reader thread that splits logs into raw + filtered rotating logs
            self.web_log_thread = threading.Thread(
                target=self._web_log_reader_thread,
                args=(self.web_log_process,),
                daemon=True
            )
            self.web_log_thread.start()

            print(f"Started web log capture to {self._wp_access_log.current_path()}")
        except Exception as e:
            print(f"Error starting web log capture: {e}")
            self.web_log_process = None

    def stop_web_log_capture(self):
        """Stop capturing WordPress logs"""
        if self.web_log_process is not None:
            try:
                self.web_log_process.terminate()
                self.web_log_process.wait(timeout=5)
            except Exception:
                try:
                    self.web_log_process.kill()
                except Exception:
                    pass
            self.web_log_process = None
            # Wait for reader thread to finish
            if hasattr(self, 'web_log_thread') and self.web_log_thread:
                self.web_log_thread.join(timeout=3)
                self.web_log_thread = None
            print("Stopped web log capture")

    def _container_log_reader(self, process, rotating_log):
        """Read docker logs from a process and write to a rotating log."""
        try:
            for line in process.stdout:
                rotating_log.write(line)
        except Exception:
            pass

    def start_container_log_capture(self):
        """Start capturing logs from onionpress-tor, onionheaven, and takeover containers."""
        docker_bin = os.path.join(self.bin_dir, "docker")
        docker_env = {"DOCKER_HOST": f"unix://{self.colima_home}/default/docker.sock"}

        # Fixed containers and their rotating logs
        captures = [
            ("onionpress-tor", self._tor_log),
            ("onionheaven", self._onionheaven_log),
        ]

        # Discover takeover containers
        try:
            result = subprocess.run(
                [docker_bin, "ps", "--format", "{{.Names}}", "--filter", "name=onionheaven-takeover"],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=10, env=docker_env
            )
            if result.returncode == 0:
                for name in result.stdout.strip().split('\n'):
                    name = name.strip()
                    if name and name.startswith("onionheaven-takeover"):
                        logs_dir = os.path.join(self.app_support, "logs")
                        rotating = RotatingLog(logs_dir, f"container-{name}")
                        captures.append((name, rotating))
        except Exception:
            pass

        for container_name, rotating_log in captures:
            if container_name in self._container_log_processes:
                continue  # Already capturing
            try:
                proc = subprocess.Popen(
                    [docker_bin, "logs", "-f", "--tail", "100", container_name],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding='utf-8', errors='replace',
                    env=docker_env
                )
                thread = threading.Thread(
                    target=self._container_log_reader,
                    args=(proc, rotating_log),
                    daemon=True
                )
                thread.start()
                self._container_log_processes[container_name] = (proc, thread)
            except Exception:
                pass

    def start_clearnet_log_capture(self):
        """Periodically poll VM dmesg for CLEARNET iptables log entries."""
        limactl = os.path.join(self.bin_dir, "limactl")
        lima_env = os.environ.copy()
        lima_env["COLIMA_HOME"] = self.colima_home
        lima_env["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")

        while True:
            try:
                result = subprocess.run(
                    [limactl, "shell", "colima", "--", "sh", "-c", "sudo dmesg"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace',
                    timeout=15, env=lima_env,
                )
                if result.returncode == 0:
                    all_lines = [l for l in result.stdout.splitlines() if "CLEARNET" in l]
                    if len(all_lines) > self._clearnet_last_offset:
                        new_lines = all_lines[self._clearnet_last_offset:]
                        self._clearnet_last_offset = len(all_lines)
                        for line in new_lines:
                            self._clearnet_log.write(line + "\n")
            except Exception:
                pass
            time.sleep(60)

    def stop_container_log_capture(self):
        """Stop all container log capture processes."""
        for name, (proc, thread) in list(self._container_log_processes.items()):
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            thread.join(timeout=3)
        self._container_log_processes.clear()

    def ensure_docker_available(self):
        """Ensure bundled Colima is running (no-op during first-time setup as launcher handles it)"""
        try:
            # During first-time setup, the launcher script handles Colima initialization
            # So we just check if it's ready, but don't try to start it ourselves
            colima_bin = os.path.join(self.bin_dir, "colima")
            if not os.path.exists(colima_bin):
                self.log("ERROR: Bundled Colima not found")
                return

            # Check if running
            result = subprocess.run([colima_bin, "status"], capture_output=True, timeout=5)

            if result.returncode == 0:
                # Verify docker accessible
                docker_check = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
                if docker_check.returncode == 0:
                    self.log("Bundled Colima is running")
                    return

            # Don't try to start Colima here - the launcher script handles initialization
            # This avoids conflicts during first-time setup
            self.log("Colima not running yet (launcher may still be initializing)")

        except Exception as e:
            self.log(f"Error checking Colima: {e}")

    def check_port_conflict(self):
        """Check if required ports are already in use by another process."""
        ports = [self.wp_port, self.socks_port, self.proxy_port]
        in_use = []
        for port in ports:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1)
                s.bind(('127.0.0.1', port))
                s.close()
            except OSError:
                in_use.append(port)
        return in_use

    def auto_start(self):
        """Automatically start the service when the app launches"""
        time.sleep(1)  # Brief delay

        # Wait for Colima to be ready (important for first-time setup)
        self.log("Waiting for container runtime to be ready...")
        if self._is_first_run:
            msg = "Starting containers (est. 3 min)..."
        else:
            msg = "Waiting for container runtime..."
        self.update_splash_status(msg)
        if self._is_first_run and setup_window and setup_window._setup_window:
            setup_window._setup_window.set_status(msg)
            setup_window._setup_window.add_log(msg)
        docker_bin = os.path.join(self.bin_dir, "docker")
        colima_initialized = os.path.join(self.colima_home, ".initialized")

        # Wait up to 3 minutes for Colima initialization
        max_wait = 180  # 3 minutes
        waited = 0
        while waited < max_wait:
            # Check if Colima is initialized and docker is responding
            if os.path.exists(colima_initialized):
                try:
                    result = subprocess.run(
                        [docker_bin, "info"],
                        capture_output=True,
                        timeout=5,
                        env=os.environ.copy()
                    )
                    if result.returncode == 0:
                        self.log("Container runtime is ready")
                        self.update_splash_status("Container runtime ready")
                        break
                except Exception:
                    pass

            time.sleep(3)
            waited += 3

        if waited >= max_wait:
            self.log("WARNING: Container runtime not ready after 3 minutes")

        # Check for port conflicts (another user's OnionPress or other process)
        # Only flag a conflict if ports are busy AND our own containers aren't running.
        # Retry a few times since a previous instance may still be releasing ports.
        in_use = self.check_port_conflict()
        if in_use:
            for retry in range(5):
                self.log(f"Ports {in_use} busy, waiting for previous instance to release ({retry+1}/5)...")
                time.sleep(2)
                in_use = self.check_port_conflict()
                if not in_use:
                    break
        if in_use:
            # Check if our containers are already running (normal restart case)
            try:
                env = os.environ.copy()
                env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
                result = subprocess.run(
                    [docker_bin, "ps", "--format", "{{.Names}}"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5, env=env
                )
                our_containers = result.stdout.strip()
            except Exception:
                our_containers = ""

            if "onionpress-" not in our_containers:
                ports_str = ', '.join(str(p) for p in in_use)
                self.log(f"Port conflict detected: ports {ports_str} already in use by another process")
                self._port_conflict = True
                # Must dispatch to main thread — rumps.alert() requires it
                _main_thread(
                    lambda: rumps.alert(
                        title="OnionPress Cannot Start",
                        message=f"Port(s) {ports_str} already in use.\n\n"
                                "Another process is using these ports.\n\n"
                                "Close the conflicting application and try again."
                    )
                )
                self.menu["Starting..."].title = "Status: Port conflict"
                return

        self._ports_checked = True

        # Check if UPDATE_ON_LAUNCH is enabled
        config_file = os.path.join(self.app_support, "config")
        update_on_launch = False
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        if line.startswith('UPDATE_ON_LAUNCH='):
                            value = line.split('=', 1)[1].strip().lower()
                            update_on_launch = (value == 'yes')
                            break
            except Exception:
                pass

        if update_on_launch:
            self.log("UPDATE_ON_LAUNCH enabled - checking for Docker image updates...")
            self.update_docker_images(show_notifications=False)

        # First run: show welcome screen, wait for user to click "Set Up",
        # then start_service runs in the callback
        if self._is_first_run and setup_window:
            self.dismiss_launch_splash()
            sw = setup_window.get_setup_window()
            sw.set_on_setup(lambda: self._first_run_after_welcome())
            setup_window.show_welcome_screen()
            return  # Don't call start_service — the callback will

        self.start_service(None)


    LAUNCHAGENT_LABEL = "com.onionpress.launcher"
    LAUNCHAGENT_PATH = os.path.expanduser(
        f"~/Library/LaunchAgents/{LAUNCHAGENT_LABEL}.plist")

    def _is_login_item_installed(self):
        """Check if LaunchAgent plist exists"""
        return os.path.exists(self.LAUNCHAGENT_PATH)

    def add_login_item(self):
        """Install LaunchAgent plist for auto-start on login"""
        try:
            plist = {
                "Label": self.LAUNCHAGENT_LABEL,
                "ProgramArguments": ["open", "-a", os.path.dirname(self.contents_dir)],
                "RunAtLoad": True,
                "LimitLoadToSessionType": "Aqua",
            }
            os.makedirs(os.path.dirname(self.LAUNCHAGENT_PATH), exist_ok=True)
            with open(self.LAUNCHAGENT_PATH, "wb") as f:
                plistlib.dump(plist, f)
            self.log("LaunchAgent installed for login auto-start")
            return True
        except Exception as e:
            self.log(f"Error installing LaunchAgent: {e}")
            return False

    def remove_login_item(self):
        """Remove LaunchAgent plist"""
        try:
            if os.path.exists(self.LAUNCHAGENT_PATH):
                # Unload first (ignore errors if not loaded)
                subprocess.run(
                    ["launchctl", "bootout", f"gui/{os.getuid()}",
                     self.LAUNCHAGENT_PATH],
                    capture_output=True, timeout=10)
                os.remove(self.LAUNCHAGENT_PATH)
            self.log("LaunchAgent removed")
            return True
        except Exception as e:
            self.log(f"Error removing LaunchAgent: {e}")
            return False


    def run_command(self, command):
        """Run a command and return output"""
        try:
            result = subprocess.run(
                [self.launcher_script, command],
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=60
            )
            return result.stdout.strip()
        except Exception as e:
            print(f"Error running command {command}: {e}")
            return None

    def check_wordpress_health(self, log_result=True):
        """Check if WordPress is actually responding to requests"""
        try:
            if log_result:
                self.log(f"Checking local access: http://localhost:{self.wp_port}")
            # Use curl instead of urllib to avoid "local network" permission prompt
            result = subprocess.run(
                ["curl", "-s", "--max-time", "3", "-H", "User-Agent: OnionPress-HealthCheck", f"http://localhost:{self.wp_port}"],
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=5
            )
            if result.returncode == 0:
                content = result.stdout
                # Check for database errors or WordPress not ready
                if 'Error establishing a database connection' in content:
                    if log_result:
                        self.log("✗ Local access: Database connection error")
                    return False
                if 'Database connection error' in content:
                    if log_result:
                        self.log("✗ Local access: Database connection error")
                    return False
                # If we get here and got a response, WordPress is responding
                # Either it's the install page or actual WordPress content
                if log_result:
                    self.log("✓ Local access: WordPress responding")
                return True
            else:
                if log_result:
                    self.log(f"✗ Local access: Connection failed (curl exit code {result.returncode})")
                return False
        except Exception as e:
            if log_result:
                self.log(f"✗ Local access: Connection failed ({str(e)})")
            return False

    def check_tor_reachability(self, log_result=True):
        """Check if the .onion service is properly configured and published"""
        self._tor_internally_ready = False
        if not self.onion_address or self.onion_address in ["Starting...", "Not running", "Generating address..."]:
            return False

        try:
            if log_result:
                self.log(f"Checking Tor onion service status for: {self.onion_address}")

            # Check 1: Verify hostname file exists and matches
            hostname = self._health_checker.check_tor_hostname(self.onion_address)
            if not hostname:
                if log_result:
                    self.log("✗ Onion service hostname file not found")
                return False
            if hostname != self.onion_address:
                if log_result:
                    self.log(f"✗ Hostname mismatch: {hostname} != {self.onion_address}")
                return False

            # Check 2: Verify Tor has bootstrapped (via control port)
            bootstrapped, pct = self._health_checker.check_tor_bootstrap()
            if not bootstrapped:
                if log_result:
                    self.log(f"✗ onionpress-tor not fully bootstrapped yet ({pct}%)")
                return False

            # Check 4: Internal connectivity (Tor → WordPress over Docker network)
            if not self._health_checker.check_internal_connectivity():
                if log_result:
                    self.log("✗ WordPress not reachable from Tor container")
                return False

            self._tor_internally_ready = True

            # Check 5: External reachability via onionheaven's independent Tor
            reachable, http_code = self._health_checker.check_external_reachability(self.onion_address)
            if not reachable:
                if log_result:
                    if http_code == "302":
                        self.log("✗ Onion service returning 302 (OnionHeaven takeover active)")
                    elif http_code.startswith("000"):
                        # Decode curl exit code for debugging
                        _curl_reasons = {
                            "1": "protocol error (descriptor not yet available)",
                            "6": "DNS resolution failed",
                            "7": "connection refused",
                            "28": "timeout (30s)",
                            "35": "TLS handshake failed",
                            "52": "empty reply",
                            "56": "connection reset",
                            "97": "SOCKS handshake failed (descriptor not yet available)",
                        }
                        rc = http_code.split("rc=")[1] if "rc=" in http_code else ""
                        reason = _curl_reasons.get(rc, f"curl rc={rc}" if rc else "unknown")
                        self.log(f"✗ Our onion service not yet reachable through Tor network ({reason})")
                    else:
                        self.log(f"✗ Onion service returned HTTP {http_code}")
                return False

            if log_result:
                self.log(f"✓ Onion service verified: {self.onion_address}")
            return True

        except Exception as e:
            if log_result:
                self.log(f"✗ Tor status check failed: {str(e)}")
            return False

    def _remove_pid_file(self):
        """Remove PID file on exit"""
        try:
            if os.path.exists(self.pid_file):
                with open(self.pid_file) as f:
                    pid = int(f.read().strip())
                if pid == os.getpid():
                    os.remove(self.pid_file)
        except Exception:
            pass

    def _signal_handler(self, signum, frame):
        """Handle SIGTERM/SIGINT — trigger graceful quit (same as Quit button)"""
        self.log(f"Received signal {signum}, initiating graceful shutdown...")
        _main_thread(lambda: self.quit_app(None))

    def _manual_analytics_upload(self):
        """Run analytics upload immediately (called from SIGUSR1 handler)."""
        try:
            enabled = self.read_config_value(
                "SHARE_ANALYTICS_WITH_ONIONHOME", "no"
            ).lower()
            if enabled != "yes":
                self.log("Analytics upload skipped: SHARE_ANALYTICS_WITH_ONIONHOME is not 'yes'")
                return
            analytics_sharing._do_upload_cycle(self, include_active=True)
            self.log("Analytics upload complete")
        except Exception as e:
            self.log(f"Analytics upload error: {e}")

    def handle_reopen(self):
        """Handle reopen signal from launcher (user double-clicked app while running)"""
        self.log("Reopen signal received")
        if self.is_running and self.is_ready:
            self.log("Service is ready — opening browser")
            self.open_tor_browser(None)
        elif not self.is_running:
            self.log("Service not running — starting service")
            self.start_service(None)

    def check_internet_connectivity(self):
        """Check if host has internet connectivity."""
        return self._health_checker.check_internet_connectivity()

    def _parse_bootstrap_percentage(self):
        """Parse Tor bootstrap percentage from container logs."""
        try:
            _bootstrapped, pct = self._health_checker.check_tor_bootstrap()
            return pct
        except Exception:
            return 0

    @property
    def display_state(self):
        """Compute the display state from current variables.
        Returns one of: 'stopped', 'available', 'offline', 'stuck', 'starting'."""
        if not self.is_running:
            return "stopped"
        if self.is_ready:
            return "available"
        if not self._has_internet:
            return "offline"
        # Check for stuck: yellow 5min+ (gives auto-restart time to work)
        if self._yellow_since and (time.time() - self._yellow_since) > 300:
            return "stuck"
        return "starting"

    def _read_config_value(self, key, default=""):
        """Read a value from ~/.onionpress/config."""
        return op_config.read_value(self.config_file, key, default)

    def check_status(self):
        """Check if containers are running and get onion address"""
        if self._port_conflict:
            return
        with self._checking_lock:
            if self.checking:
                return
            self.checking = True

        try:
            # Check for reopen signal from launcher
            reopen_file = os.path.join(self.app_support, ".reopen")
            if os.path.exists(reopen_file):
                try:
                    os.remove(reopen_file)
                except OSError:
                    pass
                self.handle_reopen()

            # Check for upload-analytics trigger (host file from CLI,
            # or Docker volume file from WordPress "Share Now" button)
            upload_trigger = os.path.join(self.app_support, ".upload-analytics")
            trigger_found = os.path.exists(upload_trigger)
            if trigger_found:
                try:
                    os.remove(upload_trigger)
                except OSError:
                    pass
            else:
                # Check inside Docker volume — test -f && rm is atomic enough
                r = self._docker.exec("onionpress-wordpress",
                    ["sh", "-c", "test -f /var/lib/onionpress/.upload-analytics && rm /var/lib/onionpress/.upload-analytics"],
                    timeout=5, quiet=True)
                if r.ok:
                    trigger_found = True
            if trigger_found:
                self.log("Upload-analytics trigger detected")
                threading.Thread(target=self._manual_analytics_upload, daemon=True).start()

            # Check if containers are running
            status_json = self.run_command("status")

            if status_json and status_json != "[]":
                try:
                    status = json.loads(status_json)
                    self.is_running = len(status) > 0 and all(
                        s.get("State", "").lower() == "running" for s in status
                    )
                except Exception:
                    self.is_running = False
            else:
                self.is_running = False

            # Get onion address if running
            if self.is_running:
                addr = self.run_command("address")
                if addr and addr != "Generating...":
                    self.onion_address = addr.strip()
                    # Cache address locally for instant availability on next launch
                    try:
                        with open(os.path.join(self.app_support, "onion_address"), 'w') as f:
                            f.write(self.onion_address)
                    except OSError:
                        pass
                else:
                    self.onion_address = "Generating address..."

                # Check internet connectivity
                had_internet = self._has_internet
                self._has_internet = self.check_internet_connectivity()
                if not self._has_internet and had_internet:
                    self.log("Internet connectivity lost")
                elif self._has_internet and not had_internet:
                    self.log("Internet connectivity restored")
                    # Show yellow immediately while Tor check runs
                    self.update_menu()

                if not self._has_internet:
                    # No internet — skip expensive WordPress/Tor checks
                    if self.is_ready:
                        self.log("Going offline — no internet connection")
                    self.is_ready = False
                    # Track yellow/starting state
                    if self._yellow_since is None:
                        self._yellow_since = time.time()
                else:
                    # Internet available — do full health checks
                    # Determine if we should do detailed checks and logging
                    current_status = (self.is_running, self.onion_address)
                    should_log = (current_status != self.last_status_logged) or not self.is_ready

                    # Check if WordPress is ready and Tor is reachable.
                    # Once WordPress responds, skip rechecking it — it stays up
                    # reliably inside Docker. Only Tor needs ongoing monitoring.
                    if not self._wordpress_confirmed:
                        wordpress_ready = self.check_wordpress_health(log_result=should_log)
                        if wordpress_ready:
                            self._wordpress_confirmed = True
                    else:
                        wordpress_ready = True
                    tor_reachable = self.check_tor_reachability(log_result=should_log)

                    previous_ready = self.is_ready
                    ready_now = wordpress_ready and tor_reachable

                    if ready_now and not previous_ready:
                        self.is_ready = True
                        self._was_ready = True
                        self._onionheaven_reclaim_succeeded = False
                        self._onionheaven_reclaim_in_flight = False
                        self._onionheaven_reclaim_last_attempt = 0
                        self._bootstrap_stall_count = 0
                        self._yellow_since = None
                        elapsed = int(time.time() - self.startup_time)
                        self.log(f"✓ System fully operational (launched in {elapsed}s)")
                        self.last_status_logged = current_status

                        # Write setup_complete marker (first-run detection)
                        try:
                            setup_marker = os.path.join(self.app_support, ".setup_complete")
                            if not os.path.exists(setup_marker):
                                with open(setup_marker, 'w') as f:
                                    f.write("1")
                        except OSError:
                            pass

                        # Re-read Cloudflare Tunnel config (may have changed since launch)
                        self.cloudflare_tunnel_enabled = bool(self._read_config_value("CLOUDFLARE_TUNNEL_TOKEN"))

                        # Dismiss setup dialog if it's showing
                        self.dismiss_setup_dialog()

                        # Advance setup window: reachability + heartbeat + browser, then close
                        if setup_window and setup_window._setup_window is not None:
                            sw = setup_window._setup_window
                            if sw.window:
                                sw.set_step(5)
                                sw.add_log("Onion service reachable through Tor")
                                sw.complete_step(5)
                                sw.set_status("Starting heartbeat...")
                                sw.add_log("Starting heartbeat...")
                                sw.complete_step(6)
                                sw.set_status("Opening tor-enabled browser...")
                                sw.add_log("Opening tor-enabled browser...")

                        # Auto-open browser on first ready (runs in background
                        # so the monitoring loop can continue and start the proxy)
                        if not self.auto_opened_browser:
                            self.auto_opened_browser = True
                            self.log(f"DEBUG: Spawning auto_open_browser thread, onion_address={self.onion_address!r}")
                            threading.Thread(target=self.auto_open_browser, daemon=True).start()

                        # Complete browser step and show completion in setup window
                        if setup_window and setup_window._setup_window is not None:
                            sw = setup_window._setup_window
                            if sw.window:
                                sw.complete_step(7)
                                sw.show_completion(self.onion_address)
                                # Auto-close after 10 seconds (give user time to read address)
                                def _close_setup():
                                    time.sleep(10)
                                    setup_window.close_setup_progress()
                                threading.Thread(target=_close_setup, daemon=True).start()

                        # Force menu update (changes icon to purple)
                        self.update_menu()

                        # Dismiss splash AFTER icon turns purple
                        self.dismiss_launch_splash()
                    elif ready_now:
                        # Already was ready, keep it ready
                        self.is_ready = True
                        self._bootstrap_stall_count = 0
                        self._yellow_since = None
                        self.last_status_logged = current_status
                    elif previous_ready and not ready_now:
                        # Was ready, now failing — go to reconnecting state
                        # (but skip if user intentionally stopped or is quitting)
                        if self._stopping or self._quitting:
                            self.is_ready = False
                        else:
                            self.is_ready = False
                            self._yellow_since = time.time()
                            self._bootstrap_stall_count = 0
                            self._onionheaven_heartbeat_succeeded = False
                            self.startup_time = time.time()  # Reset so "launched in Xs" shows recovery time
                            self.log("Service became unreachable — reconnecting")
                    else:
                        # Not ready yet — track bootstrap progress for stuck detection
                        pct = self._parse_bootstrap_percentage()
                        if pct > self._last_bootstrap_pct:
                            self._last_bootstrap_pct = pct
                            self._bootstrap_stall_count = 0
                            if pct >= 100:
                                self.update_splash_status("Waiting for onion service to become reachable...")
                            else:
                                self.update_splash_status(f"Tor bootstrap: {pct}%")
                        else:
                            self._bootstrap_stall_count += 1
                        if self._yellow_since is None:
                            self._yellow_since = time.time()

                        # Auto-restart tor if stuck for 2+ minutes AND
                        # the container shows signs of actual trouble (broken
                        # guards, circuit failures). If Arti is healthy but
                        # just waiting for descriptor propagation, don't restart
                        # — that would reset progress.
                        # Uses cooldown (5 min) so we can retry if the spiral recurs.
                        # Watchdog inside tor container handles recovery via control port

                # Start web log capture if not already running
                if self.web_log_process is None:
                    threading.Thread(target=self.start_web_log_capture, daemon=True).start()

                # Start container log capture (tor, onionheaven, takeover workers)
                if not self._container_log_processes:
                    threading.Thread(target=self.start_container_log_capture, daemon=True).start()
                if not getattr(self, '_clearnet_capture_started', False):
                    self._clearnet_capture_started = True
                    threading.Thread(target=self.start_clearnet_log_capture, daemon=True).start()

                # Start caffeinate if not already running (prevents sleep while service runs)
                if self.caffeinate_process is None or self.caffeinate_process.poll() is not None:
                    self.start_caffeinate()

                # Start onion proxy if not already running (wait for port check first)
                if self.proxy_server is None and self._ports_checked:
                    self.start_onion_proxy()
                elif self.proxy_server:
                    # Update onion address and readiness on existing proxy
                    self.proxy_server.onion_address = self.onion_address
                    self.proxy_server.healthcheck_address = self.healthcheck_address
                    self.proxy_server.tor_ready = self.is_ready

                # Read healthcheck address as soon as tor is internally ready
                # (needed for OnionHeaven heartbeat, which starts before purple)
                if self.healthcheck_address is None and self._tor_internally_ready:
                    self.read_healthcheck_address()

                # Poll for OnionHeaven messages from healthcheck service
                if self.is_ready:
                    self.poll_onionheaven_messages()

                # Poll and drain Wayback queue
                self.poll_wayback_queue()
                if self.is_ready:
                    self.drain_wayback_queue()

                # Write status, poll for config updates & action requests from WordPress settings page
                self.write_status_to_volume()
                self.poll_config_updates()
                self.poll_requested_actions()

                # OnionHeaven: detect onionheaven mode (one-shot)
                if self.is_ready and not self._onionheaven_checked:
                    self._onionheaven_checked = True
                    if onionheaven.is_onionheaven_instance(self.onion_address):
                        self.is_onionheaven = True
                        self.log("OnionHeaven mode activated (heartbeat monitor runs in onionheaven container)")
                        # One-shot: auto-set PREVENT_SLEEP=never on first OnionHeaven detection
                        # Uses a marker file so we never override the user's later choice
                        sleep_marker = os.path.join(self.app_support, ".onionheaven_sleep_set")
                        if not os.path.exists(sleep_marker):
                            self.write_config_value("PREVENT_SLEEP", "never")
                            try:
                                with open(sleep_marker, 'w') as f:
                                    f.write("1")
                            except OSError:
                                pass
                            self.log("Auto-set PREVENT_SLEEP=never for OnionHeaven machine (first detection)")
                        # Restart caffeinate with the (now-updated) config
                        self.stop_caffeinate()
                        self.start_caffeinate()
                        self.update_menu()

                # OnionHeaven: start heartbeat as soon as Tor is bootstrapped.
                # Don't wait for internal readiness or purple — the heartbeat IS
                # the reclaim mechanism. If OnionHeaven has taken over our address,
                # the heartbeat's /online will release it. Without this, fresh
                # installs get stuck: 302 takeover → internal check fails →
                # _tor_internally_ready never set → heartbeat never fires.
                # Use bootstrap percentage instead (100% = Tor can make circuits).
                tor_bootstrapped = self._last_bootstrap_pct >= 100
                if (tor_bootstrapped and self.onion_address
                        and self.onion_address not in ["Starting...", "Not running", "Generating address..."]
                        and not self.is_onionheaven
                        and not self._onionheaven_registration_succeeded
                        and not self._onionheaven_registration_in_flight):
                    self._onionheaven_registration_in_flight = True
                    onionheaven.start_registration_thread(self)

                    # Start analytics sharing (opt-in, checks config each cycle)
                    if not getattr(self, '_analytics_sharing_started', False):
                        self._analytics_sharing_started = True
                        analytics_sharing.start_analytics_sharing(self)

                # Check if WordPress setup is needed (first-run guard)
                if self._wp_installed is not True and self.proxy_server:
                    wp_installed = self.check_wp_installed()
                    if wp_installed:
                        was_waiting = (self._wp_installed is False)
                        self._wp_installed = True
                        if was_waiting:
                            # Setup just completed — start Tor
                            self.log("Setup complete — starting Tor")
                            threading.Thread(
                                target=lambda: subprocess.run([self.launcher_script, "start-tor"]),
                                daemon=True
                            ).start()
                    elif wp_installed is False and not self._setup_page_opened:
                        # WordPress container responded but WP not installed.
                        # On first run, wp core install handles this — don't open browser.
                        if self._is_first_run:
                            pass
                        else:
                            # Require 5 consecutive "not installed" results before opening
                            # the setup page — the DB may still be warming up.
                            self._wp_not_installed_count += 1
                            if self._wp_not_installed_count >= 5:
                                self._wp_installed = False
                                self._setup_page_opened = True
                                self.log("WordPress not installed — opening setup page")
                                # Dismiss dialogs before opening browser
                                self.dismiss_setup_dialog()
                                self.dismiss_launch_splash()
                                subprocess.run(["open", f"http://localhost:{onion_proxy.PROXY_PORT}/setup"])
                    else:
                        # Reset counter on None (container not ready) or True
                        self._wp_not_installed_count = 0
            else:
                # Log when stopping
                if self.is_running or self.is_ready:
                    self.log("Service stopped")
                    self.last_status_logged = None

                    # Only dismiss setup dialog when actually stopping (not during startup)
                    self.dismiss_setup_dialog()

                # Keep cached address visible even when stopped — it's still valid
                if not self.onion_address or self.onion_address in ["Starting...", "Generating address..."]:
                    self.onion_address = "Not running"
                self.is_ready = False
                self.auto_opened_browser = False  # Reset for next start
                self._wp_installed = None  # Reset for next start
                self._wp_not_installed_count = 0
                self._setup_page_opened = False
                self._was_ready = False
                self._last_bootstrap_pct = 0
                self._bootstrap_stall_count = 0
                self._yellow_since = None
                self.healthcheck_address = None
                self.onionheaven_messages = []
                self._onionheaven_alert_shown = False
                self._onionheaven_checked = False
                self._onionheaven_registration_succeeded = False
                self._onionheaven_heartbeat_succeeded = False
                self._onionheaven_registration_in_flight = False
                self._onionheaven_reclaim_succeeded = False
                self._onionheaven_reclaim_in_flight = False
                self._onionheaven_reclaim_last_attempt = 0
                self._tor_internally_ready = False

                # Stop web log capture if running
                if self.web_log_process is not None:
                    self.stop_web_log_capture()
                    self.stop_container_log_capture()

                # Stop caffeinate to allow Mac to sleep
                self.stop_caffeinate()

            # Update menu
            self.update_menu()

        except Exception as e:
            self.log(f"ERROR in check_status: {e}")
            import traceback
            self.log(traceback.format_exc())
        finally:
            self.checking = False

    def update_menu(self):
        """Update menu items based on current state - thread-safe"""
        # Dispatch UI updates to main thread to avoid AppKit threading violations
        def do_update():
            state = self.display_state

            # OnionHeaven alert indicator: show "!" next to icon when messages exist
            if self.onionheaven_messages:
                self.title = "!"
                count = len(self.onionheaven_messages)
                self.onionheaven_alert_item.title = f"OnionHeaven Alerts ({count})"
                self.onionheaven_alert_item.set_callback(self.view_onionheaven_alerts)
                if self.onionheaven_alert_item.title not in self.menu:
                    self.menu.insert_after("Copy Onion Address", self.onionheaven_alert_item)
            else:
                self.title = ""
                if "OnionHeaven Alerts" in self.menu:
                    del self.menu["OnionHeaven Alerts"]
                for key in list(self.menu.keys()):
                    if isinstance(key, str) and key.startswith("OnionHeaven Alerts ("):
                        del self.menu[key]

            # Show/hide clearnet status based on tunnel config and state
            show_clearnet = (state == "available" and self.cloudflare_tunnel_enabled)
            if show_clearnet:
                self.clearnet_status_item.title = "Clearnet: Active (via Cloudflare)"
                self.clearnet_status_item.set_callback(None)
                if self.clearnet_status_item.title not in self.menu:
                    self.menu.insert_after("Copy Onion Address", self.clearnet_status_item)
            else:
                if "Clearnet: Active (via Cloudflare)" in self.menu:
                    del self.menu["Clearnet: Active (via Cloudflare)"]

            if self._quitting:
                return  # Don't update icon/menu during shutdown

            # Show/hide Wayback queue status
            with self._wayback_queue_lock:
                wq_count = len(self._wayback_queue)
            if wq_count > 0:
                self._wayback_queue_item.title = f"Pending Wayback Saves ({wq_count})"
                if self._wayback_queue_item.title not in self.menu:
                    self.menu.insert_after("Copy Onion Address", self._wayback_queue_item)
            else:
                # Remove any existing queue item (title may have changed)
                for key in list(self.menu.keys()):
                    if isinstance(key, str) and key.startswith("Pending Wayback Saves"):
                        del self.menu[key]

            if state == "available":
                self.icon = self.icon_running
                if self.is_onionheaven:
                    self.menu["Starting..."].title = f"OnionHeaven: {self.onion_address}"
                else:
                    self.menu["Starting..."].title = f"Address: {self.onion_address}"
                self.menu["Start"].set_callback(None)
                self.menu["Stop"].set_callback(self.stop_service)
                self.menu["Restart"].set_callback(self.restart_service)
                self.menu["Backup..."].set_callback(self.backup)
                self.menu["Restore..."].set_callback(self.restore)
                self.update_browser_menu_title()
                # Purple: browser opens .onion, local site available as secondary
                self.browser_menu_item.set_callback(self.open_tor_browser)
                self.local_site_item.title = f"Open Local Site ({self.local_url})"
                self.local_site_item.set_callback(self.open_local_site)
            elif state in ("starting", "offline", "stuck"):
                if state == "starting":
                    self.icon = self.icon_starting
                    pct = self._last_bootstrap_pct
                    if pct > 0:
                        self.menu["Starting..."].title = f"Status: Connecting to Tor ({pct}%)..."
                    else:
                        self.menu["Starting..."].title = "Status: Starting up, please wait..."
                elif state == "offline":
                    self.icon = self.icon_stopped
                    self.menu["Starting..."].title = "Status: Offline — no internet connection"
                else:  # stuck
                    self.icon = self.icon_starting
                    self.menu["Starting..."].title = "Status: Slow to connect — try Restart"
                self.menu["Start"].set_callback(None)
                self.menu["Stop"].set_callback(self.stop_service)
                self.menu["Restart"].set_callback(self.restart_service)
                self.menu["Backup..."].set_callback(self.backup)
                self.menu["Restore..."].set_callback(self.restore)
                # Gray/Yellow: browser opens local site since .onion isn't reachable
                self.browser_menu_item.title = f"Open Local Site ({self.local_url})"
                self.browser_menu_item.set_callback(self.open_local_site)
                self.local_site_item.title = ""
                self.local_site_item.set_callback(None)
            else:
                # Stopped
                self.icon = self.icon_stopped
                if self.onion_address and self.onion_address.endswith('.onion'):
                    self.menu["Starting..."].title = f"Stopped — {self.onion_address}"
                else:
                    self.menu["Starting..."].title = "Status: Stopped"
                self.menu["Start"].set_callback(self.start_service)
                self.menu["Stop"].set_callback(None)
                self.menu["Restart"].set_callback(None)
                self.menu["Backup..."].set_callback(None)
                self.menu["Restore..."].set_callback(self.restore)
                # Stopped: disable browser items
                self.browser_menu_item.set_callback(None)
                self.local_site_item.title = ""
                self.local_site_item.set_callback(None)

        # Execute on main thread
        _main_thread(do_update)

    def read_healthcheck_address(self):
        """Read the healthcheck .onion address from the tor container."""
        try:
            # First try the cached file written by the launcher
            hc_file = os.path.join(self.app_support, "healthcheck-address")
            if os.path.exists(hc_file):
                with open(hc_file) as f:
                    addr = f.read().strip()
                if addr and addr.endswith('.onion'):
                    self.healthcheck_address = addr
                    self.log(f"Healthcheck address: {addr}")
                    return

            # Fall back to reading from container
            docker_bin = os.path.join(self.bin_dir, "docker")
            env = os.environ.copy()
            env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
            env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")
            result = subprocess.run(
                [docker_bin, "exec", "onionpress-tor",
                 "cat", "/var/lib/tor/hidden_service/healthcheck/hostname"],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10, env=env
            )
            if result.returncode == 0:
                addr = result.stdout.strip()
                if addr and addr.endswith('.onion'):
                    self.healthcheck_address = addr
                    # Cache for next time
                    try:
                        with open(hc_file, 'w') as f:
                            f.write(addr)
                    except OSError:
                        pass
                    self.log(f"Healthcheck address: {addr}")
        except Exception as e:
            self.log(f"Failed to read healthcheck address: {e}")

    def poll_onionheaven_messages(self):
        """Poll for messages from OnionHeaven via the healthcheck service."""
        try:
            docker_bin = os.path.join(self.bin_dir, "docker")
            env = os.environ.copy()
            env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
            env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")

            # List message files in the container
            result = subprocess.run(
                [docker_bin, "exec", "onionpress-tor",
                 "ls", "/var/lib/tor/healthcheck-messages/"],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10, env=env
            )
            if result.returncode != 0 or not result.stdout.strip():
                if self.onionheaven_messages:
                    self.onionheaven_messages = []
                    self._onionheaven_alert_shown = False
                return

            files = result.stdout.strip().split('\n')
            json_files = [f for f in files if f.endswith('.json')]
            if not json_files:
                if self.onionheaven_messages:
                    self.onionheaven_messages = []
                    self._onionheaven_alert_shown = False
                return

            # Read all message files
            messages = []
            for fname in json_files:
                try:
                    r = subprocess.run(
                        [docker_bin, "exec", "onionpress-tor",
                         "cat", f"/var/lib/tor/healthcheck-messages/{fname}"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5, env=env
                    )
                    if r.returncode == 0 and r.stdout.strip():
                        msg = json.loads(r.stdout.strip())
                        messages.append(msg)
                except Exception:
                    continue

            if messages and messages != self.onionheaven_messages:
                self.onionheaven_messages = messages
                if not self._onionheaven_alert_shown:
                    self._onionheaven_alert_shown = True
                    self.log(f"Received {len(messages)} message(s) from OnionHeaven")
                    latest = messages[-1]
                    msg_type = latest.get("type", "unknown")
                    msg_text = latest.get("message", "New message from OnionHeaven")
                    self.log(f"OnionHeaven alert: {msg_type} - {msg_text}")
        except Exception:
            # Don't spam logs — OnionHeaven polling failures are expected when container is starting
            pass

    def view_onionheaven_alerts(self, _):
        """Show OnionHeaven alert messages and offer to dismiss them."""
        if not self.onionheaven_messages:
            rumps.alert("No OnionHeaven alerts.")
            return

        # Build summary of all messages
        lines = []
        for msg in self.onionheaven_messages:
            msg_type = msg.get("type", "unknown").replace("_", " ").title()
            msg_text = msg.get("message", "")
            lines.append(f"[{msg_type}] {msg_text}")
        summary = "\n".join(lines)

        response = rumps.alert(
            title=f"OnionHeaven Alerts ({len(self.onionheaven_messages)})",
            message=summary,
            ok="Dismiss All",
            cancel="Close"
        )

        if response == 1:  # "Dismiss All" clicked
            self.log("Dismissing OnionHeaven alerts")
            self.onionheaven_messages = []
            self._onionheaven_alert_shown = False
            # Delete message files from container
            try:
                docker_bin = os.path.join(self.bin_dir, "docker")
                env = os.environ.copy()
                env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
                env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")
                subprocess.run(
                    [docker_bin, "exec", "onionpress-tor",
                     "sh", "-c", "rm -f /var/lib/tor/healthcheck-messages/*.json"],
                    capture_output=True, timeout=10, env=env
                )
            except Exception:
                pass
            self.update_menu()

    def register_wake_notification(self):
        """Register for macOS wake notification to immediately update icon"""
        ws = AppKit.NSWorkspace.sharedWorkspace()
        nc = ws.notificationCenter()
        def _safe_callback(handler_name, handler):
            """Wrap notification callbacks to guard against early/crash calls."""
            def wrapper(notification):
                try:
                    if not getattr(self, '_quitting', False):
                        handler()
                except Exception as e:
                    try:
                        self.log(f"WARNING: {handler_name} callback error: {e}")
                    except Exception:
                        pass
            return wrapper

        nc.addObserverForName_object_queue_usingBlock_(
            AppKit.NSWorkspaceWillSleepNotification,
            None,
            AppKit.NSOperationQueue.mainQueue(),
            _safe_callback("sleep", self.handle_sleep))
        nc.addObserverForName_object_queue_usingBlock_(
            AppKit.NSWorkspaceDidWakeNotification,
            None,
            AppKit.NSOperationQueue.mainQueue(),
            _safe_callback("wake", self.handle_wake))
        # Register for app termination (catches osascript quit / Apple Event quit)
        AppKit.NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            AppKit.NSApplicationWillTerminateNotification,
            None,
            None,  # Deliver on posting thread (main thread)
            _safe_callback("terminate", self._handle_terminate))
        self.log("Registered for system sleep/wake/terminate notifications")

    def _signal_watchdog(self, container, sig):
        """Send a Unix signal to the tor-watchdog process inside a container."""
        # [t]or-watchdog bracket trick prevents pgrep from matching the sh -c command itself
        result = self._docker.exec(
            container,
            ["sh", "-c", f'kill -{sig} $(pgrep -f "[t]or-watchdog") 2>/dev/null'],
            timeout=10,
        )
        return result.ok

    def handle_sleep(self):
        """Handle system sleep — DEL_ONION via watchdog, notify hub, release caffeinate.

        Sends USR1 to the watchdog in each Tor container, which DEL_ONIONs all
        services. Tor stays running with guards/circuits. Hub takes over cleanly
        (no competing descriptors). On wake, USR2 re-ADDs the services.
        """
        self.log("System going to sleep")
        self._sleeping = True
        self._onionheaven_heartbeat_succeeded = False
        if not self.is_onionheaven:
            # Signal all Tor containers to DEL_ONION their services
            for container in ["onionpress-tor", "onionheaven"]:
                if self._signal_watchdog(container, "USR1"):
                    self.log(f"Sent USR1 (sleep) to {container} watchdog")
                else:
                    self.log(f"Failed to send USR1 to {container} watchdog")
            # Notify OnionHeaven hub so it can take over quickly
            if self.is_ready and self._onionheaven_registration_succeeded:
                try:
                    onionheaven.notify_onionheaven_offline(self)
                except Exception:
                    pass
            self.stop_caffeinate()

    def _handle_terminate(self):
        """Handle app termination (osascript quit, Apple Event, etc.).
        Runs synchronously before the app exits to ensure proper cleanup."""
        if self._quitting:
            return  # Already cleaning up via Quit button
        self._quitting = True
        self.log("="*60)
        self.log("APP TERMINATING (Apple Event / osascript quit)")
        self.log("="*60)

        # Notify OnionHeaven before stopping services
        if self._onionheaven_registration_succeeded and not self.is_onionheaven:
            try:
                onionheaven.notify_onionheaven_offline(self)
            except Exception:
                pass

        # Stop services
        try:
            self.log("Stopping services...")
            subprocess.run([self.launcher_script, "stop"], capture_output=True, timeout=30)
            self.log("Services stopped")
        except Exception as e:
            self.log(f"Warning: Stop failed: {e}")

        self.stop_caffeinate()
        self.stop_onion_proxy()

        try:
            colima_bin = os.path.join(self.bin_dir, "colima")
            self.log("Stopping Colima VM...")
            env = os.environ.copy()
            env["COLIMA_HOME"] = self.colima_home
            env["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")
            env["LIMA_INSTANCE"] = "onionpress"
            subprocess.run([colima_bin, "stop"], capture_output=True, timeout=60, env=env)
            self.log("Colima stopped")
        except Exception as e:
            self.log(f"Warning: Colima stop failed: {e}")

        self._remove_pid_file()
        self.log("Cleanup complete")

    def handle_wake(self):
        """Handle system wake — signal watchdogs to ADD_ONION, go yellow until verified."""
        self.log("System wake detected — marking Tor as reconnecting")
        self._sleeping = False
        self.startup_time = time.time()  # Reset so "launched in Xs" shows time since wake
        self.start_caffeinate()
        # Reset OnionHeaven check so /online fires when Tor reconnects
        self._onionheaven_checked = False
        self._wordpress_confirmed = False  # Re-verify WordPress once after wake
        if self.is_ready:
            self.is_ready = False
            self._last_bootstrap_pct = 0
            self._bootstrap_stall_count = 0
            self._yellow_since = time.time()
            self.update_menu()
        # Signal all Tor containers to re-ADD_ONION their services
        for container in ["onionpress-tor", "onionheaven"]:
            if self._signal_watchdog(container, "USR2"):
                self.log(f"Sent USR2 (wake) to {container} watchdog")
            else:
                self.log(f"Failed to send USR2 to {container} watchdog")

    def start_status_checker(self):
        """Start background thread to check status periodically"""
        def checker():
            while True:
                if self._port_conflict:
                    time.sleep(30)
                    continue
                if self._sleeping:
                    time.sleep(30)
                    continue
                self.check_status()
                # Adaptive polling based on display state
                state = self.display_state
                if state == "available":
                    time.sleep(30)  # Check every 30 seconds when operational
                elif state == "offline":
                    time.sleep(10)  # Check every 10 seconds when offline (detect recovery)
                else:
                    time.sleep(5)   # Check every 5 seconds during startup/stuck

        thread = threading.Thread(target=checker, daemon=True)
        thread.start()

    @property
    def local_url(self):
        """The local URL for accessing WordPress."""
        return f"http://localhost:{self.wp_port}"

    @rumps.clicked("Copy Onion Address")
    def copy_address(self, _):
        """Copy onion address to clipboard"""
        if self.onion_address and self.onion_address not in ["Starting...", "Not running", "Generating address..."]:
            subprocess.run(
                ["pbcopy"],
                input=self.onion_address.encode(),
                check=True
            )
        else:
            rumps.alert("Onion address not available yet. Please wait for the service to start.")

    def open_local_site(self, _):
        """Open the local WordPress site in the default browser"""
        url = self.local_url
        subprocess.run(["open", url])
        self.log(f"Opened local site: {url}")

    def monitor_tor_browser_install(self):
        """Monitor for Tor Browser installation and offer to open site when detected."""
        self._monitor_browser_install(
            "Tor Browser",
            op_browser.TOR_BROWSER_PATH,
            "Contents/MacOS/firefox",
            lambda url: subprocess.run(["open", "-a", op_browser.TOR_BROWSER_PATH, url]),
        )

    def monitor_brave_install(self):
        """Monitor for Brave Browser installation and offer to open site when detected."""
        self._monitor_browser_install(
            "Brave Browser",
            op_browser.BRAVE_BROWSER_PATH,
            "Contents/MacOS/Brave Browser",
            lambda url: subprocess.run([
                os.path.join(op_browser.BRAVE_BROWSER_PATH, "Contents", "MacOS", "Brave Browser"),
                "--tor", url]),
        )

    def _monitor_browser_install(self, name, app_path, executable_subpath, open_func):
        """Generic browser install monitor."""
        if self.monitoring_tor_install:
            return
        self.monitoring_tor_install = True
        self.log(f"Starting {name} installation monitor")

        def _check():
            found = op_browser.wait_for_app_install(
                app_path, executable_subpath,
                cancel_check=lambda: not self.monitoring_tor_install,
            )
            self.monitoring_tor_install = False
            if not found:
                self.log(f"{name} installation monitor timed out")
                return
            self.log(f"{name} detected in Applications!")
            self.dismiss_setup_dialog()
            address = self.onion_address
            try:
                button_index = self.show_native_alert(
                    title="OnionPress",
                    message=f"{name} is now installed!\n\nWould you like to open your site?\n\n{address}",
                    buttons=["Open Site", "Later"],
                    default_button=0,
                    style="informational"
                )
                if button_index == 0:
                    open_func(f"http://{address}")
                    self.log(f"Opened site in {name}: http://{address}")
            except Exception as e:
                self.log(f"Error showing {name} ready dialog: {e}")

        threading.Thread(target=_check, daemon=True).start()

    # Browsers we trust for open -a / osascript activate
    ALLOWED_BROWSERS = op_browser.ALLOWED_BROWSERS

    def extension_connected_recently(self):
        """Check if a browser extension is actively connected right now."""
        return op_browser.extension_connected_recently(self.app_support)

    def update_browser_menu_title(self):
        """Update the browser menu item title based on which browser is available."""
        self.browser_menu_item.title = op_browser.browser_menu_title(self.app_support)

    def open_tor_browser(self, _):
        """Open the onion address in the best available browser."""
        if self.onion_address and self.onion_address not in ["Starting...", "Not running", "Generating address..."]:
            url = f"http://{self.onion_address}"
            if not op_browser.open_onion_url(url, self.app_support, self.log):
                self.show_browser_install_dialog()
        else:
            rumps.alert("Onion address not available yet. Please wait for the service to start.")

    def show_browser_install_dialog(self):
        """Show dialog with browser options based on what's installed."""
        installed = op_browser.installed_extension_browsers()
        address = self.onion_address or ""
        try:
            if installed:
                browsers_str = ", ".join(installed)
                button_index = self.show_native_alert(
                    title="OnionPress",
                    message=f"Your site is ready!\n\n{address}\n\nInstall the OnionPress extension for {browsers_str} to browse .onion sites.\n\nOr download Tor Browser for a dedicated solution.",
                    buttons=["Install Extension", "Download Tor Browser", "Later"],
                    default_button=0,
                    cancel_button=2,
                    style="informational"
                )
                if button_index == 0:
                    subprocess.run(["open", "https://github.com/brewsterkahle/onionpress/releases/latest"])
                elif button_index == 1:
                    subprocess.run(["open", "https://www.torproject.org/download/"])
                    self.monitor_tor_browser_install()
            else:
                button_index = self.show_native_alert(
                    title="OnionPress",
                    message=f"Your site is ready!\n\n{address}\n\nTo visit .onion sites, download Tor Browser or Brave Browser (both are free).",
                    buttons=["Download Tor Browser", "Download Brave Browser", "Later"],
                    default_button=0,
                    cancel_button=2,
                    style="informational"
                )
                if button_index == 0:
                    subprocess.run(["open", "https://www.torproject.org/download/"])
                    self.monitor_tor_browser_install()
                elif button_index == 1:
                    subprocess.run(["open", "https://brave.com/download/"])
                    self.monitor_brave_install()
        except Exception as e:
            self.log(f"Browser dialog failed: {e}")

    def auto_open_browser(self):
        """Automatically open a browser when service becomes ready"""
        try:
            self._auto_open_browser_inner()
        except Exception as e:
            self.log(f"ERROR in auto_open_browser: {e}")
            import traceback
            self.log(traceback.format_exc())

    def _auto_open_browser_inner(self):
        """Inner implementation of auto_open_browser"""
        # Wait until the onion service is actually reachable before opening
        # the browser. Poll via docker exec into the tor container (the same
        # path the launcher uses) instead of a fixed sleep.
        if not self.onion_address or self.onion_address in ["Starting...", "Not running", "Generating address..."]:
            self.log(f"auto_open_browser: skipping, onion_address={self.onion_address!r}")
            return

        self.log("Waiting for onion service to become reachable before opening browser...")

        # Test reachability through onionheaven's independent Tor
        onion_url = f"http://{self.onion_address}/"
        reachable = False
        for attempt in range(30):  # Up to 90s (30 x 3s)
            try:
                ok, http_code = self._health_checker.check_external_reachability(self.onion_address)
                if ok:
                    reachable = True
                    self.log(f"Onion service reachable after {(attempt + 1) * 3}s")
                    break
            except Exception:
                pass
            time.sleep(3)

        if not reachable:
            self.log("WARNING: Onion service not reachable after 90s, opening browser anyway")

        if self.onion_address and self.onion_address not in ["Starting...", "Not running", "Generating address..."]:
            # First run: open to login page, redirect to homepage (admin bar visible)
            if self._is_first_run:
                url = f"http://{self.onion_address}/wp-login.php?redirect_to=/"
            else:
                url = f"http://{self.onion_address}"

            if op_browser.is_tor_browser_installed():
                self.log(f"Auto-opening Tor Browser: {url}")
                subprocess.run(["open", "-a", "Tor Browser", url])
            else:
                # Wait for the onion proxy to start
                for i in range(15):
                    if self.proxy_server is not None:
                        break
                    time.sleep(1)

                # Wait up to 5 seconds for a browser extension to register
                ext_browser = None
                for i in range(5):
                    ext_browser = self.extension_connected_recently()
                    if ext_browser:
                        break
                    self.log(f"Waiting for extension registration... ({i+1}/5)")
                    time.sleep(1)
                if ext_browser:
                    self.log(f"Auto-opening {ext_browser} (extension detected): {url}")
                    # Open browser first so extension can set up SOCKS routing
                    subprocess.run(["open", "-a", ext_browser])
                    if op_browser.wait_for_extension_active(self.app_support):
                        self.log("Extension active, opening .onion URL")
                    else:
                        self.log("Extension did not poll within 30s, opening .onion URL anyway")
                    subprocess.run(["open", "-a", ext_browser, url])
                    subprocess.run(["osascript", "-e", f'tell application "{ext_browser}" to activate'])
                elif op_browser.is_brave_installed():
                    self.log(f"Auto-opening Brave Browser (Tor mode): {url}")
                    brave_exe = os.path.join(op_browser.BRAVE_BROWSER_PATH, "Contents", "MacOS", "Brave Browser")
                    subprocess.run([brave_exe, "--tor", url])
                else:
                    self.log("No Tor-capable browser found - showing options dialog")
                    self.dismiss_setup_dialog()
                    self.dismiss_launch_splash()
                    self.show_browser_install_dialog()

    def validate_address_prefix(self, prefix):
        """Validate an address prefix string.

        Returns:
            (valid, error_message, suggestion) tuple.
        """
        return op_config.validate_address_prefix(prefix)

    def check_address_prefix_change(self):
        """Check if ADDRESS_PREFIX has changed and handle regeneration.

        Called from a background thread before starting the launcher.
        Returns True if startup should proceed, False to abort.
        """
        # Read configured prefix (fall back to old VANITY_PREFIX for migration)
        prefix = self._read_config_value("ADDRESS_PREFIX", "").strip()
        if not prefix:
            prefix = self._read_config_value("VANITY_PREFIX", "op2").strip()
        if not prefix:
            prefix = "op2"

        # Validate prefix
        valid, error_msg, suggestion = self.validate_address_prefix(prefix)
        if not valid:
            self.log(f"Invalid ADDRESS_PREFIX: {prefix}")

            # Try to determine the current working prefix from the onion address
            current_prefix = "op2"  # fallback default
            try:
                docker_bin = os.path.join(self.bin_dir, "docker")
                env = os.environ.copy()
                env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
                env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")
                result = subprocess.run(
                    [docker_bin, "run", "--rm", "-v", "onionpress-tor-keys:/keys",
                     "alpine", "cat", "/keys/wordpress/hostname"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=15
                )
                hostname = result.stdout.strip().replace(".onion", "")
                if hostname:
                    # Extract the prefix from the current address
                    current_prefix = hostname[:len(prefix)] if len(hostname) >= len(prefix) else hostname[:3]
            except Exception:
                pass

            # Build button list: Use suggestion (if different), Revert, Edit Again
            # NSAlert supports max 3 buttons well
            buttons = []
            if suggestion and suggestion != current_prefix:
                buttons.append(f"Use \"{suggestion}\"")
            buttons.append(f"Revert to \"{current_prefix}\"")
            buttons.append("Edit Again")
            revert_idx = len(buttons) - 2
            edit_idx = len(buttons) - 1

            button_index = self.show_native_alert(
                "Invalid Address Prefix",
                error_msg + (f"\n\nSuggested prefix: \"{suggestion}\"" if suggestion and suggestion != current_prefix else ""),
                buttons=buttons,
                default_button=0,
                cancel_button=edit_idx,
                style="warning"
            )

            if suggestion and suggestion != current_prefix and button_index == 0:
                self.log(f"User accepted suggested prefix: {suggestion}")
                self.write_config_value("ADDRESS_PREFIX", suggestion)
                prefix = suggestion
            elif button_index == revert_idx:
                self.log(f"User reverted to prefix: {current_prefix}")
                self.write_config_value("ADDRESS_PREFIX", current_prefix)
                prefix = current_prefix
            else:
                # Open config for editing and bring TextEdit to front
                self.log("User chose to edit config — opening TextEdit")
                config_file = os.path.join(self.app_support, "config")
                subprocess.Popen(["open", "-a", "TextEdit", config_file])
                subprocess.Popen(["osascript", "-e", 'tell application "TextEdit" to activate'])
                # Show follow-up dialog — when dismissed, retry start
                self.show_native_alert(
                    "Edit Settings",
                    "Edit the config file in TextEdit, then save it (⌘S).\n\nClick OK when you're done to restart.",
                    buttons=["OK"],
                    style="informational"
                )
                self.log("User finished editing — retrying start")
                # Re-read config and retry from the top
                return self.check_address_prefix_change()

        self.log(f"Prefix validation passed, checking current hostname (prefix={prefix})")

        # Skip prefix check if a key import is pending — the launcher will handle it
        pending_file = os.path.join(self.app_support, ".import-key-pending")
        if os.path.exists(pending_file):
            self.log("Key import pending — skipping prefix check (launcher will swap volume)")
            return True

        # Try to get current hostname from tor-keys volume
        try:
            docker_bin = os.path.join(self.bin_dir, "docker")
            env = os.environ.copy()
            env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
            env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")
            result = subprocess.run(
                [docker_bin, "run", "--rm", "-v", "onionpress-tor-keys:/keys",
                 "alpine", "cat", "/keys/wordpress/hostname"],
                capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=15
            )
            current_hostname = result.stdout.strip()
        except Exception as e:
            self.log(f"Could not read current hostname (likely first run): {e}")
            return True  # No existing volume, proceed normally

        if not current_hostname or not current_hostname.endswith(".onion"):
            self.log("No existing onion address found, proceeding with first run")
            return True

        # Check if current hostname already matches the prefix
        hostname_base = current_hostname.replace(".onion", "")
        if hostname_base.startswith(prefix):
            self.log(f"Address prefix '{prefix}' matches current address {current_hostname}")
            return True

        # Mismatch detected — determine old prefix for display
        old_prefix = hostname_base[:len(prefix)] if len(hostname_base) >= len(prefix) else hostname_base[:3]
        self.log(f"Address prefix changed: current address starts with '{old_prefix}', config says '{prefix}'")

        # Show confirmation dialog with time estimates
        time_estimates = (
            "Estimated generation time:\n"
            "  2 characters:  < 1 second\n"
            "  3 characters:  < 1 second\n"
            "  4 characters:  5-30 seconds\n"
            "  5 characters:  10-30 minutes"
        )

        message = (
            f"Your address prefix has changed from what was used to generate "
            f"your current onion address.\n\n"
            f"Current address:\n{current_hostname}\n\n"
            f"New prefix: \"{prefix}\"\n\n"
            f"Changing will generate a NEW onion address.\n"
            f"Your current address will stop working permanently.\n\n"
            f"{time_estimates}"
        )

        button_index = self.show_native_alert(
            "Change Onion Address?",
            message,
            buttons=["Change Address", "Keep Current Address"],
            default_button=1,
            cancel_button=1,
            style="warning"
        )

        if button_index == 0:
            # User confirmed — delete old keys so launcher regenerates
            self.log("User confirmed address prefix change — deleting old keys")

            # Unregister old address from OnionHeaven (it will never come back)
            try:
                onionheaven.unregister_from_onionheaven(self, content_address=current_hostname)
            except Exception as e:
                self.log(f"OnionHeaven unregister failed (continuing): {e}")

            try:
                docker_bin = os.path.join(self.bin_dir, "docker")
                env = os.environ.copy()
                env["DOCKER_HOST"] = f"unix://{self.colima_home}/default/docker.sock"
                env["DOCKER_CONFIG"] = os.path.join(self.app_support, "docker-config")

                # Delete vanity-keys directory
                vanity_dir = os.path.join(self.app_support, "shared", "vanity-keys")
                if os.path.exists(vanity_dir):
                    import shutil
                    shutil.rmtree(vanity_dir)
                    self.log(f"Deleted vanity-keys directory: {vanity_dir}")

                # Delete docker volume
                subprocess.run(
                    [docker_bin, "volume", "rm", "onionpress-tor-keys"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', env=env, timeout=15
                )
                self.log("Deleted onionpress-tor-keys volume")

                # Clear cached onion address
                cached_addr_file = os.path.join(self.app_support, "onion_address")
                if os.path.exists(cached_addr_file):
                    os.remove(cached_addr_file)
                    self.log("Cleared cached onion address")

            except Exception as e:
                self.log(f"Error cleaning up old keys: {e}")
                self.show_native_alert(
                    "Error",
                    f"Failed to remove old onion keys:\n\n{e}\n\nPlease try again or manually delete ~/.onionpress/shared/vanity-keys/ and run: docker volume rm onionpress-tor-keys",
                    buttons=["OK"],
                    style="critical"
                )
                return False

            return True
        else:
            # User cancelled — don't start
            self.log("User chose to keep current address — aborting start")
            return False

    @rumps.clicked("Start")
    def start_service(self, _):
        """Start the WordPress + Tor service"""
        self._stopping = False  # Clear in case Stop was hit previously
        self.menu["Starting..."].title = "Status: Starting..."

        def start():
            # Check if this is first run (uses same marker as __init__)
            first_run = self._is_first_run

            # First run: setup window is already showing (from __init__), just run setup
            if first_run:
                self.log("First run detected - starting installation")
                self.dismiss_launch_splash()  # In case splash was shown instead
                if setup_window:
                    sw = setup_window.get_setup_window()
                    sw.add_log("First-time setup starting...")
                threading.Thread(target=self._run_first_time_setup, daemon=True).start()
                return

            # Not first run: check if address prefix changed before starting
            if not self.check_address_prefix_change():
                self.log("Start aborted due to address prefix issue")
                self.menu["Starting..."].title = "Status: Stopped"
                return

            # Start the service normally
            self.update_splash_status("Starting containers...")
            subprocess.run([self.launcher_script, "start"])

            # Poll until WordPress is responding (replaces fixed sleep)
            self.update_splash_status("Waiting for WordPress...")
            max_wait = 60
            waited = 0
            while waited < max_wait:
                if self.check_wordpress_health(log_result=False):
                    self.log(f"WordPress responding after {waited}s")
                    break
                time.sleep(2)
                waited += 2

            # Send USR2 to arm onionheaven's HSFETCH timer for cold start
            # (watchdog already ADD_ONION'd, this just sets last_recovery_time)
            for container in ["onionpress-tor", "onionheaven"]:
                self._signal_watchdog(container, "USR2")

            self.check_status()

            # Start caffeinate to prevent sleep while service runs
            self.start_caffeinate()

        threading.Thread(target=start, daemon=True).start()

    def _first_run_after_welcome(self):
        """Called after user fills in credentials and clicks Set Up.
        Runs on a background thread (from setup_window callback)."""
        self.start_service(None)

    def _wp_core_install(self, sw):
        """Run wp core install with credentials from the setup window."""
        if not sw or not sw.admin_pass:
            return
        try:
            docker_bin = os.path.join(self.bin_dir, "docker")
            # Get the onion address for --url
            addr_result = subprocess.run(
                [docker_bin, "exec", "onionpress-tor", "cat",
                 "/var/lib/tor/hidden_service/wordpress/hostname"],
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=10
            )
            onion_addr = addr_result.stdout.strip() or "localhost"

            self.log(f"Running wp core install (title={sw.site_title}, user={sw.admin_user})")
            if sw:
                sw.set_status("Configuring WordPress...")
                sw.add_log(f"Installing WordPress as '{sw.admin_user}'...")

            result = subprocess.run(
                [docker_bin, "exec", "onionpress-wordpress",
                 "wp", "core", "install",
                 f"--url=http://{onion_addr}",
                 f"--title={sw.site_title}",
                 f"--admin_user={sw.admin_user}",
                 f"--admin_password={sw.admin_pass}",
                 "--admin_email=admin@onionpress.local",
                 "--allow-root", "--skip-email"],
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=60
            )
            if result.returncode == 0:
                self.log("WordPress installed successfully via wp core install")
                if sw:
                    sw.add_log("WordPress configured successfully")
            else:
                self.log(f"wp core install failed: {result.stderr[-200:]}")
                if sw:
                    sw.add_log("WordPress setup may need manual configuration")
        except Exception as e:
            self.log(f"wp core install error: {e}")

    def _run_first_time_setup(self):
        """Run first-time setup: launcher start with concurrent progress monitoring.

        The launcher 'start' command does everything (start Colima, pull images,
        generate vanity address, docker compose up, wait for services).  We run
        it in a background thread and poll for milestones concurrently so the
        setup window shows real-time progress instead of a single long wait.
        """
        sw = setup_window.get_setup_window() if setup_window else None

        # Step 0: System check — verify bundled binaries exist
        if sw:
            sw.set_step(0)
            sw.set_status("Checking system requirements...")
            sw.add_log("Checking system requirements...")
        self.log("Checking system requirements...")
        missing = []
        for binary in ["docker", "colima", "limactl"]:
            if not os.path.exists(os.path.join(self.bin_dir, binary)):
                missing.append(binary)
        if missing:
            msg = f"Missing binaries: {', '.join(missing)}"
            self.log(f"System check failed: {msg}")
            if sw:
                sw.set_status(msg)
                sw.add_log(f"ERROR: {msg}")
            return
        if sw:
            sw.set_progress(1 / 8)
            sw.complete_step(0)
            sw.add_log("System check passed")

        # Launch the launcher script in the background — it does steps 1-4
        if sw:
            sw.set_status("Starting container runtime...")
            sw.add_log("Starting Colima VM...")
        self.log("Starting Colima VM and containers...")

        launcher_done = threading.Event()
        launcher_failed = [False]

        def run_launcher():
            try:
                result = subprocess.run(
                    [self.launcher_script, "start"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace'
                )
                if result.returncode != 0:
                    # Don't treat as fatal — the launcher may return non-zero for
                    # benign reasons (port offset log message hitting system `log`).
                    # The milestone polling loop will detect real failures via timeout.
                    self.log(f"Launcher exited with rc={result.returncode} (may be benign)")
            except Exception as e:
                launcher_failed[0] = True
                self.log(f"Error in _run_first_time_setup: {e}")
            finally:
                launcher_done.set()

        threading.Thread(target=run_launcher, daemon=True).start()

        # Poll for milestones while the launcher runs
        docker_bin = os.path.join(self.bin_dir, "docker")
        step1_done = False   # Container runtime
        step2_done = False   # Images downloaded
        step3_done = False   # .onion address generated
        step4_done = False   # WordPress responding
        images_found = {'wordpress': False, 'mariadb': False, 'tor': False}
        total_images = len(images_found)
        setup_start = time.time()
        setup_timeout = 600  # 10 minute max

        while not launcher_done.is_set() or not step4_done:
            # Check for timeout
            if time.time() - setup_start > setup_timeout:
                if sw:
                    sw.set_status("Setup timed out — check log for details")
                    sw.add_log("ERROR: Setup timed out after 10 minutes")
                self.log("First-time setup timed out after 10 minutes")
                break

            # Check for launcher failure
            if launcher_failed[0]:
                if sw:
                    sw.set_status("Setup failed — check log for details")
                    sw.add_log("ERROR: Launcher script failed")
                self.log("First-time setup failed")
                break

            # Step 1: Container runtime ready?
            if not step1_done:
                colima_initialized = os.path.join(self.colima_home, ".initialized")
                if os.path.exists(colima_initialized):
                    try:
                        result = subprocess.run(
                            [docker_bin, "info"],
                            capture_output=True, timeout=5, env=os.environ.copy()
                        )
                        if result.returncode == 0:
                            step1_done = True
                            if sw:
                                sw.set_progress(2 / 8)
                                sw.complete_step(1)
                                sw.add_log("Container runtime ready")
                                sw.set_status("Downloading container images...")
                    except Exception:
                        pass

            # Step 2: Docker images downloaded?
            if step1_done and not step2_done:
                try:
                    result = subprocess.run(
                        [docker_bin, "images", "--format", "{{.Repository}}"],
                        capture_output=True, text=True, encoding='utf-8',
                        errors='replace', timeout=5
                    )
                    current_images = result.stdout.strip().split('\n')
                    for name in images_found:
                        if not images_found[name] and any(name in img for img in current_images):
                            images_found[name] = True
                            done = sum(images_found.values())
                            self.log(f"Image downloaded: {name}")
                            if sw:
                                sw.set_progress(
                                    (2 + done / total_images) / 8,
                                    f"Downloading images ({done}/{total_images})"
                                )
                                sw.add_log(f"Image downloaded: {name}")
                    if all(images_found.values()):
                        step2_done = True
                        self.log("All images downloaded")
                        if sw:
                            sw.complete_step(2)
                            sw.add_log("All images downloaded")
                            sw.set_status("Generating .onion address...")
                except Exception:
                    pass

            # Step 3: .onion address generated?
            if step2_done and not step3_done:
                try:
                    result = subprocess.run(
                        [docker_bin, "exec", "onionpress-tor", "cat",
                         "/var/lib/tor/hidden_service/wordpress/hostname"],
                        capture_output=True, text=True, encoding='utf-8',
                        errors='replace', timeout=10
                    )
                    addr = result.stdout.strip()
                    if addr and '.onion' in addr:
                        step3_done = True
                        if sw:
                            sw.complete_step(3)
                            sw.set_progress(4 / 8)
                            sw.add_log(f"Address: {addr[:30]}...")
                            sw.set_status("Starting WordPress + Tor...")
                except Exception:
                    pass

            # Step 4: WordPress responding?
            if step3_done and not step4_done:
                if self.check_wordpress_health(log_result=False):
                    step4_done = True
                    self.log("WordPress responding")
                    if sw:
                        sw.add_log("WordPress responding")
                    # Install WordPress with credentials from setup window
                    if sw and sw.admin_pass:
                        self._wp_core_install(sw)
                    if sw:
                        sw.complete_step(4)
                        sw.set_progress(5 / 8)
                        sw.set_status("Publishing onion address to Tor network (may take 5-10 min)...")

            # Between steps 4 and 5, feed bootstrap % into setup window
            # so it doesn't look frozen during descriptor propagation
            if step4_done and sw and sw.window:
                pct = self._parse_bootstrap_percentage()
                elapsed = int(time.time() - setup_start)
                mins, secs = divmod(elapsed, 60)
                if pct < 100:
                    sw.set_status(f"Tor bootstrap: {pct}% ({mins}m {secs:02d}s)")
                else:
                    sw.set_status(f"Publishing onion address to Tor network (may take 5-10 min)... {mins}m {secs:02d}s")

            time.sleep(3)

        # If launcher succeeded but we missed some steps (e.g. fast cached restart),
        # mark them complete
        if not launcher_failed[0] and sw:
            if not step1_done:
                sw.complete_step(1)
            if not step2_done:
                sw.complete_step(2)
            if not step3_done:
                sw.complete_step(3)
            if not step4_done:
                sw.complete_step(4)
            sw.set_progress(5 / 8)
            sw.set_status("Publishing onion address to Tor network (may take 5-10 min)...")

        # Send USR2 to arm onionheaven's HSFETCH timer for cold start
        for container in ["onionpress-tor", "onionheaven"]:
            self._signal_watchdog(container, "USR2")

        self.check_status()
        self.start_caffeinate()

    @rumps.clicked("Stop")
    def stop_service(self, _):
        """Stop the WordPress + Tor service"""
        self._stopping = True  # Prevent health monitor from auto-restarting
        self._run_generation += 1  # Cancel any pending SIGHUP threads
        self.menu["Starting..."].title = "Status: Stopping..."

        def stop():
            # Notify OnionHeaven before stopping services
            if self._onionheaven_registration_succeeded and not self.is_onionheaven:
                try:
                    onionheaven.notify_onionheaven_offline(self)
                except Exception:
                    pass

            subprocess.run([self.launcher_script, "stop"])
            time.sleep(1)
            self.check_status()

            # Stop background processes
            self.stop_web_log_capture()
            self.stop_container_log_capture()
            self.stop_caffeinate()
            self.stop_onion_proxy()
            self._stopping = False

        threading.Thread(target=stop, daemon=True).start()

    @rumps.clicked("Restart")
    def restart_service(self, _):
        """Restart the WordPress + Tor service"""
        self._stopping = False  # Clear in case Stop was hit previously
        self.menu["Starting..."].title = "Status: Restarting..."
        self.icon = self.icon_starting  # Change icon to indicate restarting

        def restart():
            # Mark as not ready during restart
            self.is_ready = False
            self.is_running = False
            self._was_ready = False
            self._last_bootstrap_pct = 0
            self._bootstrap_stall_count = 0
            self._yellow_since = None
            self.auto_opened_browser = False  # Re-open browser after restart

            # Check if address prefix changed before restarting
            if not self.check_address_prefix_change():
                self.log("Restart aborted due to address prefix issue")
                self.menu["Starting..."].title = "Status: Stopped"
                self.icon = self.icon_stopped
                return

            # Run restart command
            subprocess.run([self.launcher_script, "restart"])

            # Poll until WordPress is responding (replaces fixed sleep)
            max_wait = 60
            waited = 0
            while waited < max_wait:
                if self.check_wordpress_health(log_result=False):
                    self.log(f"WordPress responding after restart ({waited}s)")
                    break
                time.sleep(2)
                waited += 2

            # Check status after restart
            self.check_status()

        threading.Thread(target=restart, daemon=True).start()

    @rumps.clicked("View Logs")
    def view_logs(self, _):
        """Open logs in built-in log viewer"""
        log_file = self._onionpress_log.current_path()
        if os.path.exists(log_file):
            _LogViewerWindow.show_for_file(log_file, "OnionPress Log")
        else:
            rumps.alert("No logs available yet")

    @rumps.clicked("View Web Usage Log")
    def view_web_log(self, _):
        """Open WordPress access log in built-in log viewer"""
        if not self.is_running:
            rumps.alert("Service not running. Please start the service first.")
            return

        web_log_file = self._wp_visitors_log.current_path()

        # Ensure the log file exists
        if not os.path.exists(web_log_file):
            # Create it and wait a moment for logs to populate
            open(web_log_file, 'a').close()
            time.sleep(1)

        # Open in built-in log viewer (filtered log excludes health check pings)
        _LogViewerWindow.show_for_file(web_log_file, "OnionPress Web Usage Log")

    def get_version(self):
        """Get version from Info.plist"""
        try:
            with open(self.info_plist, 'rb') as f:
                plist = plistlib.load(f)
                return plist.get('CFBundleShortVersionString', 'Unknown')
        except Exception:
            return 'Unknown'

    def read_config_value(self, key, default=""):
        """Read a value from the config file."""
        return op_config.read_value(self.config_file, key, default)

    def write_config_value(self, key, value):
        """Write a value to the config file."""
        if not os.path.exists(self.config_file):
            config_template = os.path.join(self.resources_dir, "config-template.txt")
            if os.path.exists(config_template):
                import shutil
                shutil.copy2(config_template, self.config_file)
        op_config.write_value(self.config_file, key, value)

    @rumps.clicked("Settings...")
    def open_settings(self, _):
        """Show GUI settings dialog."""
        from onionpress.settings_ui import show_settings_dialog

        # Create default config if it doesn't exist
        if not os.path.exists(self.config_file):
            config_template = os.path.join(self.parent_resources_dir, "config-template.txt")
            if os.path.exists(config_template):
                import shutil
                shutil.copy2(config_template, self.config_file)

        icon_path = os.path.join(self.resources_dir, "app-icon.png")

        def _restart_caffeinate():
            self.stop_caffeinate()
            self.start_caffeinate()

        show_settings_dialog(
            config_path=self.config_file,
            icon_path=icon_path,
            launcher_script=self.launcher_script,
            log_func=self.log,
            callbacks={
                'write_config': self.write_config_value,
                'restart_caffeinate': _restart_caffeinate,
                'add_login_item': self.add_login_item,
                'remove_login_item': self.remove_login_item,
            },
        )

    @rumps.clicked("Backup...")
    def backup(self, _):
        """Create a full backup of OnionPress (Tor keys, database, wp-content)"""
        # Show credentials dialog using AppKit accessory view
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Backup OnionPress")
        alert.setInformativeText_(
            "Enter your WordPress administrator credentials.\n"
            "The password will be used to encrypt the backup.")

        icon_path = os.path.join(self.resources_dir, "app-icon.png")
        if os.path.exists(icon_path):
            icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
            if icon:
                alert.setIcon_(icon)

        # Build accessory view with username and password fields
        container = AppKit.NSView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, 300, 70))

        user_label = AppKit.NSTextField.labelWithString_("Username:")
        user_label.setFrame_(AppKit.NSMakeRect(0, 48, 80, 18))
        container.addSubview_(user_label)

        user_field = AppKit.NSTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(85, 44, 210, 24))
        user_field.setStringValue_("admin")
        container.addSubview_(user_field)

        pass_label = AppKit.NSTextField.labelWithString_("Password:")
        pass_label.setFrame_(AppKit.NSMakeRect(0, 18, 80, 18))
        container.addSubview_(pass_label)

        pass_field = AppKit.NSSecureTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(85, 14, 210, 24))
        container.addSubview_(pass_field)

        alert.setAccessoryView_(container)
        alert.addButtonWithTitle_("Backup").setKeyEquivalent_("\r")
        alert.addButtonWithTitle_("Cancel").setKeyEquivalent_("\x1b")

        # Make username field first responder
        alert.window().setInitialFirstResponder_(user_field)
        user_field.setNextKeyView_(pass_field)

        response = alert.runModal()
        if response != 1000:  # Not "Backup"
            return

        username = user_field.stringValue().strip()
        password = pass_field.stringValue()

        if not username or not password:
            rumps.alert(title="Missing Credentials",
                        message="Both username and password are required.")
            return

        # Verify credentials
        self.log("Backup: verifying credentials...")
        ok, err = backup_manager.verify_wp_admin(username, password)
        if not ok:
            self.log(f"Backup: credential verification failed: {err}")
            rumps.alert(title="Verification Failed", message=err)
            return

        # Show NSSavePanel for output location
        panel = AppKit.NSSavePanel.savePanel()
        panel.setTitle_("Save Backup")
        panel.setNameFieldStringValue_(
            backup_manager.backup_filename(self.onion_address, username))
        panel.setDirectoryURL_(
            AppKit.NSURL.fileURLWithPath_(os.path.expanduser("~/Downloads/")))
        panel.setAllowedContentTypes_([
            AppKit.UTType.typeWithFilenameExtension_("zip")])

        if panel.runModal() != 1:  # NSModalResponseOK
            return

        output_path = panel.URL().path()

        # Show progress window (stored on self to prevent garbage collection)
        self._progress_window = _BackupProgressWindow("Backing Up OnionPress")
        self._progress_window.show()

        def do_backup():
            pw = self._progress_window
            try:
                def log_and_update(msg):
                    self.log(msg)
                    display = msg.replace("Backup: ", "") if msg.startswith("Backup: ") else msg
                    _main_thread(lambda: pw.update(display))

                backup_manager.create_backup(
                    self.onion_address, username, password,
                    output_path, self.version, log_and_update)

                size_mb = os.path.getsize(output_path) / (1024 * 1024)
                msg = f"Backup saved to {os.path.basename(output_path)} ({size_mb:.1f} MB)"
                _main_thread(lambda: pw.finish(msg))
            except Exception as e:
                self.log(f"Backup failed: {e}")
                _main_thread(lambda: pw.finish(f"Backup failed: {e}"))

        threading.Thread(target=do_backup, daemon=True).start()

    @rumps.clicked("Restore...")
    def restore(self, _):
        """Restore OnionPress from a backup zip"""
        # File picker for .zip
        panel = AppKit.NSOpenPanel.openPanel()
        panel.setTitle_("Select OnionPress Backup")
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setAllowedContentTypes_([
            AppKit.UTType.typeWithFilenameExtension_("zip")])

        if panel.runModal() != 1:  # NSModalResponseOK
            return

        zip_path = panel.URL().path()

        # Try to extract username from backup filename
        # Format: OnionPress-<addr>-<username>-<date>.zip
        zip_name = os.path.basename(zip_path)
        backup_user = None
        if zip_name.startswith("OnionPress-") and zip_name.endswith(".zip"):
            parts = zip_name[len("OnionPress-"):-len(".zip")].split("-")
            if len(parts) >= 3:
                # parts[0] = addr prefix, parts[1] = username, rest = date
                backup_user = parts[1]

        # Prompt for password
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_("Enter Backup Password")
        if backup_user:
            alert.setInformativeText_(
                f"Enter the password of '{backup_user}' that was used "
                f"when this backup was created.")
        else:
            alert.setInformativeText_(
                "Enter the password that was used when this backup was created.")

        icon_path = os.path.join(self.resources_dir, "app-icon.png")
        if os.path.exists(icon_path):
            icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
            if icon:
                alert.setIcon_(icon)

        pass_field = AppKit.NSSecureTextField.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, 300, 24))
        alert.setAccessoryView_(pass_field)
        alert.addButtonWithTitle_("Continue").setKeyEquivalent_("\r")
        alert.addButtonWithTitle_("Cancel").setKeyEquivalent_("\x1b")
        alert.window().setInitialFirstResponder_(pass_field)

        response = alert.runModal()
        if response != 1000:
            return

        password = pass_field.stringValue()
        if not password:
            rumps.alert(title="No Password", message="A password is required.")
            return

        # Validate zip by reading metadata
        try:
            metadata = backup_manager.read_backup_metadata(zip_path, password)
        except ValueError as e:
            rumps.alert(title="Invalid Backup", message=str(e))
            return
        except Exception as e:
            self.log(f"Restore: failed to read backup metadata: {e}")
            rumps.alert(title="Invalid Backup",
                        message=f"Could not read backup: {e}")
            return

        # Show confirmation with backup details
        addr = metadata.get('onion_address', 'unknown')
        date = metadata.get('backup_date', 'unknown')
        user = metadata.get('username', 'unknown')
        ver = metadata.get('onionpress_version', 'unknown')

        button_index = self.show_native_alert(
            title="Confirm Restore",
            message=(
                f"You are about to restore from this backup:\n\n"
                f"Onion address: {addr}\n"
                f"Backup date: {date}\n"
                f"Username: {user}\n"
                f"OnionPress version: {ver}\n\n"
                f"WARNING: This will overwrite your current site, "
                f"database, and onion address. This cannot be undone."),
            buttons=["Cancel", "Restore"],
            default_button=0,
            cancel_button=0,
            style="critical"
        )

        if button_index != 1:
            return

        # Show progress window (stored on self to prevent garbage collection)
        self._progress_window = _BackupProgressWindow("Restoring OnionPress")
        self._progress_window.show()

        def do_restore():
            pw = self._progress_window
            try:
                def log_and_update(msg):
                    self.log(msg)
                    display = msg.replace("Restore: ", "") if msg.startswith("Restore: ") else msg
                    _main_thread(lambda: pw.update(display))

                backup_manager.restore_from_backup(
                    zip_path, password, log_and_update)

                restored_addr = metadata.get('onion_address', addr)

                # Build summary of what was restored and what will happen
                notes = [f"Onion address: {restored_addr}"]

                # Check if onionheaven mode was restored
                onionheaven_addr = "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion"
                if restored_addr == onionheaven_addr:
                    cur_mem = self._read_config_value("VM_MEMORY", "1")
                    try:
                        cur_mem_int = int(cur_mem)
                    except ValueError:
                        cur_mem_int = 1
                    if cur_mem_int < 5:
                        notes.append("OnionHeaven detected — VM memory will increase to 5 GB on relaunch.")
                    else:
                        notes.append(f"OnionHeaven detected — VM memory: {cur_mem} GB.")

                summary = "Site restored successfully.\n\n" + "\n".join(notes)
                _main_thread(lambda: pw.finish_with_restart(summary))
            except Exception as e:
                self.log(f"Restore failed: {e}")
                _main_thread(lambda: pw.finish(f"Restore failed: {e}"))

        threading.Thread(target=do_restore, daemon=True).start()

    def update_docker_images(self, show_notifications=True):
        """Update Docker images (WordPress, MariaDB, Tor)"""
        try:
            self.log("Checking for Docker image updates...")
            docker_compose_file = os.path.join(self.parent_resources_dir, "docker", "docker-compose.yml")

            self.log("Pulling latest Docker images...")
            result = self._docker.compose(
                ["pull"],
                compose_files=[docker_compose_file],
                timeout=300,
            )

            if result.ok:
                self.log("Docker images updated successfully")
                return "Downloaded" in result.stdout or "Pulled" in result.stdout
            else:
                self.log(f"Failed to update Docker images: {result.stderr}")
                return False

        except Exception as e:
            self.log(f"Error updating Docker images: {e}")
            return False

    @rumps.clicked("Check for Updates...")
    def check_for_updates(self, _):
        """Check GitHub for newer versions and update Docker images"""
        # Check for app updates
        app_update_available = False
        try:
            # Fetch latest release from GitHub using curl to avoid permission prompts
            # --cacert needed because py2app bundle can't find CA certs (curl exit 77)
            url = "https://api.github.com/repos/brewsterkahle/onionpress/releases/latest"
            result = subprocess.run(
                ["curl", "-s", "--cacert", "/etc/ssl/cert.pem",
                 "-H", "User-Agent: onionpress", "--max-time", "10", url],
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=15
            )

            if result.returncode == 0:
                data = json.loads(result.stdout)
                latest_version = data.get('tag_name', '').lstrip('v')
                current_version = self.version
                self.log(f"Update check: current={current_version}, latest={latest_version}")

                if latest_version and parse_version(latest_version) > parse_version(current_version):
                    app_update_available = True
                    response = self.show_native_alert(
                        "App Update Available",
                        f"A new version of OnionPress is available!\n\nCurrent: v{current_version}\nLatest: v{latest_version}\n\nInstall will download and replace the app, then restart.",
                        buttons=["Install Update", "Later"],
                        cancel_button=1
                    )
                    if response == 0:  # Install Update clicked
                        threading.Thread(
                            target=self._install_update,
                            args=(data, latest_version),
                            daemon=True
                        ).start()
                        return  # Skip Docker update check — we're about to restart
            else:
                self.log(f"Update check curl failed: exit={result.returncode} stderr={result.stderr.strip()}")
        except Exception as e:
            self.log(f"Update check failed: {e}")
            import traceback
            self.log(traceback.format_exc())
            rumps.alert(
                title="Update Check Failed",
                message=f"Could not check for app updates.\n\nPlease visit:\nhttps://github.com/brewsterkahle/onionpress/releases"
            )

        # Check for Docker image updates
        threading.Thread(target=self._check_docker_updates_async, args=(app_update_available,), daemon=True).start()

    def _install_update(self, release_data, latest_version):
        """Download DMG, extract app, replace currently running app bundle, prompt restart"""
        import tempfile
        import shutil

        # Find the DMG asset in the release
        dmg_url = None
        for asset in release_data.get('assets', []):
            if asset['name'].endswith('.dmg'):
                dmg_url = asset['browser_download_url']
                break

        if not dmg_url:
            self.log("Update failed: no .dmg asset found in release")
            self.show_native_alert(
                "Update Failed",
                "No DMG file found in the release.\n\nPlease update manually from:\nhttps://github.com/brewsterkahle/onionpress/releases",
                style="warning"
            )
            return

        self.log(f"Auto-update: downloading {dmg_url}")

        # Show downloading notification
        rumps.notification(
            title="OnionPress",
            subtitle="Downloading Update...",
            message=f"Downloading v{latest_version}. This may take a minute.",
            sound=False
        )

        tmp_dir = tempfile.mkdtemp(prefix="onionpress-update-")
        dmg_path = os.path.join(tmp_dir, "onionpress.dmg")
        mount_point = os.path.join(tmp_dir, "dmg-mount")

        try:
            # Download the DMG
            result = subprocess.run(
                ["curl", "-L", "-s", "--cacert", "/etc/ssl/cert.pem",
                 "-H", "User-Agent: onionpress", "--max-time", "300",
                 "-o", dmg_path, dmg_url],
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=360
            )
            if result.returncode != 0:
                raise RuntimeError(f"Download failed (curl exit {result.returncode}): {result.stderr.strip()}")

            dmg_size = os.path.getsize(dmg_path)
            self.log(f"Auto-update: downloaded {dmg_size / 1024 / 1024:.1f} MB")

            if dmg_size < 1_000_000:  # Sanity check — DMG should be >> 1MB
                raise RuntimeError(f"Downloaded file too small ({dmg_size} bytes), likely not a valid DMG")

            # Mount the DMG
            os.makedirs(mount_point, exist_ok=True)
            result = subprocess.run(
                ["hdiutil", "attach", dmg_path, "-mountpoint", mount_point, "-nobrowse", "-quiet"],
                capture_output=True,
                text=True, encoding='utf-8', errors='replace',
                timeout=30
            )
            if result.returncode != 0:
                raise RuntimeError(f"Failed to mount DMG: {result.stderr.strip()}")

            self.log(f"Auto-update: DMG mounted at {mount_point}")

            # Find OnionPress.app inside the DMG
            source_app = os.path.join(mount_point, "OnionPress.app")
            if not os.path.isdir(source_app):
                # Some DMGs have the app in a subdirectory
                for item in os.listdir(mount_point):
                    candidate = os.path.join(mount_point, item, "OnionPress.app")
                    if os.path.isdir(candidate):
                        source_app = candidate
                        break
                else:
                    raise RuntimeError(f"OnionPress.app not found in DMG. Contents: {os.listdir(mount_point)}")

            # Determine install location — replace wherever we're currently running from
            install_path = os.path.dirname(self.contents_dir)

            self.log(f"Auto-update: replacing {install_path}")

            # Back up the current app (in case something goes wrong)
            backup_path = install_path + ".bak"
            if os.path.exists(backup_path):
                shutil.rmtree(backup_path)

            # Move current app to backup, copy new app in
            os.rename(install_path, backup_path)
            try:
                # Use cp -R to preserve permissions and structure
                result = subprocess.run(
                    ["cp", "-R", source_app, install_path],
                    capture_output=True,
                    text=True, encoding='utf-8', errors='replace',
                    timeout=120
                )
                if result.returncode != 0:
                    raise RuntimeError(f"Copy failed: {result.stderr.strip()}")
            except Exception:
                # Restore from backup if copy failed
                self.log("Auto-update: copy failed, restoring backup")
                if os.path.exists(install_path):
                    shutil.rmtree(install_path)
                os.rename(backup_path, install_path)
                raise

            # Remove Gatekeeper quarantine flag so macOS doesn't block the downloaded app
            subprocess.run(
                ["xattr", "-dr", "com.apple.quarantine", install_path],
                capture_output=True, timeout=10
            )

            # Remove backup
            shutil.rmtree(backup_path, ignore_errors=True)

            self.log(f"Auto-update: installed v{latest_version} successfully")

            # Unmount DMG
            subprocess.run(
                ["hdiutil", "detach", mount_point, "-quiet"],
                capture_output=True, timeout=15
            )

            # Clean up temp dir
            shutil.rmtree(tmp_dir, ignore_errors=True)

            # Prompt user to restart
            response = self.show_native_alert(
                "Update Installed",
                f"OnionPress v{latest_version} has been installed.\n\nRestart now to use the new version.\n\nThis will briefly stop and restart all containers to pick up any changes. Your onion address stays the same.",
                buttons=["Restart Now", "Later"]
            )
            if response == 0:  # Restart Now
                self._relaunch_app(install_path)

        except Exception as e:
            self.log(f"Auto-update failed: {e}")
            import traceback
            self.log(traceback.format_exc())

            # Clean up mount if still mounted
            subprocess.run(
                ["hdiutil", "detach", mount_point, "-quiet", "-force"],
                capture_output=True, timeout=15
            )
            shutil.rmtree(tmp_dir, ignore_errors=True)

            self.show_native_alert(
                "Update Failed",
                f"Could not install the update automatically.\n\n{e}\n\nYou can update manually from:\nhttps://github.com/brewsterkahle/onionpress/releases",
                style="warning"
            )

    def _relaunch_app(self, app_path):
        """Full quit (stop containers + VM) then launch the new app"""
        self.log(f"Auto-update: relaunching from {app_path}")

        # Spawn a background process that waits for us to exit, then relaunches
        pid = os.getpid()
        relaunch_script = f'''
            while kill -0 {pid} 2>/dev/null; do sleep 0.5; done
            sleep 1
            open "{app_path}"
        '''
        subprocess.Popen(
            ["bash", "-c", relaunch_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )

        # Skip OnionHeaven /offline — we're coming right back up
        self._updating = True

        # Full quit — stops containers, VM, and relaunches with new images
        self.quit_app(None)

    def _check_docker_updates_async(self, app_update_available):
        """Check for Docker updates in background thread"""
        images_updated = self.update_docker_images(show_notifications=True)

        # Show final summary if no app update was available.
        if not app_update_available and not images_updated:
            version = self.version
            self.show_native_alert(
                "No Updates Available",
                f"You're running the latest version (v{version})\nAll container images are up to date."
            )

    def show_setup_dialog(self):
        """Show a persistent setup dialog during first run that stays until service is ready"""
        try:
            # Dismiss any existing dialog first
            self.dismiss_setup_dialog()

            # Create and show dialog on main thread, storing reference for programmatic dismissal
            def create_and_show():
                try:
                    alert = AppKit.NSAlert.alloc().init()
                    alert.setMessageText_("OnionPress Setup")
                    alert.setInformativeText_("Setting up OnionPress for first use...\n\n• Downloading container images\n• Configuring Tor onion service\n• Starting WordPress\n\nThis may take 2-5 minutes depending on your internet speed.\n\nThis window will close automatically to set up your WordPress.")
                    alert.setAlertStyle_(AppKit.NSAlertStyleInformational)

                    btn_dismiss = alert.addButtonWithTitle_("Dismiss")
                    btn_dismiss.setKeyEquivalent_("\r")
                    btn_cancel = alert.addButtonWithTitle_("Cancel Setup")
                    btn_cancel.setKeyEquivalent_("\x1b")

                    # Set app icon
                    icon_path = os.path.join(self.resources_dir, "app-icon.png")
                    if os.path.exists(icon_path):
                        icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
                        if icon:
                            alert.setIcon_(icon)

                    # Store reference so dismiss_setup_dialog can close it
                    self.setup_alert = alert

                    # runModal blocks until button click or abortModal
                    response = alert.runModal()

                    # Close the alert window
                    alert.window().close()
                    self.setup_alert = None

                    # NSModalResponseAbort = -1001 (from abortModal call)
                    if response == AppKit.NSModalResponseAbort:
                        self.log("Setup dialog auto-dismissed (service ready)")
                    else:
                        button_index = response - 1000
                        if button_index == 1:
                            self.log("User cancelled setup - stopping services")
                            subprocess.run([self.launcher_script, "stop"], capture_output=True, timeout=30)
                        elif button_index == 0:
                            self.log("User dismissed setup dialog")

                    self.setup_dialog_showing = False
                except Exception as e:
                    self.log(f"Error in setup dialog: {e}")
                    self.setup_dialog_showing = False
                    self.setup_alert = None

            self.setup_dialog_showing = True
            _main_thread(create_and_show)
            self.log("Setup dialog shown (native NSAlert)")
        except Exception as e:
            self.log(f"Error showing setup dialog: {e}")
            self.setup_dialog_showing = False
            self.log("Setup dialog fallback - dialog failed to show")

    def dismiss_setup_dialog(self):
        """Dismiss the setup dialog if it's showing (native NSAlert)"""
        if self.setup_dialog_showing:
            self.setup_dialog_showing = False
            self.log("Setup dialog marked for dismissal")
            try:
                if self.setup_alert:
                    AppKit.NSApp.abortModal()
                    self.log("Setup dialog dismissed programmatically")
            except Exception as e:
                self.log(f"Error dismissing setup dialog: {e}")

    def monitor_image_downloads(self):
        """Monitor Docker image downloads and log progress."""
        images_to_check = {
            'wordpress': False,
            'mariadb': False,
            'tor': False
        }

        self.log("Monitoring image downloads...")

        # Check for images every 3 seconds for up to 10 minutes
        for i in range(200):
            try:
                result = subprocess.run(
                    ["docker", "images", "--format", "{{.Repository}}"],
                    capture_output=True,
                    text=True, encoding='utf-8', errors='replace',
                    timeout=5
                )
                current_images = result.stdout.strip().split('\n')

                for image_name in images_to_check:
                    if not images_to_check[image_name]:
                        if any(image_name in img for img in current_images):
                            images_to_check[image_name] = True
                            self.log(f"Image downloaded: {image_name}")

                if all(images_to_check.values()):
                    self.log("All images downloaded")
                    break

            except Exception as e:
                self.log(f"Error checking images: {e}")

            time.sleep(3)

    @rumps.clicked("About OnionPress")
    def show_about(self, _):
        """Show about dialog"""
        about_text = f"""OnionPress v{self.version}

Run your own website from your Mac. Just Works. Free, forever.
WordPress + Tor Onion Service

Features:
• Full WordPress that you own and run
• Internet Archive's Wayback Machine integration
• Tor Onion Service with an address you own, forever
• Requires visitors to use Tor or Brave browsers
• Privacy-first design
• Free and open source

Created by the Internet Archive
License: AGPL v3"""

        github_url = "https://github.com/brewsterkahle/onionpress"
        link_label = "GitHub: github.com/brewsterkahle/onionpress"

        def show_dialog():
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("About OnionPress")
            alert.setInformativeText_(about_text)
            alert.setAlertStyle_(AppKit.NSAlertStyleInformational)

            btn = alert.addButtonWithTitle_("OK")
            btn.setKeyEquivalent_("\r")

            # Set app icon if available
            icon_path = os.path.join(self.resources_dir, "app-icon.png")
            if os.path.exists(icon_path):
                icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
                if icon:
                    alert.setIcon_(icon)

            # Create clickable GitHub link as accessory view
            link_field = AppKit.NSTextField.labelWithString_("")
            link_field.setSelectable_(True)
            link_field.setAllowsEditingTextAttributes_(True)
            link_field.setBordered_(False)
            link_field.setDrawsBackground_(False)

            # Build attributed string with clickable link
            attr_str = AppKit.NSMutableAttributedString.alloc().initWithString_(link_label)
            url = AppKit.NSURL.URLWithString_(github_url)
            link_range = AppKit.NSMakeRange(len("GitHub: "), len(link_label) - len("GitHub: "))
            attr_str.addAttribute_value_range_(AppKit.NSLinkAttributeName, url, link_range)
            font = AppKit.NSFont.systemFontOfSize_(AppKit.NSFont.smallSystemFontSize())
            full_range = AppKit.NSMakeRange(0, len(link_label))
            attr_str.addAttribute_value_range_(AppKit.NSFontAttributeName, font, full_range)

            link_field.setAttributedStringValue_(attr_str)
            link_field.sizeToFit()
            alert.setAccessoryView_(link_field)

            alert.runModal()

        if AppKit.NSThread.isMainThread():
            show_dialog()
        else:
            _main_thread(show_dialog)

    @rumps.clicked("Uninstall...")
    def uninstall(self, _):
        """Uninstall OnionPress with mandatory backup prompt"""
        # Step 1: Show critical warning about data loss (native NSAlert - no permissions)
        button_index = self.show_native_alert(
            title="Uninstall Warning",
            message="CRITICAL WARNING\n\nUninstalling will PERMANENTLY DELETE:\n\u2022 Your onion address and private key\n\u2022 All WordPress content and data\n\u2022 Database and configuration\n\nYour site CANNOT BE RECOVERED unless you have a backup.\n\nDo you want to create a backup before uninstalling?",
            buttons=["Cancel", "No, Delete Everything", "Yes, Backup First"],
            default_button=2,
            cancel_button=0,
            style="critical"
        )

        if button_index == 0:  # Cancel
            return

        if button_index == 2:  # Yes, Backup First
            self.log("User chose to backup before uninstall")
            if self.is_running:
                self.backup(None)
            else:
                rumps.alert(
                    title="Service Not Running",
                    message="Cannot create a backup while service is stopped.\n\nPlease start the service first, then try uninstall again."
                )
                return

            # After backup, ask again if they want to continue with uninstall
            button_index = self.show_native_alert(
                title="Confirm Uninstall",
                message="Proceed with uninstall?\n\nThis will permanently delete all data.",
                buttons=["Cancel", "Proceed with Uninstall"],
                default_button=0,
                cancel_button=0,
                style="warning"
            )

            if button_index != 1:  # User didn't click "Proceed"
                return

        # Step 2: Final confirmation with explicit acknowledgment
        # Use rumps.Window for text input (no osascript, no permissions needed)
        window = rumps.Window(
            message="FINAL CONFIRMATION\n\nType 'DELETE' below to confirm permanent deletion of all data:",
            title="Confirm Uninstall",
            default_text="",
            ok="Confirm Deletion",
            cancel="Cancel",
            dimensions=(320, 24)
        )

        response = window.run()
        self.log(f"Final confirmation: button={response.clicked}, text='{response.text}'")

        # Check if user clicked OK and typed "DELETE" (case insensitive)
        if response.clicked != 1:  # User clicked Cancel
            self.log("Uninstall cancelled - user clicked Cancel")
            return

        user_input = response.text.strip().upper() if response.text else ""
        if user_input != "DELETE":
            self.log(f"Uninstall cancelled - user input was: '{response.text.strip()}' (expected 'DELETE')")
            rumps.alert(
                title="Uninstall Cancelled",
                message=f"Uninstall cancelled. Type 'DELETE' to confirm.\n\n(You typed: '{response.text.strip()}')"
            )
            return

        # User confirmed uninstall - run in background thread to avoid beach ball
        def do_uninstall():
            try:
                # First, stop any ongoing setup processes
                self.log("Uninstall: Stopping any ongoing processes...")
                # Stop any ongoing browser monitoring
                self.monitoring_tor_install = False
                self.dismiss_setup_dialog()

                # Unregister from OnionHeaven before stopping (needs running containers)
                if self.is_running:
                    self.log("Uninstall: Unregistering from OnionHeaven...")
                    try:
                        onionheaven.unregister_from_onionheaven(self)
                    except Exception as e:
                        self.log(f"Uninstall: OnionHeaven unregister failed (continuing): {e}")

                # Stop the service (this will cancel any startup in progress)
                self.log("Uninstall: Stopping services...")
                subprocess.run([self.launcher_script, "stop"], capture_output=True, timeout=30)
                self.stop_web_log_capture()
                self.stop_container_log_capture()
                self.stop_onion_proxy()
                self.stop_caffeinate()

                # Stop and delete Colima VM
                # Only affects OnionPress instance, not system Colima
                self.log("Uninstall: Stopping Colima VM...")
                colima_bin = os.path.join(self.bin_dir, "colima")
                env = os.environ.copy()
                env["COLIMA_HOME"] = self.colima_home
                env["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")
                env["LIMA_INSTANCE"] = "onionpress"
                subprocess.run([colima_bin, "stop", "-f"], capture_output=True, timeout=60, env=env)
                self.log("Uninstall: Deleting Colima VM...")
                subprocess.run([colima_bin, "delete", "-f"], capture_output=True, timeout=60, env=env)
                # Kill any orphaned colima/lima processes as a fallback
                subprocess.run(["pkill", "-f", f"{self.colima_home}"], capture_output=True, timeout=10)
                # Note: Docker volumes lived inside the Colima VM and are deleted with it

                # Remove login item LaunchAgent
                self.remove_login_item()

                # Step 3: Remove data directory (but keep it until after we show dialog)
                self.log("Uninstall: Preparing to remove data directory...")
                import shutil
                data_dir_exists = os.path.exists(self.app_support)

                # Step 4: Remove data directory
                if data_dir_exists:
                    shutil.rmtree(self.app_support)
                    self.log("Uninstall: Data directory removed successfully")

                # Step 5: Show final dialog and quit
                # Use show_native_alert which already handles main thread
                self.show_native_alert(
                    title="Uninstall Complete",
                    message="OnionPress has been uninstalled.\n\nFinal step: Move OnionPress.app to the Trash.\n\nClick OK to quit.",
                    buttons=["OK"]
                )
                rumps.quit_application()

            except Exception as e:
                # Show error and quit
                self.show_native_alert(
                    title="Uninstall Error",
                    message=f"An error occurred during uninstall:\n\n{str(e)}\n\nYou may need to manually remove:\n• ~/.onionpress directory\n• Docker volumes (if they exist)",
                    buttons=["OK"]
                )
                rumps.quit_application()

        # Run uninstall in background thread to avoid blocking UI
        threading.Thread(target=do_uninstall, daemon=True).start()

    # ── Settings Page Support ─────────────────────────────────────

    def write_status_to_volume(self):
        """Write status.json, config-current.json, recent-logs.txt to the shared Docker volume
        so the WordPress settings/status pages can display current state."""
        try:
            # Determine state
            if not self.is_running:
                state = "stopped"
            elif self.is_ready:
                state = "running"
            elif not self._has_internet:
                state = "offline"
            elif self._yellow_since and (time.time() - self._yellow_since) > 300:
                state = "stuck"
            else:
                state = "starting"

            # Get container states
            containers = {}
            try:
                result = subprocess.run(
                    ["docker", "ps", "--format", "{{.Names}}\t{{.State}}",
                     "--filter", "name=onionpress"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace',
                    timeout=5
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().splitlines():
                        parts = line.split("\t", 1)
                        if len(parts) == 2:
                            containers[parts[0]] = parts[1]
            except Exception:
                pass

            # Get uptime
            uptime_seconds = int(time.time() - self.startup_time) if self.is_running else 0

            # Bootstrap percentage
            bootstrap_pct = self._last_bootstrap_pct if hasattr(self, '_last_bootstrap_pct') else 0
            if self.is_ready:
                bootstrap_pct = 100

            # Wayback queue count
            wq_count = 0
            try:
                with self._wayback_queue_lock:
                    wq_count = len(self._wayback_queue)
            except Exception:
                pass

            # OnionHeaven stats
            oh_server_active = getattr(self, 'is_onionheaven', False)
            oh_stats = {'server_active': oh_server_active, 'client_registered': False,
                        'client_enabled': True, 'client_hub': '', 'registered_count': 0,
                        'online_count': 0, 'taken_over_count': 0, 'takeover_containers': 0}
            if oh_server_active:
                try:
                    result = subprocess.run(
                        ["docker", "exec", "onionheaven", "sqlite3",
                         "/var/lib/onionpress/onionheaven/registry.db",
                         "SELECT COUNT(*), SUM(CASE WHEN status='online' THEN 1 ELSE 0 END), "
                         "SUM(CASE WHEN status='taken-over' THEN 1 ELSE 0 END) FROM registry"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=5
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        parts = result.stdout.strip().split("|")
                        oh_stats['registered_count'] = int(parts[0] or 0)
                        oh_stats['online_count'] = int(parts[1] or 0)
                        oh_stats['taken_over_count'] = int(parts[2] or 0)
                except Exception:
                    pass
                try:
                    result = subprocess.run(
                        ["docker", "ps", "--format", "{{.Names}}",
                         "--filter", "name=onionheaven-takeover"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=5
                    )
                    if result.returncode == 0:
                        oh_stats['takeover_containers'] = len([
                            l for l in result.stdout.strip().splitlines() if l.strip()
                        ])
                except Exception:
                    pass
            oh_stats['client_enabled'] = self._read_config_value("REGISTER_WITH_ONIONHEAVEN", "yes") == "yes"
            oh_stats['client_hub'] = self._read_config_value(
                "ONIONHEAVEN_ADDRESS", "oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion")

            onion_addr = self.onion_address if self.onion_address and ".onion" in str(self.onion_address) else ""

            # System load averages and host uptime
            load_avg = list(os.getloadavg())
            try:
                host_uptime = int(time.time() - self._host_boot_time)
            except AttributeError:
                # Cache boot time on first call
                try:
                    r = subprocess.run(["sysctl", "-n", "kern.boottime"],
                                       capture_output=True, text=True, timeout=2)
                    # Output like: { sec = 1710345600, usec = 0 } ...
                    import re
                    m = re.search(r'sec\s*=\s*(\d+)', r.stdout)
                    self._host_boot_time = int(m.group(1)) if m else time.time()
                except Exception:
                    self._host_boot_time = time.time()
                host_uptime = int(time.time() - self._host_boot_time)

            import datetime
            status = {
                'state': state,
                'version': self.version,
                'onion_address': onion_addr,
                'tor_impl': self._read_config_value("TOR_IMPL", "tor"),
                'uptime_seconds': uptime_seconds,
                'bootstrap_pct': bootstrap_pct,
                'containers': containers,
                'wayback_queue_count': wq_count,
                'updated_at': datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                'platform': 'macos',
                'load_avg': load_avg,
                'host_uptime_seconds': host_uptime,
                'onionheaven': oh_stats,
            }

            status_json = json.dumps(status, indent=2)

            # Write status.json
            subprocess.run(
                ["docker", "exec", "-i", "onionpress-wordpress",
                 "tee", "/var/lib/onionpress/status.json"],
                input=status_json, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=5
            )

            # Write version file
            subprocess.run(
                ["docker", "exec", "-i", "onionpress-wordpress",
                 "tee", "/var/lib/onionpress/version"],
                input=self.version, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=5
            )

            # Write config-current.json
            config = {}
            config_file = os.path.join(self.app_support, "config")
            try:
                with open(config_file, encoding='utf-8', errors='replace') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        if '=' in line:
                            key, val = line.split('=', 1)
                            config[key] = val
            except (OSError, IOError):
                pass

            config_json = json.dumps(config, indent=2)
            subprocess.run(
                ["docker", "exec", "-i", "onionpress-wordpress",
                 "tee", "/var/lib/onionpress/config-current.json"],
                input=config_json, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=5
            )

            # Write recent logs
            log_file = os.path.join(self.app_support, "onionpress.log")
            try:
                with open(log_file, encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
                recent = "".join(lines[-100:])
                subprocess.run(
                    ["docker", "exec", "-i", "onionpress-wordpress",
                     "tee", "/var/lib/onionpress/recent-logs.txt"],
                    input=recent, capture_output=True, text=True,
                    encoding='utf-8', errors='replace', timeout=5
                )
            except (OSError, IOError):
                pass

        except Exception:
            pass  # Container may not be running

    def poll_config_updates(self):
        """Check for config changes written by the WordPress settings page."""
        try:
            result = self._docker.exec(
                "onionpress-wordpress",
                ["cat", "/var/lib/onionpress/config-updates.json"],
                timeout=5,
                quiet=True,
            )
            if not result.ok or not result.output:
                return

            updates = json.loads(result.output)
            if not updates or not isinstance(updates, dict):
                return

            self.log(f"Settings page: applying {len(updates)} config update(s)")

            for key, val in updates.items():
                old_val = self._read_config_value(key)
                if old_val != val:
                    self.write_config_value(key, val)
                    self.log(f"  {key}: {old_val!r} → {val!r}")

            self._docker.exec(
                "onionpress-wordpress",
                ["rm", "-f", "/var/lib/onionpress/config-updates.json"],
                timeout=5,
            )

            self.cloudflare_tunnel_enabled = bool(self._read_config_value("CLOUDFLARE_TUNNEL_TOKEN"))

        except json.JSONDecodeError:
            pass
        except Exception:
            pass  # Container may not be running

    def poll_requested_actions(self):
        """Check for action requests from the WordPress settings page."""
        try:
            result = self._docker.exec(
                "onionpress-wordpress",
                ["cat", "/var/lib/onionpress/requested-action"],
                timeout=5,
                quiet=True,
            )
            if not result.ok or not result.output:
                return

            action = result.output.strip()

            self._docker.exec(
                "onionpress-wordpress",
                "rm -f /var/lib/onionpress/requested-action /var/lib/onionpress/service-result.json",
                timeout=5,
            )

            if action == "refresh-status":
                self.write_status_to_volume()
                return

            self.log(f"Settings page: handling action '{action}'")

            # Run action in background thread so it doesn't block the polling loop
            threading.Thread(
                target=self._handle_requested_action, args=(action,),
                daemon=True
            ).start()

        except Exception:
            pass

    def _handle_requested_action(self, action):
        """Execute a requested action from the WordPress settings page."""
        import datetime

        def _write_result(filename, data):
            """Write a JSON result file to the WordPress shared volume."""
            try:
                self._docker.run(
                    ["exec", "-i", "onionpress-wordpress",
                     "tee", f"/var/lib/onionpress/{filename}"],
                    timeout=5, input=json.dumps(data),
                )
            except Exception:
                pass

        def _clear_pending():
            try:
                self._docker.exec(
                    "onionpress-wordpress",
                    "rm -f /var/lib/onionpress/service-pending",
                    timeout=5,
                )
            except Exception:
                pass

        now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            if action in ("restart", "start"):
                # Write pending marker
                try:
                    subprocess.run(
                        ["docker", "exec", "onionpress-wordpress",
                         "sh", "-c", f"echo {action} > /var/lib/onionpress/service-pending"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=5
                    )
                except Exception:
                    pass

                if action == "restart":
                    self.log("Settings page: restarting OnionPress...")
                    self.run_command("restart")
                else:
                    self.log("Settings page: starting OnionPress...")
                    self.run_command("start")

                # Wait for services to come back
                import time
                for _ in range(30):
                    time.sleep(2)
                    if self.is_running:
                        break

                self.write_status_to_volume()
                containers_info = ""
                try:
                    r = subprocess.run(
                        ["docker", "ps", "--filter", "name=onionpress", "--format", "{{.Names}}: {{.Status}}"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5
                    )
                    if r.returncode == 0:
                        containers_info = r.stdout.strip().replace("onionpress-", "").replace("\n", ", ")
                except Exception:
                    pass
                _write_result("service-result.json", {
                    "success": True, "action": action,
                    "message": f"{'Restarted' if action == 'restart' else 'Started'} {containers_info}",
                    "completed_at": now_iso
                })
                _clear_pending()

            elif action == "stop":
                self.log("Settings page: stopping OnionPress...")
                self.run_command("stop")
                _write_result("service-result.json", {
                    "success": True, "action": "stop",
                    "message": "OnionPress stopped", "completed_at": now_iso
                })

            elif action == "check-reachability":
                self.log("Settings page: running reachability test...")
                onion_addr = self.onion_address if self.onion_address and ".onion" in str(self.onion_address) else ""
                if not onion_addr:
                    _write_result("reachability-result.json", {
                        "reachable": False, "error": "No onion address found",
                        "tested_at": now_iso
                    })
                else:
                    try:
                        r = subprocess.run(
                            ["docker", "exec", "onionheaven",
                             "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                             "--max-time", "60", "--socks5-hostname", "127.0.0.1:9050",
                             f"http://{onion_addr}/"],
                            capture_output=True, text=True, encoding='utf-8', errors='replace',
                            timeout=75
                        )
                        http_code = r.stdout.strip() or "000"
                        reachable = http_code.isdigit() and 200 <= int(http_code) < 500
                    except Exception:
                        http_code = "000"
                        reachable = False
                    _write_result("reachability-result.json", {
                        "reachable": reachable, "http_code": http_code,
                        "address": onion_addr, "tested_at": now_iso
                    })
                    self.log(f"Settings page: reachability test -> HTTP {http_code} (reachable={reachable})")

            elif action == "generate-vanity":
                self.log("Settings page: generating vanity address...")
                prefix = self._read_config_value("ADDRESS_PREFIX", "op2")
                try:
                    # Use the onionpress script's prefix change mechanism:
                    # stop → delete arti state volume → start (triggers key regen)
                    self.run_command("stop")
                    import time
                    time.sleep(2)
                    # Delete the arti state volume so a new key is generated on next start
                    subprocess.run(
                        ["docker", "volume", "rm", "onionpress-arti-state"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=15
                    )
                    self.run_command("start")
                    for _ in range(60):
                        time.sleep(2)
                        if self.is_running and self.onion_address and ".onion" in str(self.onion_address):
                            break
                    self.write_status_to_volume()
                    new_addr = self.onion_address or ""
                    _write_result("vanity-result.json", {
                        "success": True, "address": new_addr,
                        "generated_at": now_iso
                    })
                    self.log(f"Settings page: vanity address generated: {new_addr}")
                except Exception as e:
                    _write_result("vanity-result.json", {
                        "success": False, "error": str(e),
                        "generated_at": now_iso
                    })
                    self.log(f"Settings page: vanity generation failed: {e}")

            elif action == "import-key-file":
                self.log("Settings page: importing key...")
                try:
                    r = subprocess.run(
                        ["docker", "exec", "onionpress-wordpress",
                         "cat", "/var/lib/onionpress/import-key-data"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=5
                    )
                    key_data = r.stdout.strip() if r.returncode == 0 else ""
                    subprocess.run(
                        ["docker", "exec", "onionpress-wordpress",
                         "rm", "-f", "/var/lib/onionpress/import-key-data"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=5
                    )
                    if not key_data:
                        _write_result("import-result.json", {
                            "success": False, "error": "No key data found",
                            "imported_at": now_iso
                        })
                    else:
                        # Use the onionpress script's import-key command
                        r = subprocess.run(
                            [self.launcher_script, "import-key", key_data],
                            capture_output=True, text=True, encoding='utf-8', errors='replace',
                            timeout=120
                        )
                        if r.returncode == 0:
                            # Restart to pick up the imported key
                            self.run_command("restart")
                            import time
                            for _ in range(60):
                                time.sleep(2)
                                if self.is_running and self.onion_address and ".onion" in str(self.onion_address):
                                    break
                            self.write_status_to_volume()
                            new_addr = self.onion_address or ""
                            _write_result("import-result.json", {
                                "success": True, "address": new_addr,
                                "imported_at": now_iso
                            })
                            self.log(f"Settings page: key imported, address: {new_addr}")
                        else:
                            error_msg = r.stderr.strip() or r.stdout.strip() or "Import failed"
                            _write_result("import-result.json", {
                                "success": False, "error": error_msg,
                                "imported_at": now_iso
                            })
                            self.log(f"Settings page: key import failed: {error_msg}")
                except Exception as e:
                    _write_result("import-result.json", {
                        "success": False, "error": str(e),
                        "imported_at": now_iso
                    })
                    self.log(f"Settings page: key import failed: {e}")

            elif action == "create-backup":
                self.log("Settings page: creating backup...")
                try:
                    r = subprocess.run(
                        ["docker", "exec", "onionpress-wordpress",
                         "cat", "/var/lib/onionpress/backup-password"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=5
                    )
                    password = r.stdout.strip() if r.returncode == 0 else ""
                    subprocess.run(
                        ["docker", "exec", "onionpress-wordpress",
                         "rm", "-f", "/var/lib/onionpress/backup-password"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=5
                    )
                    if not password:
                        _write_result("backup-result.json", {
                            "success": False, "error": "No password provided",
                            "created_at": now_iso
                        })
                    else:
                        import tempfile
                        onion_short = (self.onion_address or "site").replace(".onion", "")[:8]
                        import time
                        filename = f"OnionPress-{onion_short}-{time.strftime('%Y-%m-%d-%H-%M')}.zip"
                        tmp_path = os.path.join(tempfile.gettempdir(), filename)

                        import backup_manager
                        onion_addr = self.onion_address or "unknown"
                        backup_manager.create_backup(
                            onion_addr, "admin", password,
                            tmp_path, self.version, self.log
                        )

                        # Copy into WordPress container for download
                        subprocess.run(
                            ["docker", "cp", tmp_path,
                             f"onionpress-wordpress:/var/lib/onionpress/{filename}"],
                            capture_output=True, text=True, encoding='utf-8', errors='replace',
                            timeout=30
                        )
                        subprocess.run(
                            ["docker", "exec", "onionpress-wordpress",
                             "chown", "www-data:www-data", f"/var/lib/onionpress/{filename}"],
                            capture_output=True, text=True, encoding='utf-8', errors='replace',
                            timeout=5
                        )
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass
                        _write_result("backup-result.json", {
                            "success": True, "filename": filename,
                            "created_at": now_iso
                        })
                        self.log(f"Settings page: backup created: {filename}")
                except Exception as e:
                    _write_result("backup-result.json", {
                        "success": False, "error": str(e),
                        "created_at": now_iso
                    })
                    self.log(f"Settings page: backup failed: {e}")

            elif action == "restore-backup":
                self.log("Settings page: restoring from backup...")
                try:
                    r = subprocess.run(
                        ["docker", "exec", "onionpress-wordpress",
                         "cat", "/var/lib/onionpress/restore-password"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=5
                    )
                    password = r.stdout.strip() if r.returncode == 0 else ""
                    subprocess.run(
                        ["docker", "exec", "onionpress-wordpress",
                         "rm", "-f", "/var/lib/onionpress/restore-password"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=5
                    )

                    import tempfile
                    local_zip = os.path.join(tempfile.gettempdir(), "onionpress-restore.zip")
                    subprocess.run(
                        ["docker", "cp",
                         "onionpress-wordpress:/var/lib/onionpress/restore-upload.zip",
                         local_zip],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=60
                    )
                    subprocess.run(
                        ["docker", "exec", "onionpress-wordpress",
                         "rm", "-f", "/var/lib/onionpress/restore-upload.zip"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=5
                    )

                    if not password or not os.path.exists(local_zip):
                        _write_result("restore-result.json", {
                            "success": False, "error": "Missing password or backup file",
                            "restored_at": now_iso
                        })
                    else:
                        import backup_manager
                        backup_manager.restore_from_backup(local_zip, password, self.log)
                        try:
                            os.remove(local_zip)
                        except OSError:
                            pass
                        # Restart to pick up restored keys
                        self.run_command("restart")
                        import time
                        for _ in range(30):
                            time.sleep(2)
                            if self.is_running:
                                break
                        self.write_status_to_volume()
                        new_addr = self.onion_address or ""
                        _write_result("restore-result.json", {
                            "success": True, "address": new_addr,
                            "restored_at": now_iso
                        })
                        self.log(f"Settings page: restore complete, address: {new_addr}")
                except Exception as e:
                    _write_result("restore-result.json", {
                        "success": False, "error": str(e),
                        "restored_at": now_iso
                    })
                    self.log(f"Settings page: restore failed: {e}")

            else:
                self.log(f"Settings page: unknown action '{action}'")

        except Exception as e:
            self.log(f"Settings page: action '{action}' failed: {e}")

    # ── Wayback Queue ──────────────────────────────────────────────

    def poll_wayback_queue(self):
        """Read the Wayback queue from the WordPress container's shared volume."""
        try:
            result = subprocess.run(
                ["docker", "exec", "onionpress-wordpress",
                 "cat", "/var/lib/onionpress/wayback-queue.json"],
                capture_output=True, text=True, encoding='utf-8', errors='replace',
                timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                queue = json.loads(result.stdout.strip())
                with self._wayback_queue_lock:
                    self._wayback_queue = queue if isinstance(queue, list) else []
            else:
                with self._wayback_queue_lock:
                    self._wayback_queue = []
        except Exception:
            pass  # Container may not be running

    def drain_wayback_queue(self):
        """Process one item from the Wayback queue (rate-limited to 1 per 30s)."""
        now = time.time()
        if now - self._wayback_last_drain < 30:
            return

        with self._wayback_queue_lock:
            if not self._wayback_queue:
                return
            item = self._wayback_queue[0]

        url = item.get("url", "")
        if not url:
            self._remove_wayback_queue_item(url)
            return

        self._wayback_last_drain = now
        self.log(f"Wayback queue: archiving {url}")

        # .onion first, clearnet-via-Tor fallback. Never direct clearnet.
        endpoints = [
            ("http://web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion/save",
             ["--socks5-hostname", "127.0.0.1:9050"]),
            ("https://web.archive.org/save",
             ["--socks5-hostname", "127.0.0.1:9050"]),
        ]

        for save_url, proxy_args in endpoints:
            try:
                result = subprocess.run(
                    ["docker", "exec", "onionpress-tor",
                     "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     "--max-time", "30"] + proxy_args + [
                     "--data-urlencode", f"url={url}",
                     save_url],
                    capture_output=True, text=True, encoding='utf-8', errors='replace',
                    timeout=45
                )
                http_code = result.stdout.strip()

                if http_code and 200 <= int(http_code) < 500:
                    self.log(f"Wayback queue: archived {url} (HTTP {http_code})")
                    self._remove_wayback_queue_item(url)
                    return
            except Exception as e:
                self.log(f"Wayback queue: failed for {url} via {save_url}: {e}")

        self.log(f"Wayback queue: all endpoints failed for {url}")

    def _remove_wayback_queue_item(self, url):
        """Remove a URL from the Wayback queue in the container."""
        with self._wayback_queue_lock:
            self._wayback_queue = [i for i in self._wayback_queue if i.get("url") != url]
            updated = json.dumps(self._wayback_queue)
        try:
            subprocess.run(
                ["docker", "exec", "-i", "onionpress-wordpress",
                 "tee", "/var/lib/onionpress/wayback-queue.json"],
                input=updated, capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=5
            )
        except Exception:
            pass

    @rumps.clicked("Quit")
    def quit_app(self, _):
        """Quit the application"""
        self.log("="*60)
        self.log("QUIT BUTTON CLICKED - v2.4.44 RUNNING")
        self.log("="*60)
        self._quitting = True  # Prevent _handle_terminate from running again

        # Stop monitoring immediately
        self.monitoring_tor_install = False
        self.dismiss_setup_dialog()
        self.stop_web_log_capture()
        self.stop_container_log_capture()

        # Close any open log viewer windows
        _LogViewerWindow.close_all()

        # Show stopped icon and status during shutdown — stays visible until
        # all services are actually stopped (prevents port conflicts on relaunch)
        def show_stopping():
            self.menu["Starting..."].title = "Quitting..."
            self.icon = self.icon_stopped
        _main_thread(show_stopping)

        def cleanup_and_quit():
            # Small delay to ensure UI updates
            time.sleep(0.5)

            # Stop onion proxy first (release port 9077 immediately)
            self.stop_onion_proxy()

            # Stop caffeinate to allow Mac to sleep
            self.stop_caffeinate()

            # Notify OnionHeaven before stopping services (containers needed for curl)
            # Skip if restarting for an update — we're coming right back
            if self._onionheaven_registration_succeeded and not self.is_onionheaven and not getattr(self, '_updating', False):
                try:
                    onionheaven.notify_onionheaven_offline(self)
                except Exception:
                    pass

            # Now run cleanup — 90s timeout for OnionHeaven farm containers
            try:
                self.log("Stopping services...")
                subprocess.run([self.launcher_script, "stop"], capture_output=True, timeout=90)
                self.log("Services stopped")
            except subprocess.TimeoutExpired:
                self.log("Warning: Stop command timed out")
            except Exception as e:
                self.log(f"Warning: Stop failed: {e}")

            try:
                colima_bin = os.path.join(self.bin_dir, "colima")
                self.log("Stopping Colima VM...")
                env = os.environ.copy()
                env["COLIMA_HOME"] = self.colima_home
                env["LIMA_HOME"] = os.path.join(self.colima_home, "_lima")
                env["LIMA_INSTANCE"] = "onionpress"
                subprocess.run([colima_bin, "stop"], capture_output=True, timeout=60, env=env)
                self.log("Colima stopped")
            except subprocess.TimeoutExpired:
                self.log("Warning: Colima stop timed out")
            except Exception as e:
                self.log(f"Warning: Colima stop failed: {e}")

            # Remove PID file
            self._remove_pid_file()

            self.log("Cleanup complete, exiting")

            # Now quit (must dispatch to main thread)
            _main_thread(rumps.quit_application)

        # Non-daemon thread so the app stays alive until cleanup finishes
        threading.Thread(target=cleanup_and_quit, daemon=False).start()

if __name__ == "__main__":
    OnionPressApp().run()
