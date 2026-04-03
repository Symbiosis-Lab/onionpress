#!/usr/bin/env python3
"""
OnionPress Setup Progress Window — safe implementation.

Uses only standard AppKit controls (NSTextField, NSProgressIndicator,
NSButton, NSScrollView/NSTextView).  No custom drawRect_, no CGColor
layer styling, no NSTimer animations.  Retro feel comes from Monaco
font + purple/orange/cream colour scheme.
"""

import AppKit
from AppKit import (
    NSWindow, NSView, NSTextField, NSProgressIndicator, NSButton,
    NSImage, NSImageView, NSFont, NSColor, NSMakeRect,
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
        self.step_labels = []       # NSTextField per step
        self.progress_bar = None    # NSProgressIndicator
        self.percent_label = None   # NSTextField  "55%"
        self.status_label = None    # NSTextField  status line
        self.log_text_view = None   # NSTextView   log tail
        self.current_step = -1
        self._log_file_path = os.path.expanduser("~/.onionpress/onionpress.log")
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
        y = height - 20  # top padding

        # -- Logo --
        logo_path = _logo_path()
        if logo_path:
            logo_image = NSImage.alloc().initWithContentsOfFile_(logo_path)
            if logo_image:
                logo_h = 120
                logo_w = 140
                y -= logo_h
                logo_view = NSImageView.alloc().initWithFrame_(
                    NSMakeRect((width - logo_w) / 2, y, logo_w, logo_h)
                )
                logo_view.setImage_(logo_image)
                logo_view.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
                content.addSubview_(logo_view)
                y -= 8

        # -- Title --
        y -= 24
        title = _label(
            NSMakeRect(20, y, width - 40, 24),
            "[ SETTING UP YOUR ONION SERVICE ]",
            font=_monaco(14), color=_HEADING_PURPLE,
            align=NSCenterTextAlignment,
        )
        content.addSubview_(title)

        # -- Subtitle --
        y -= 18
        subtitle = _label(
            NSMakeRect(20, y, width - 40, 16),
            ">> Estimated time: 2-5 minutes",
            font=_monaco(10), color=_ACCENT_ORANGE,
            align=NSCenterTextAlignment,
        )
        content.addSubview_(subtitle)

        y -= 16  # spacing

        # -- Step checklist --
        self.step_labels = []
        for i, step_text in enumerate(STEPS):
            y -= 20
            mark = _MARK_PENDING
            text = f"  {mark}  {step_text}"
            lbl = _label(
                NSMakeRect(40, y, width - 80, 18),
                text,
                font=_monaco(12), color=_TEXT_DIM,
            )
            content.addSubview_(lbl)
            self.step_labels.append(lbl)

        y -= 16  # spacing

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
        content.addSubview_(self.progress_bar)

        self.percent_label = _label(
            NSMakeRect(width - 72, y, 50, 18),
            "0%",
            font=_monaco(11), color=_TEXT_DIM,
            align=NSLeftTextAlignment,
        )
        content.addSubview_(self.percent_label)

        y -= 8  # spacing

        # -- Status line --
        y -= 18
        self.status_label = _label(
            NSMakeRect(40, y, width - 80, 16),
            "Initializing...",
            font=_monaco(10), color=_TEXT_DIM,
            align=NSCenterTextAlignment,
        )
        content.addSubview_(self.status_label)

        y -= 12  # spacing

        # -- Log tail area --
        y -= 120
        log_scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(40, y, width - 80, 120)
        )
        log_scroll.setHasVerticalScroller_(True)
        log_scroll.setBorderType_(AppKit.NSBezelBorder)

        self.log_text_view = NSTextView.alloc().initWithFrame_(
            NSMakeRect(0, 0, width - 80 - 15, 120)
        )
        self.log_text_view.setEditable_(False)
        self.log_text_view.setSelectable_(True)
        self.log_text_view.setFont_(_monaco(9))
        self.log_text_view.setTextColor_(_HEADING_PURPLE)
        self.log_text_view.setBackgroundColor_(_LOG_BG)
        self.log_text_view.setString_("Waiting for log entries...")
        # Auto-scroll: allow vertical resize
        self.log_text_view.setVerticallyResizable_(True)
        self.log_text_view.setHorizontallyResizable_(False)
        self.log_text_view.textContainer().setWidthTracksTextView_(True)

        log_scroll.setDocumentView_(self.log_text_view)
        content.addSubview_(log_scroll)

        y -= 16  # spacing

        # -- Buttons --
        y -= 32
        view_log_btn = NSButton.alloc().initWithFrame_(NSMakeRect(40, y, 130, 32))
        view_log_btn.setTitle_("View Log")
        view_log_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        view_log_btn.setTarget_(self)
        view_log_btn.setAction_(objc.selector(self.viewLogClicked_, signature=b'v@:@'))
        content.addSubview_(view_log_btn)

        dismiss_btn = NSButton.alloc().initWithFrame_(NSMakeRect(width - 170, y, 130, 32))
        dismiss_btn.setTitle_("Dismiss")
        dismiss_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        dismiss_btn.setTarget_(self)
        dismiss_btn.setAction_(objc.selector(self.dismissClicked_, signature=b'v@:@'))
        content.addSubview_(dismiss_btn)

    # -- button handlers ----------------------------------------------------

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

    def show(self):
        def _show():
            if not self.window:
                self.create_window()
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

    # -- compatibility stubs ------------------------------------------------

    def set_modem_active(self, active):
        pass  # No modem visualizer in safe version

    def set_tor_final_hop_connected(self):
        pass  # No Tor hop animation in safe version

    def transition_to_progress(self):
        pass  # No welcome/progress split — always shows progress

    def set_callbacks(self, on_continue=None, on_cancel=None):
        pass  # No welcome screen callbacks needed

    def show_welcome(self):
        self.show()  # Just show the progress view


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
    window.show()
    return window

def show_welcome_screen(on_continue=None, on_cancel=None):
    """Compatibility — just shows the progress window."""
    window = get_setup_window()
    window.show()
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
    win.show()

    def demo():
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

    threading.Thread(target=demo, daemon=True).start()
    app.run()
