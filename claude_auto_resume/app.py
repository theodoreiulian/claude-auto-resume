"""
app.py — Main Claude Auto-Resume menu bar application.

A lightweight macOS menu bar app built with rumps that:
1. Lists open Claude Code sessions — Terminal.app tabs and Conductor workspaces
2. Lets the user select sessions to watch
3. Polls watched sessions for session limit messages
4. Schedules automatic "continue" sends at the reset time + 1 minute
5. Shows macOS notifications on auto-resume
"""

import logging
import threading
from datetime import datetime
from functools import partial

import rumps

from .targets import Target, TargetKind, list_targets
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
        self.watch_menu = rumps.MenuItem("Watch Session")
        self.active_menu = rumps.MenuItem("Active Watches")
        self.active_menu.add(rumps.MenuItem("No active watches", callback=None))

        self.menu = [
            self.watch_menu,
            self.active_menu,
            None,  # Separator
            rumps.MenuItem("Quit", callback=self._quit),
        ]

        # Populate the session list on startup
        self._refresh_target_list()

        logger.info("Claude Auto-Resume started")

    # ─── Session List ──────────────────────────────────────────────────

    def _refresh_target_list(self, _=None):
        """Refresh the list of watchable sessions in the Watch menu."""
        _safe_clear_menu(self.watch_menu)

        try:
            targets = list_targets()
        except Exception as e:
            logger.error("Failed to list sessions: %s", e)
            self.watch_menu.add(rumps.MenuItem("Error listing sessions", callback=None))
            self.watch_menu.add(rumps.MenuItem("↻ Refresh", callback=self._refresh_target_list))
            return

        # Anything already being watched lives in the Active Watches menu instead.
        available = [t for t in targets if t.key not in self.watchers]

        if not available:
            self.watch_menu.add(rumps.MenuItem("No sessions found", callback=None))
        else:
            # Group by host so a long list of Terminal tabs doesn't bury Conductor's
            # workspaces (and vice versa). Headers are inert menu items.
            first_group = True
            for kind in (TargetKind.TERMINAL, TargetKind.CONDUCTOR):
                group = [t for t in available if t.kind is kind]
                if not group:
                    continue
                if not first_group:
                    self.watch_menu.add(None)  # Separator between groups
                first_group = False

                self.watch_menu.add(rumps.MenuItem(group[0].source_label, callback=None))
                for target in group:
                    item = rumps.MenuItem(
                        f"   {target.menu_label}",
                        callback=partial(self._on_watch_target, target),
                    )
                    self.watch_menu.add(item)

        self.watch_menu.add(None)  # Separator
        self.watch_menu.add(rumps.MenuItem("↻ Refresh", callback=self._refresh_target_list))

    # ─── Watch / Unwatch ───────────────────────────────────────────────

    def _on_watch_target(self, target: Target, _=None):
        """Start watching a session."""
        if target.key in self.watchers:
            return

        self.watchers[target.key] = Watcher(target=target)

        logger.info("Now watching %s (%s)", target.key, target.name)

        self._update_active_menu()
        self._update_title()
        self._refresh_target_list()  # Remove from the "Watch" menu

    def _on_stop_watching(self, key: str, _=None):
        """Stop watching a session and cancel any pending resume timer."""
        if key in self.watchers:
            self.watchers[key].stop()
            del self.watchers[key]

        if key in self._resume_timers:
            self._resume_timers[key].cancel()
            del self._resume_timers[key]

        logger.info("Stopped watching %s", key)

        self._update_active_menu()
        self._update_title()
        self._refresh_target_list()  # Add back to the "Watch" menu

    def _on_stop_all(self, _=None):
        """Stop all watchers."""
        for key in list(self.watchers.keys()):
            self._on_stop_watching(key)

    # ─── Polling ───────────────────────────────────────────────────────

    @rumps.timer(15)
    def _poll_tick(self, _):
        """
        Main polling loop — runs every 15 seconds.
        Checks each WATCHING session for the session limit message.
        """
        for key, watcher in list(self.watchers.items()):
            if watcher.state != WatcherState.WATCHING:
                continue

            reset_info = watcher.poll()

            if reset_info is not None:
                # Session limit detected! Schedule the resume.
                self._schedule_resume(key, watcher)
                self._update_active_menu()
                self._update_title()

    def _schedule_resume(self, key: str, watcher: Watcher):
        """Schedule a one-shot timer to send 'continue' at the resume time."""
        delay = watcher.seconds_until_resume()
        if delay is None:
            logger.error("Cannot schedule resume for %s — no resume time", key)
            return

        logger.info(
            "Scheduling resume for %s in %.0f seconds (at %s)",
            key,
            delay,
            watcher.reset_info.resume_time.strftime("%I:%M %p") if watcher.reset_info else "?",
        )

        # Cancel any existing timer for this target
        if key in self._resume_timers:
            self._resume_timers[key].cancel()

        # Create a one-shot timer
        timer = threading.Timer(delay, self._fire_resume, args=[key])
        timer.daemon = True
        timer.start()
        self._resume_timers[key] = timer

    def _fire_resume(self, key: str):
        """Called when the resume timer fires. Sends 'continue' to the session."""
        watcher = self.watchers.get(key)
        if not watcher:
            logger.warning("Resume timer fired for %s but watcher not found", key)
            return

        success = watcher.fire_resume()

        # Clean up timer reference
        self._resume_timers.pop(key, None)

        where = f"{watcher.target.source_label}: {watcher.target.name}"

        if success:
            # Show macOS notification
            rumps.notification(
                title="Claude Auto-Resume",
                subtitle="Session Resumed ✅",
                message=f"Sent 'continue' to {where}",
                sound=True,
            )
        else:
            rumps.notification(
                title="Claude Auto-Resume",
                subtitle="Resume Failed ❌",
                message=(
                    f"Could not send 'continue' to {where}. "
                    f"{watcher.last_error or 'The session may have been closed.'}"
                ),
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

        for key, watcher in self.watchers.items():
            target = watcher.target
            label = f"{watcher.status_text}  {target.source_label}: {target.menu_label}"

            # The menu item — clicking it stops watching
            item = rumps.MenuItem(label, callback=partial(self._on_stop_watching, key))
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
