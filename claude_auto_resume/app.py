"""
app.py — Main Claude Auto-Resume menu bar application.

A lightweight macOS menu bar app built with rumps that:
1. Lists open terminal windows from Terminal.app and iTerm2
2. Lets the user select terminals to watch
3. Polls watched terminals every 60 seconds for session limit messages
4. Schedules automatic "continue" sends at the reset time + 1 minute
5. Shows macOS notifications on auto-resume
"""

import logging
import threading
from datetime import datetime
from functools import partial

import rumps

from .terminal import list_terminals, TerminalInfo
from .watcher import Watcher, WatcherState

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("claude_auto_resume.app")


# Menu bar icons/titles for different states
ICON_IDLE = "👁"          # Watching, no limit detected
ICON_WAITING = "⏳"       # A resume is pending
ICON_NONE = "👁‍🗨"       # No terminals being watched


def _safe_clear_menu(menu_item):
    """Clear a rumps MenuItem's submenu, handling the case where the NSMenu isn't initialized yet."""
    try:
        if menu_item._menu is not None:
            menu_item.clear()
    except AttributeError:
        pass


class ClaudeAutoResumeApp(rumps.App):
    """macOS menu bar app for auto-resuming Claude Code sessions."""

    def __init__(self):
        super().__init__(
            name="Claude Auto-Resume",
            title=ICON_NONE,
            quit_button=None,  # We'll add our own quit button
        )

        # Active watchers keyed by TTY
        self.watchers: dict[str, Watcher] = {}

        # Pending resume timers keyed by TTY
        self._resume_timers: dict[str, threading.Timer] = {}

        # Build menu structure
        self.watch_menu = rumps.MenuItem("Watch Terminal")
        self.active_menu = rumps.MenuItem("Active Watches")
        self.active_menu.add(rumps.MenuItem("No active watches", callback=None))

        self.menu = [
            self.watch_menu,
            self.active_menu,
            None,  # Separator
            rumps.MenuItem("Quit", callback=self._quit),
        ]

        # Populate terminal list on startup
        self._refresh_terminal_list()

        logger.info("Claude Auto-Resume started")

    # ─── Terminal List ─────────────────────────────────────────────────

    def _refresh_terminal_list(self, _=None):
        """Refresh the list of available terminals in the Watch menu."""
        _safe_clear_menu(self.watch_menu)

        try:
            terminals = list_terminals()
        except Exception as e:
            logger.error("Failed to list terminals: %s", e)
            self.watch_menu.add(rumps.MenuItem("Error listing terminals", callback=None))
            self.watch_menu.add(rumps.MenuItem("↻ Refresh", callback=self._refresh_terminal_list))
            return

        if not terminals:
            self.watch_menu.add(rumps.MenuItem("No terminals found", callback=None))
        else:
            for term in terminals:
                # Skip terminals we're already watching
                if term.tty in self.watchers:
                    continue

                # Build a descriptive label
                short_tty = term.tty.replace("/dev/", "")
                label = f"{term.app}: {term.name} ({short_tty})"

                item = rumps.MenuItem(label, callback=partial(self._on_watch_terminal, term))
                self.watch_menu.add(item)

        self.watch_menu.add(None)  # Separator
        self.watch_menu.add(rumps.MenuItem("↻ Refresh", callback=self._refresh_terminal_list))

    # ─── Watch / Unwatch ───────────────────────────────────────────────

    def _on_watch_terminal(self, terminal: TerminalInfo, _=None):
        """Start watching a terminal."""
        if terminal.tty in self.watchers:
            return

        watcher = Watcher(terminal=terminal)
        self.watchers[terminal.tty] = watcher

        logger.info("Now watching %s (%s)", terminal.tty, terminal.name)

        self._update_active_menu()
        self._update_title()
        self._refresh_terminal_list()  # Remove from the "Watch" menu

    def _on_stop_watching(self, tty: str, _=None):
        """Stop watching a terminal and cancel any pending resume timer."""
        if tty in self.watchers:
            self.watchers[tty].stop()
            del self.watchers[tty]

        if tty in self._resume_timers:
            self._resume_timers[tty].cancel()
            del self._resume_timers[tty]

        logger.info("Stopped watching %s", tty)

        self._update_active_menu()
        self._update_title()
        self._refresh_terminal_list()  # Add back to the "Watch" menu

    def _on_stop_all(self, _=None):
        """Stop all watchers."""
        for tty in list(self.watchers.keys()):
            self._on_stop_watching(tty)

    # ─── Polling ───────────────────────────────────────────────────────

    @rumps.timer(60)
    def _poll_tick(self, _):
        """
        Main polling loop — runs every 60 seconds.
        Checks each WATCHING terminal for the session limit message.
        """
        for tty, watcher in list(self.watchers.items()):
            if watcher.state != WatcherState.WATCHING:
                continue

            reset_info = watcher.poll()

            if reset_info is not None:
                # Session limit detected! Schedule the resume.
                self._schedule_resume(tty, watcher)
                self._update_active_menu()
                self._update_title()

    def _schedule_resume(self, tty: str, watcher: Watcher):
        """Schedule a one-shot timer to send 'continue' at the resume time."""
        delay = watcher.seconds_until_resume()
        if delay is None:
            logger.error("Cannot schedule resume for %s — no resume time", tty)
            return

        logger.info(
            "Scheduling resume for %s in %.0f seconds (at %s)",
            tty,
            delay,
            watcher.reset_info.resume_time.strftime("%I:%M %p") if watcher.reset_info else "?",
        )

        # Cancel any existing timer for this TTY
        if tty in self._resume_timers:
            self._resume_timers[tty].cancel()

        # Create a one-shot timer
        timer = threading.Timer(delay, self._fire_resume, args=[tty])
        timer.daemon = True
        timer.start()
        self._resume_timers[tty] = timer

    def _fire_resume(self, tty: str):
        """Called when the resume timer fires. Sends 'continue' to the terminal."""
        watcher = self.watchers.get(tty)
        if not watcher:
            logger.warning("Resume timer fired for %s but watcher not found", tty)
            return

        success = watcher.fire_resume()

        # Clean up timer reference
        self._resume_timers.pop(tty, None)

        if success:
            # Show macOS notification
            rumps.notification(
                title="Claude Auto-Resume",
                subtitle="Session Resumed ✅",
                message=f"Sent 'continue' to {watcher.terminal.name} ({tty.replace('/dev/', '')})",
                sound=True,
            )
        else:
            rumps.notification(
                title="Claude Auto-Resume",
                subtitle="Resume Failed ❌",
                message=f"Could not send 'continue' to {tty.replace('/dev/', '')}. The terminal may have been closed.",
                sound=True,
            )

        self._update_active_menu()
        self._update_title()

    # ─── UI Updates ────────────────────────────────────────────────────

    def _update_title(self):
        """Update the menu bar icon/title based on current state."""
        if not self.watchers:
            self.title = ICON_NONE
            return

        # If any watcher is waiting to resume, show the waiting icon
        any_waiting = any(
            w.state == WatcherState.WAITING_TO_RESUME
            for w in self.watchers.values()
        )
        self.title = ICON_WAITING if any_waiting else ICON_IDLE

    def _update_active_menu(self):
        """Rebuild the Active Watches submenu."""
        _safe_clear_menu(self.active_menu)

        if not self.watchers:
            self.active_menu.add(rumps.MenuItem("No active watches", callback=None))
            return

        for tty, watcher in self.watchers.items():
            short_tty = tty.replace("/dev/", "")
            label = f"{watcher.status_text}  {watcher.terminal.name} ({short_tty})"

            # The menu item — clicking it stops watching
            item = rumps.MenuItem(label, callback=partial(self._on_stop_watching, tty))
            self.active_menu.add(item)

        if len(self.watchers) > 1:
            self.active_menu.add(None)  # Separator
            self.active_menu.add(rumps.MenuItem("✕ Stop All", callback=self._on_stop_all))

    # ─── Quit ──────────────────────────────────────────────────────────

    def _quit(self, _):
        """Clean shutdown."""
        logger.info("Shutting down...")

        # Cancel all pending timers
        for timer in self._resume_timers.values():
            timer.cancel()
        self._resume_timers.clear()

        # Stop all watchers
        for watcher in self.watchers.values():
            watcher.stop()
        self.watchers.clear()

        rumps.quit_application()


def main():
    """Entry point."""
    app = ClaudeAutoResumeApp()
    app.run()


if __name__ == "__main__":
    main()
