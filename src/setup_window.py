#!/usr/bin/env python3
"""
OnionPress Setup Progress Window — safe implementation.

Uses only standard AppKit controls (NSTextField, NSProgressIndicator,
NSButton, NSScrollView/NSTextView).  No custom drawRect_, no CGColor
layer styling, no NSTimer animations.  Retro feel comes from Monaco
font + purple/orange/cream colour scheme.

Two phases:
  1. Welcome — collects Site Title, Username, Password then starts setup
  2. Progress — step checklist, progress bar, live log tail
"""

import AppKit
from AppKit import (
    NSWindow, NSView, NSTextField, NSSecureTextField, NSProgressIndicator,
    NSButton, NSImage, NSImageView, NSFont, NSColor, NSMakeRect,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSBackingStoreBuffered, NSCenterTextAlignment, NSLeftTextAlignment,
    NSLineBreakByWordWrapping, NSApp, NSScrollView, NSTextView,
    NSProgressIndicatorStyleBar,
)
import objc
import threading
import os


# ---------------------------------------------------------------------------
# Colour palette  (cream panels, purple headings, orange accents)
# ---------------------------------------------------------------------------

_LIGHT_BG        = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.90, 0.90, 0.91, 1.0)
_CREAM           = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.98, 0.97, 0.92, 1.0)
_HEADING_PURPLE  = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.45, 0.25, 0.50, 1.0)
_ACCENT_ORANGE   = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.85, 0.55, 0.20, 1.0)
_TEXT_DARK        = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.15, 0.15, 0.18, 1.0)
_TEXT_DIM         = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.40, 0.40, 0.45, 1.0)
_GREEN           = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.00, 0.50, 0.20, 1.0)
_LOG_BG          = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.96, 0.95, 0.90, 1.0)


def _monaco(size):
    return NSFont.fontWithName_size_("Monaco", size) or NSFont.monospacedSystemFontOfSize_weight_(size, 0.0)


def _label(frame, text, font=None, color=None, align=NSLeftTextAlignment, wrap=False):
    """Create a read-only NSTextField (label)."""
    tf = NSTextField.alloc().initWithFrame_(frame)
    tf.setStringValue_(text)
    tf.setBezeled_(False)
    tf.setDrawsBackground_(False)
    tf.setEditable_(False)
    tf.setSelectable_(False)
    tf.setAlignment_(align)
    if font:
        tf.setFont_(font)
    if color:
        tf.setTextColor_(color)
    if wrap:
        tf.setLineBreakMode_(NSLineBreakByWordWrapping)
    return tf


def _input_field(frame, placeholder="", secure=False):
    """Create an editable NSTextField (or NSSecureTextField)."""
    cls = NSSecureTextField if secure else NSTextField
    tf = cls.alloc().initWithFrame_(frame)
    tf.setPlaceholderString_(placeholder)
    tf.setBezeled_(True)
    tf.setDrawsBackground_(True)
    tf.setEditable_(True)
    tf.setSelectable_(True)
    tf.setFont_(NSFont.systemFontOfSize_(13))
    return tf


