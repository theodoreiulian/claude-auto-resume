"""
watcher.py — Per-target watcher with state machine.

Each Watcher monitors a single Claude Code session — a Terminal.app tab or a
Conductor workspace — transitioning through:
    WATCHING → LIMIT_DETECTED → WAITING_TO_RESUME → RESUMED → WATCHING

The watcher is driven by the main app's polling timer — it doesn't create
its own timers. Instead, it reports when it needs a one-shot resume timer
to be scheduled, and the app handles timer management.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Optional

from .detector import ResetInfo, detect_session_limit
from .targets import Target, TargetKind, read_content, send_text_detailed

logger = logging.getLogger("claude_auto_resume.watcher")


class WatcherState(Enum):
    """States for the per-terminal watcher state machine."""
    WATCHING = auto()           # Actively polling for session limit message
    LIMIT_DETECTED = auto()     # Just detected the limit, transitioning
    WAITING_TO_RESUME = auto()  # Timer scheduled, waiting for resume time
    RESUMED = auto()            # Just sent "continue", transitioning back
    STOPPED = auto()            # Manually stopped by user


@dataclass
class Watcher:
    """
    Manages the lifecycle of watching a single Claude Code session.

    The main app calls `poll()` on each tick, and `fire_resume()` when
    the scheduled timer goes off.
    """
    target: Target
    state: WatcherState = WatcherState.WATCHING
    reset_info: Optional[ResetInfo] = None
    resume_count: int = 0                    # How many times we've auto-resumed
    last_error: Optional[str] = None
    _last_content_hash: Optional[int] = None  # Avoid re-processing identical content

    @property
    def status_text(self) -> str:
        """Human-readable status for display in the menu."""
        if self.state == WatcherState.WATCHING:
            return "👀 Watching"
        elif self.state == WatcherState.LIMIT_DETECTED:
            return "⚡ Limit detected"
        elif self.state == WatcherState.WAITING_TO_RESUME:
            if self.reset_info:
                t = self.reset_info.resume_time.strftime("%-I:%M %p")
                return f"⏳ Resumes at {t}"
            return "⏳ Waiting..."
        elif self.state == WatcherState.RESUMED:
            return "✅ Resumed"
        elif self.state == WatcherState.STOPPED:
            return "⏹ Stopped"
        return "?"

    def poll(self) -> Optional[ResetInfo]:
        """
        Poll the target for the session limit message.

        Called by the main app on each tick (every 15s).
        Returns ResetInfo if a session limit was just detected, None otherwise.
        Only runs when in WATCHING state.
        """
        if self.state != WatcherState.WATCHING:
            return None

        self.last_error = None

        try:
            content = read_content(self.target)
        except Exception as e:
            self.last_error = f"Read error: {e}"
            logger.error("Failed to read %s: %s", self.target.key, e)
            return None

        if content is None:
            # What "no content" means depends on the backend. A Terminal tab that reads
            # back nothing has been closed, which is worth surfacing. Conductor returns
            # None on almost every poll — it only reports standing limit notices — so
            # treating that as an error would flag every healthy session as broken.
            if self.target.kind is TargetKind.TERMINAL:
                self.last_error = "Terminal not found (closed?)"
                logger.warning("Terminal %s returned no content", self.target.ref)
            self._last_content_hash = None
            return None

        # Skip if content hasn't changed
        content_hash = hash(content)
        if content_hash == self._last_content_hash:
            return None
        self._last_content_hash = content_hash

        # Check for session limit message
        reset_info = detect_session_limit(content)
        if reset_info is None:
            return None

        # Detected!
        logger.info(
            "Session limit detected on %s! Resets at %s, will resume at %s",
            self.target.key,
            reset_info.reset_time.strftime("%I:%M %p"),
            reset_info.resume_time.strftime("%I:%M %p"),
        )
        self.reset_info = reset_info
        self.state = WatcherState.LIMIT_DETECTED

        # Immediately transition to WAITING
        self.state = WatcherState.WAITING_TO_RESUME

        return reset_info

    def fire_resume(self) -> bool:
        """
        Send "continue" to the watched session.

        Called by the main app when the scheduled resume timer fires.
        Returns True if the text was sent successfully.
        """
        if self.state != WatcherState.WAITING_TO_RESUME:
            logger.warning(
                "fire_resume called on %s in unexpected state %s",
                self.target.key,
                self.state,
            )
            return False

        logger.info("Sending 'continue' to %s", self.target.key)

        success, error = send_text_detailed(self.target, "continue")

        if success:
            self.resume_count += 1
            self.state = WatcherState.RESUMED
            logger.info("Successfully resumed session on %s (count: %d)",
                        self.target.key, self.resume_count)

            # Transition back to WATCHING for the next cycle
            self.state = WatcherState.WATCHING
            self.reset_info = None
            self._last_content_hash = None  # Reset so we re-read fresh content
        else:
            self.last_error = error or "Failed to send 'continue'"
            logger.error("Failed to send 'continue' to %s: %s",
                         self.target.key, self.last_error)

        return success

    def stop(self):
        """Stop watching this target."""
        self.state = WatcherState.STOPPED
        self.reset_info = None
        logger.info("Stopped watching %s", self.target.key)

    def seconds_until_resume(self) -> Optional[float]:
        """
        If waiting to resume, return the number of seconds until resume time.
        Returns None if not in WAITING state or if resume time has passed.
        """
        if self.state != WatcherState.WAITING_TO_RESUME or not self.reset_info:
            return None

        delta = (self.reset_info.resume_time - datetime.now()).total_seconds()
        return max(0, delta)
