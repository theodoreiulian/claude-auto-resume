"""
targets.py — A single watchable thing, and dispatch to the backend that owns it.

Claude Code runs in places that have nothing in common mechanically: a Terminal.app
tab is a pty read over AppleScript, a Conductor workspace is a SQLite row plus a
WKWebView composer. `Target` is the thin seam between them, so `watcher.py` and
`app.py` never branch on where a session lives.

The seam is deliberately narrow — enumerate, read, send:

    list_targets()               -> every watchable Claude Code session
    read_content(target)         -> text to scan for the session limit notice
    send_text_detailed(target, ) -> put a message into that session
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from . import conductor, terminal

logger = logging.getLogger("claude_auto_resume.targets")


class TargetKind(str, Enum):
    """Where a watched Claude Code session lives."""
    TERMINAL = "terminal"
    CONDUCTOR = "conductor"


@dataclass(frozen=True)
class Target:
    """
    One watchable Claude Code session.

    `ref` is whatever the owning backend needs to find it again: a TTY path for
    Terminal.app, a workspace id for Conductor. Nothing outside the backend
    interprets it.
    """
    kind: TargetKind
    ref: str
    name: str      # Primary label, e.g. the tab title or workspace name
    detail: str    # Secondary label, e.g. running processes or session title

    @property
    def key(self) -> str:
        """Stable identity for this target, unique across backends."""
        return f"{self.kind.value}:{self.ref}"

    @property
    def source_label(self) -> str:
        """Human name of the app hosting this session."""
        return "Terminal" if self.kind is TargetKind.TERMINAL else "Conductor"

    @property
    def menu_label(self) -> str:
        """Label shown in the menu bar's target lists."""
        if self.kind is TargetKind.TERMINAL:
            return f"{self.name} ({self.ref.replace('/dev/', '')})"
        return f"{self.name} — {self.detail}"


def list_targets() -> list[Target]:
    """
    List every watchable Claude Code session across all supported hosts.

    A failure in one backend must not hide the other's sessions, so each is
    collected independently.
    """
    targets: list[Target] = []

    try:
        for tab in terminal.list_terminals():
            targets.append(Target(
                kind=TargetKind.TERMINAL,
                ref=tab.tty,
                name=tab.name,
                detail=tab.processes,
            ))
    except Exception as e:
        logger.error("Failed to list Terminal.app tabs: %s", e)

    try:
        for session in conductor.list_sessions():
            targets.append(Target(
                kind=TargetKind.CONDUCTOR,
                ref=session.workspace_id,
                name=session.workspace_name,
                detail=session.session_title,
            ))
    except Exception as e:
        logger.error("Failed to list Conductor sessions: %s", e)

    return targets


def read_content(target: Target) -> Optional[str]:
    """
    Return text to scan for the session limit notice, or None if unavailable.

    What "content" means is backend-specific and intentionally so. Terminal.app
    returns everything on the visible screen and relies on the detector's regex to
    find the notice. Conductor returns only messages Claude Code generated itself,
    because its transcripts routinely quote the limit text without being limited.
    """
    if target.kind is TargetKind.TERMINAL:
        return terminal.read_content(target.ref)
    return conductor.read_content(target.ref)


def send_text_detailed(target: Target, text: str) -> tuple[bool, Optional[str]]:
    """Send `text` + Return to a target. Returns (success, error message)."""
    if target.kind is TargetKind.TERMINAL:
        return terminal.send_text_detailed(target.ref, text)
    return conductor.send_text_detailed(target.ref, text)