def _logo_path():
    """Path to logo.png (bundle or dev tree)."""
    try:
        bundle = AppKit.NSBundle.mainBundle()
        if bundle and bundle.resourcePath():
            p = os.path.join(bundle.resourcePath(), "logo.png")
            if os.path.exists(p):
                return p
            p = os.path.join(bundle.resourcePath(), "assets", "branding", "logo.png")
            if os.path.exists(p):
                return p
    except Exception:
        pass
    script_dir = os.path.dirname(os.path.realpath(__file__))
    root = os.path.dirname(script_dir)
    for candidate in [
        os.path.join(root, "assets", "branding", "logo.png"),
        os.path.join(root, "logo.png"),
    ]:
        if os.path.exists(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Step definitions
# ---------------------------------------------------------------------------

STEPS = [
    "Checking system requirements",
    "Starting container runtime",
    "Downloading container images",
    "Generating .onion address",
    "Starting WordPress + Tor",
    "Checking reachability",
    "Starting heartbeat",
    "Opening tor-enabled browser",
]

def _default_site_title():
    """Generate default site title from macOS user's initials."""
    try:
        import subprocess
        result = subprocess.run(['id', '-F'], capture_output=True, text=True, timeout=5)
        full_name = result.stdout.strip()
        if full_name:
            initials = ''.join(w[0].upper() for w in full_name.split() if w)
            if initials:
                return f"{initials} OnionPress"
    except Exception:
        pass
    return "My OnionPress Site"


_MARK_DONE    = "\u2713"   # ✓
_MARK_ACTIVE  = "\u27F3"  # ⟳
_MARK_PENDING = "\u00B7"  # ·


# ---------------------------------------------------------------------------
# Main window class
# ---------------------------------------------------------------------------

class SetupProgressWindow(AppKit.NSObject):
    """Safe setup progress window using only standard AppKit controls."""

    def init(self):
        self = objc.super(SetupProgressWindow, self).init()
        if self is None:
            return None
        self.window = None
        self.welcome_view = None    # Phase 1: credentials input
        self.progress_view = None   # Phase 2: step checklist + progress
        self.step_labels = []       # NSTextField per step
        self.progress_bar = None    # NSProgressIndicator
        self.percent_label = None   # NSTextField  "55%"
        self.status_label = None    # NSTextField  status line
        self.log_text_view = None   # NSTextView   log tail
        self.current_step = -1
        self._log_file_path = os.path.expanduser("~/.onionpress/onionpress.log")
        # Credentials from welcome phase
        self.site_title = _default_site_title()
        self.admin_user = "admin"
        self.admin_pass = ""
        self._title_field = None
        self._user_field = None
        self._pass_field = None
        self._on_setup_callback = None  # Called when user clicks "Set Up"
        self._showing_welcome = True
        return self

    # -- window creation ----------------------------------------------------

    def create_window(self):
        width, height = 480, 620
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, width, height),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered,
            False,
        )
        self.window.setTitle_("OnionPress Setup")
        self.window.setBackgroundColor_(_LIGHT_BG)
        self.window.center()
        self.window.setLevel_(AppKit.NSFloatingWindowLevel)
        self.window.setReleasedWhenClosed_(False)
        self.window.setHidesOnDeactivate_(False)

        content = self.window.contentView()
        self._create_welcome_view(content, width, height)
        self._create_progress_view(content, width, height)

        # Start showing welcome
        self.welcome_view.setHidden_(False)
        self.progress_view.setHidden_(True)
        self._showing_welcome = True

    def _create_welcome_view(self, content, width, height):
        """Phase 1: Logo + credential fields + Set Up button."""
        self.welcome_view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        content.addSubview_(self.welcome_view)

        y = height - 20

        # -- Logo --
        logo_path = _logo_path()
        if logo_path:
            logo_image = NSImage.alloc().initWithContentsOfFile_(logo_path)
            if logo_image:
                logo_h = 100
                logo_w = 120
                y -= logo_h
                logo_view = NSImageView.alloc().initWithFrame_(
                    NSMakeRect((width - logo_w) / 2, y, logo_w, logo_h)
                )
                logo_view.setImage_(logo_image)
                logo_view.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
                self.welcome_view.addSubview_(logo_view)
                y -= 8

        # -- Title --
        y -= 24
        title = _label(
            NSMakeRect(20, y, width - 40, 24),
            "Welcome to OnionPress!",
            font=_monaco(16), color=_HEADING_PURPLE,
            align=NSCenterTextAlignment,
        )
        self.welcome_view.addSubview_(title)

        # -- Subtitle --
        y -= 20
        subtitle = _label(
            NSMakeRect(20, y, width - 40, 16),
            "Set up your site and admin account",
            font=_monaco(11), color=_TEXT_DIM,
            align=NSCenterTextAlignment,
        )
        self.welcome_view.addSubview_(subtitle)

        y -= 30  # spacing

        # -- Form fields --
        label_x = 40
        field_x = 180
        field_w = width - field_x - 40

        # Site Title
        y -= 24
        self.welcome_view.addSubview_(_label(
            NSMakeRect(label_x, y, 130, 20),
            "Site Title",
            font=_monaco(12), color=_HEADING_PURPLE,
        ))
        self._title_field = _input_field(
            NSMakeRect(field_x, y - 2, field_w, 24),
            placeholder=_default_site_title(),
        )
        self._title_field.setStringValue_(_default_site_title())
        self.welcome_view.addSubview_(self._title_field)

        # Username
        y -= 40
        self.welcome_view.addSubview_(_label(
            NSMakeRect(label_x, y, 130, 20),
            "Username",
            font=_monaco(12), color=_HEADING_PURPLE,
        ))
        self._user_field = _input_field(
            NSMakeRect(field_x, y - 2, field_w, 24),
            placeholder="admin",
        )
        self._user_field.setStringValue_("admin")
        self.welcome_view.addSubview_(self._user_field)

        # Password
        y -= 40
        self.welcome_view.addSubview_(_label(
            NSMakeRect(label_x, y, 130, 20),
            "Password",
            font=_monaco(12), color=_HEADING_PURPLE,
        ))
        pass_frame = NSMakeRect(field_x, y - 2, field_w - 36, 24)
        # Visible field (shown by default)
        self._pass_field = _input_field(pass_frame, placeholder="Choose a password")
        self.welcome_view.addSubview_(self._pass_field)
        # Secure field (hidden by default)
        self._pass_field_secure = _input_field(pass_frame, placeholder="Choose a password", secure=True)
        self._pass_field_secure.setHidden_(True)
        self.welcome_view.addSubview_(self._pass_field_secure)
        self._pass_visible = True
        # Eye toggle button
        eye_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(field_x + field_w - 32, y - 1, 30, 22)
        )
        eye_btn.setTitle_("\U0001F441")  # 👁
        eye_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        eye_btn.setTarget_(self)
        eye_btn.setAction_(objc.selector(self.togglePasswordVisibility_, signature=b'v@:@'))
        self.welcome_view.addSubview_(eye_btn)

        # Password hint
        y -= 20
        self.welcome_view.addSubview_(_label(
            NSMakeRect(field_x, y, field_w, 16),
            "Save this password somewhere safe.",
            font=NSFont.systemFontOfSize_(10), color=_TEXT_DIM,
        ))

        y -= 30

        # -- Share analytics checkbox --
        self._analytics_check = NSButton.alloc().initWithFrame_(
            NSMakeRect(field_x, y, field_w, 18)
        )
        self._analytics_check.setButtonType_(AppKit.NSButtonTypeSwitch)
        self._analytics_check.setTitle_("Share diagnostic logs with OnionHome")
        self._analytics_check.setFont_(NSFont.systemFontOfSize_(11))
        self._analytics_check.setState_(AppKit.NSControlStateValueOn)
        self.welcome_view.addSubview_(self._analytics_check)

        y -= 16
        self.welcome_view.addSubview_(_label(
            NSMakeRect(field_x + 18, y, field_w - 18, 14),
            "Helps the OnionPress project diagnose issues.",
            font=NSFont.systemFontOfSize_(10), color=_TEXT_DIM,
        ))

        y -= 30  # spacing

        # -- Set Up button --
        setup_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect((width - 200) / 2, y, 200, 40)
        )
        setup_btn.setTitle_("Set Up OnionPress")
        setup_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        setup_btn.setFont_(_monaco(13))
        setup_btn.setTarget_(self)
        setup_btn.setAction_(objc.selector(self.setupClicked_, signature=b'v@:@'))
        setup_btn.setKeyEquivalent_("\r")  # Enter key
        self.welcome_view.addSubview_(setup_btn)

        y -= 30

        # -- Estimated time --
        self.welcome_view.addSubview_(_label(
            NSMakeRect(20, y, width - 40, 16),
            ">> Setup takes about 3-5 minutes",
            font=_monaco(10), color=_ACCENT_ORANGE,
            align=NSCenterTextAlignment,
        ))

    def _create_progress_view(self, content, width, height):
        """Phase 2: Step checklist + progress bar + log area."""
        self.progress_view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        content.addSubview_(self.progress_view)

        y = height - 20

        # -- Logo (smaller) --
        logo_path = _logo_path()
        if logo_path:
            logo_image = NSImage.alloc().initWithContentsOfFile_(logo_path)
            if logo_image:
                logo_h = 80
                logo_w = 100
                y -= logo_h
                logo_view = NSImageView.alloc().initWithFrame_(
                    NSMakeRect((width - logo_w) / 2, y, logo_w, logo_h)
                )
                logo_view.setImage_(logo_image)
                logo_view.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
                self.progress_view.addSubview_(logo_view)
                y -= 4

        # -- Title --
        y -= 20
        title = _label(
            NSMakeRect(20, y, width - 40, 20),
            "[ SETTING UP YOUR ONION SERVICE ]",
            font=_monaco(13), color=_HEADING_PURPLE,
            align=NSCenterTextAlignment,
        )
        self.progress_view.addSubview_(title)

        y -= 12  # spacing

        # -- Step checklist --
        self.step_labels = []
        for i, step_text in enumerate(STEPS):
            y -= 18
            mark = _MARK_PENDING
            text = f"  {mark}  {step_text}"
            lbl = _label(
                NSMakeRect(40, y, width - 80, 16),
                text,
                font=_monaco(11), color=_TEXT_DIM,
            )
            self.progress_view.addSubview_(lbl)
            self.step_labels.append(lbl)

        y -= 12  # spacing

        # -- Progress bar --
        y -= 20
        self.progress_bar = NSProgressIndicator.alloc().initWithFrame_(
            NSMakeRect(40, y, width - 120, 20)
        )
        self.progress_bar.setStyle_(NSProgressIndicatorStyleBar)
        self.progress_bar.setMinValue_(0)
        self.progress_bar.setMaxValue_(100)
        self.progress_bar.setDoubleValue_(0)
        self.progress_bar.setIndeterminate_(False)
        self.progress_view.addSubview_(self.progress_bar)

        self.percent_label = _label(
            NSMakeRect(width - 72, y, 50, 18),
            "0%",
            font=_monaco(11), color=_TEXT_DIM,
            align=NSLeftTextAlignment,
        )
        self.progress_view.addSubview_(self.percent_label)

        y -= 6  # spacing

        # -- Status line --
        y -= 16
        self.status_label = _label(
            NSMakeRect(40, y, width - 80, 14),
            "Initializing...",
            font=_monaco(10), color=_HEADING_PURPLE,
            align=NSCenterTextAlignment,
        )
        self.progress_view.addSubview_(self.status_label)

        y -= 8  # spacing

        # -- Log tail area --
        log_h = max(80, y - 50)
        y -= log_h
        log_scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(40, y, width - 80, log_h)
        )
        log_scroll.setHasVerticalScroller_(True)
        log_scroll.setBorderType_(AppKit.NSBezelBorder)

        self.log_text_view = NSTextView.alloc().initWithFrame_(
            NSMakeRect(0, 0, width - 80 - 15, log_h)
        )
        self.log_text_view.setEditable_(False)
        self.log_text_view.setSelectable_(True)
        self.log_text_view.setFont_(_monaco(9))
        self.log_text_view.setTextColor_(_HEADING_PURPLE)
        self.log_text_view.setBackgroundColor_(_LOG_BG)
        self.log_text_view.setString_("Waiting for log entries...")
        self.log_text_view.setVerticallyResizable_(True)
        self.log_text_view.setHorizontallyResizable_(False)
        self.log_text_view.textContainer().setWidthTracksTextView_(True)

        log_scroll.setDocumentView_(self.log_text_view)
        self.progress_view.addSubview_(log_scroll)

        y -= 8  # spacing

        # -- Buttons --
        y -= 32
        view_log_btn = NSButton.alloc().initWithFrame_(NSMakeRect(40, y, 130, 32))
        view_log_btn.setTitle_("View Log")
        view_log_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        view_log_btn.setTarget_(self)
        view_log_btn.setAction_(objc.selector(self.viewLogClicked_, signature=b'v@:@'))
        self.progress_view.addSubview_(view_log_btn)

        dismiss_btn = NSButton.alloc().initWithFrame_(NSMakeRect(width - 170, y, 130, 32))
        dismiss_btn.setTitle_("Dismiss")
        dismiss_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        dismiss_btn.setTarget_(self)
        dismiss_btn.setAction_(objc.selector(self.dismissClicked_, signature=b'v@:@'))
        self.progress_view.addSubview_(dismiss_btn)

    # -- button handlers ----------------------------------------------------

    def togglePasswordVisibility_(self, sender):
        """Toggle between visible and secure password fields."""
        if self._pass_visible:
            # Switch to secure: copy text, hide visible, show secure
            pw = self._pass_field.stringValue()
            self._pass_field_secure.setStringValue_(pw)
            self._pass_field.setHidden_(True)
            self._pass_field_secure.setHidden_(False)
            self._pass_field_secure.becomeFirstResponder()
            self._pass_visible = False
        else:
            # Switch to visible: copy text, hide secure, show visible
            pw = self._pass_field_secure.stringValue()
            self._pass_field.setStringValue_(pw)
            self._pass_field_secure.setHidden_(True)
            self._pass_field.setHidden_(False)
            self._pass_field.becomeFirstResponder()
            self._pass_visible = True

    def setupClicked_(self, sender):
        """User clicked Set Up — save credentials and switch to progress view."""
        # Read field values
        if self._title_field:
            self.site_title = self._title_field.stringValue() or _default_site_title()
        if self._user_field:
            self.admin_user = self._user_field.stringValue() or "admin"
        # Read from whichever password field is visible
        if self._pass_visible:
            self.admin_pass = self._pass_field.stringValue() or ""
        else:
            self.admin_pass = self._pass_field_secure.stringValue() or ""

        if not self.admin_pass:
            # Generate a random password if none provided
            import secrets
            self.admin_pass = secrets.token_urlsafe(12)

        # Save analytics preference
        if self._analytics_check.state() == AppKit.NSControlStateValueOn:
            self.share_analytics = "yes"
        else:
            self.share_analytics = "no"

        # Switch to progress view
        self._showing_welcome = False
        if self.welcome_view:
            self.welcome_view.setHidden_(True)
        if self.progress_view:
            self.progress_view.setHidden_(False)

        # Fire callback to start setup
        if self._on_setup_callback:
            threading.Thread(target=self._on_setup_callback, daemon=True).start()

    def viewLogClicked_(self, sender):
        try:
            log_path = os.path.expanduser("~/.onionpress/onionpress.log")
            if os.path.exists(log_path):
                try:
                    from onionpress.ui_helpers import LogViewerWindow
                    LogViewerWindow.show_for_file(log_path, "OnionPress Log")
                except ImportError:
                    import subprocess
                    subprocess.Popen(["open", "-a", "Console", log_path])
        except Exception:
            pass

    def dismissClicked_(self, sender):
        self.hide()

    # -- public API ---------------------------------------------------------

    def set_on_setup(self, callback):
        """Set callback for when user clicks Set Up. Called in a background thread."""
        self._on_setup_callback = callback

    def show(self):
        def _show():
            if not self.window:
                self.create_window()
            self.window.makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)
        _on_main(_show)

    def show_welcome(self):
        """Show the welcome/credentials phase."""
        def _show():
            if not self.window:
                self.create_window()
            self._showing_welcome = True
            if self.welcome_view:
                self.welcome_view.setHidden_(False)
            if self.progress_view:
                self.progress_view.setHidden_(True)
            self.window.makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)
        _on_main(_show)

    def show_progress(self):
        """Switch to the progress phase (skip welcome)."""
        def _show():
            if not self.window:
                self.create_window()
            self._showing_welcome = False
            if self.welcome_view:
                self.welcome_view.setHidden_(True)
            if self.progress_view:
                self.progress_view.setHidden_(False)
            self.window.makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)
        _on_main(_show)

    def hide(self):
        def _hide():
            if self.window:
                self.window.orderOut_(None)
        _on_main(_hide)

    def close(self):
        def _close():
            if self.window:
                self.window.close()
                self.window = None
        _on_main(_close)

    def set_step(self, step_index):
        """Update checklist: steps < index get check, index gets spinner, rest pending."""
        def _update():
            self.current_step = step_index
            for i, lbl in enumerate(self.step_labels):
                step_text = STEPS[i]
                if i < step_index:
                    lbl.setStringValue_(f"  {_MARK_DONE}  {step_text}")
                    lbl.setTextColor_(_GREEN)
                elif i == step_index:
                    lbl.setStringValue_(f"  {_MARK_ACTIVE}  {step_text}")
                    lbl.setTextColor_(_HEADING_PURPLE)
                else:
                    lbl.setStringValue_(f"  {_MARK_PENDING}  {step_text}")
                    lbl.setTextColor_(_TEXT_DIM)
        _on_main(_update)

    def complete_step(self, step_index):
        """Mark step as done, advance to next."""
        next_step = step_index + 1
        if next_step < len(STEPS):
            self.set_step(next_step)
        else:
            # All done — mark everything green
            def _update():
                for i, lbl in enumerate(self.step_labels):
                    lbl.setStringValue_(f"  {_MARK_DONE}  {STEPS[i]}")
                    lbl.setTextColor_(_GREEN)
            _on_main(_update)

    def set_progress(self, value, label=None):
        """Set progress bar (0.0-1.0) and optional label."""
        def _update():
            if self.progress_bar:
                self.progress_bar.setDoubleValue_(value * 100)
            if self.percent_label:
                self.percent_label.setStringValue_(f"{int(value * 100)}%")
            if label and self.status_label:
                self.status_label.setStringValue_(label)
        _on_main(_update)

    def set_status(self, message):
        """Update the status line below the progress bar."""
        def _update():
            if self.status_label:
                self.status_label.setStringValue_(message)
        _on_main(_update)

    def set_detail(self, message):
        """Alias for set_status (compatibility)."""
        self.set_status(message)

    def add_log(self, message, status="info"):
        """Append a line to the log tail area and auto-scroll."""
        def _update():
            if not self.log_text_view:
                return
            current = self.log_text_view.string()
            if current == "Waiting for log entries...":
                current = ""
            if current:
                current += "\n"
            current += message
            self.log_text_view.setString_(current)
            # Auto-scroll to bottom
            length = self.log_text_view.string().length() if hasattr(self.log_text_view.string(), 'length') else len(self.log_text_view.string())
            self.log_text_view.scrollRangeToVisible_(AppKit.NSMakeRange(length, 0))
        _on_main(_update)

    def show_completion(self, onion_address=None):
        """All steps done, progress 100%."""
        def _update():
            for i, lbl in enumerate(self.step_labels):
                lbl.setStringValue_(f"  {_MARK_DONE}  {STEPS[i]}")
                lbl.setTextColor_(_GREEN)
            if self.progress_bar:
                self.progress_bar.setDoubleValue_(100)
            if self.percent_label:
                self.percent_label.setStringValue_("100%")
            if self.status_label:
                self.status_label.setStringValue_("Setup complete!")
                self.status_label.setTextColor_(_GREEN)
        _on_main(_update)
        self.add_log("All systems operational")
        if onion_address:
            self.add_log(f"Address: {onion_address}")
        self.add_log("")
        self.add_log("Tip: Click the OnionPress icon in your")
        self.add_log("menu bar to manage your site.")

    # -- compatibility stubs ------------------------------------------------

    def set_modem_active(self, active):
        pass

    def set_tor_final_hop_connected(self):
        pass

    def transition_to_progress(self):
        self.show_progress()

    def set_callbacks(self, on_continue=None, on_cancel=None):
        pass


# ---------------------------------------------------------------------------
# Thread-safe main-thread dispatch
# ---------------------------------------------------------------------------

def _on_main(block):
    """Run block on the main thread."""
    if threading.current_thread() is threading.main_thread():
        block()
    else:
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(block)


# ---------------------------------------------------------------------------
# Singleton access (same API as old setup_window)
# ---------------------------------------------------------------------------

_setup_window = None

def get_setup_window():
    global _setup_window
    if _setup_window is None:
        _setup_window = SetupProgressWindow.alloc().init()
    return _setup_window

def show_setup_progress():
    window = get_setup_window()
    window.show_progress()
    return window

def show_welcome_screen(on_continue=None, on_cancel=None):
    """Show the welcome screen with credential fields."""
    window = get_setup_window()
    window.show_welcome()
    return window

def hide_setup_progress():
    window = get_setup_window()
    window.hide()

def close_setup_progress():
    global _setup_window
    if _setup_window:
        _setup_window.close()
        _setup_window = None


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import time

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)

    win = get_setup_window()

    def on_setup():
        print(f"Title: {win.site_title}")
        print(f"User: {win.admin_user}")
        print(f"Pass: {win.admin_pass}")
        time.sleep(1)
        for i in range(len(STEPS)):
            win.set_step(i)
            win.set_progress(i / len(STEPS), f"Step {i+1}/{len(STEPS)}")
            win.add_log(f"Running: {STEPS[i]}")
            time.sleep(1.5)
        win.show_completion("abc123xyz.onion")
        time.sleep(3)
        win.close()
        AppKit.NSApp.terminate_(None)

    win.set_on_setup(on_setup)
    win.show_welcome()

    app.run()
