"""
conductor.py — Conductor.app (conductor.build) interaction layer.

Conductor is a Tauri app that runs coding agents in parallel git worktrees. Unlike
Terminal.app it does *not* run Claude Code in a pty — it spawns the CLI headlessly
(`claude --output-format stream-json --input-format stream-json`) and renders the
stream itself. There is no terminal buffer to scrape, so reading and writing work
differently from `terminal.py`:

- **Read** comes from Conductor's own SQLite store (read-only). Each session's raw
  stream-json messages are kept in `session_messages`, which includes the notice the
  agent emits when it hits its limit: a synthetic assistant message from Claude Code,
  an error envelope from Codex.
- **Write** goes through the UI via Accessibility, because the only supported way to
  put a message into a session is Conductor's composer. See `send_text_detailed`.

Claude Code and Codex sessions are supported. Conductor can also drive Cursor and
OpenCode, whose limits aren't reported this way — `sessions.agent_type` filters those out.
"""

import json
import logging
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("claude_auto_resume.conductor")

CONDUCTOR_DB = (
    Path.home() / "Library" / "Application Support" / "com.conductor.app" / "conductor.db"
)

# Conductor's process/bundle identity, used for the Accessibility send path.
CONDUCTOR_PROCESS = "conductor"
CONDUCTOR_BUNDLE_ID = "com.conductor.app"

# `agent_type` values we watch, and how to name them. Cursor/OpenCode sessions use
# other values and are deliberately not supported.
AGENT_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
}

# Claude Code stamps messages it generates itself (rather than the model) with this
# model name. The session limit notice is one of them, and gating on it is what
# separates a real limit event from an agent that merely *mentions* the limit text
# in a tool result or its own prose.
SYNTHETIC_MODEL = "<synthetic>"

# Codex has no synthetic assistant message. Its usage limit arrives as a turn error,
# which Conductor stores as `{"type": "error", "content": "You've hit your usage
# limit…", "willRetry": false, "errorInfo": "usageLimitExceeded"}`. Error envelopes
# come from the harness, never the model, so they serve the same gating purpose.
ERROR_ENVELOPE = "error"

# How many of a session's most recent messages to scan for the limit notice.
_SCAN_DEPTH = 40


@dataclass
class ConductorSession:
    """A Conductor workspace and the agent session currently open in it."""
    workspace_id: str
    session_id: str
    workspace_name: str   # Display name for the workspace (falls back to directory)
    session_title: str    # e.g. "Strip iTerm2 Support"
    status: str           # Conductor's session status, e.g. "idle" / "working" / "error"
    branch: str           # Git branch, which Conductor shows in its window header
    agent_type: str = "claude"  # A key of AGENT_LABELS

    @property
    def agent_label(self) -> str:
        return AGENT_LABELS.get(self.agent_type, self.agent_type)


def is_available() -> bool:
    """True if Conductor appears to be installed on this machine."""
    return CONDUCTOR_DB.exists()


def _connect() -> Optional[sqlite3.Connection]:
    """
    Open Conductor's database read-only.

    Read-only (`mode=ro`) matters for two reasons: we must never take a write lock on
    a database another app owns, and it makes accidental writes impossible. Conductor
    runs in WAL mode, so a read-only connection still observes committed changes made
    while it is running.
    """
    if not CONDUCTOR_DB.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{CONDUCTOR_DB}?mode=ro", uri=True, timeout=5.0)
        conn.execute("PRAGMA busy_timeout = 3000")
        return conn
    except sqlite3.Error as e:
        logger.error("Could not open Conductor's database: %s", e)
        return None


def list_sessions() -> list[ConductorSession]:
    """
    List each live Conductor workspace's currently-open Claude Code or Codex session.

    Conductor allows several sessions per workspace, but only one is open and visible
    at a time — `workspaces.active_session_id`. That's the one we watch, so a workspace
    contributes at most one entry.
    """
    conn = _connect()
    if conn is None:
        return []

    agent_types = tuple(AGENT_LABELS)
    try:
        rows = conn.execute(
            f"""
            SELECT w.id,
                   s.id,
                   COALESCE(NULLIF(w.workspace_name, ''), NULLIF(w.directory_name, ''), w.id),
                   COALESCE(NULLIF(s.title, ''), 'Untitled'),
                   COALESCE(s.status, 'idle'),
                   COALESCE(w.branch, ''),
                   s.agent_type
              FROM workspaces w
              JOIN sessions s ON s.id = w.active_session_id
             WHERE COALESCE(w.state, 'active') != 'archived'
               AND s.agent_type IN ({", ".join("?" for _ in agent_types)})
               AND COALESCE(s.is_hidden, 0) = 0
             ORDER BY w.updated_at DESC
            """,
            agent_types,
        ).fetchall()
    except sqlite3.Error as e:
        logger.error("Failed to list Conductor sessions: %s", e)
        return []
    finally:
        conn.close()

    return [
        ConductorSession(
            workspace_id=r[0],
            session_id=r[1],
            workspace_name=r[2],
            session_title=r[3],
            status=r[4],
            branch=r[5],
            agent_type=r[6],
        )
        for r in rows
    ]


def get_session(workspace_id: str) -> Optional[ConductorSession]:
    """Re-resolve a workspace's currently-open agent session, or None if it's gone."""
    for session in list_sessions():
        if session.workspace_id == workspace_id:
            return session
    return None


def _synthetic_limit_text(raw: str) -> Optional[str]:
    """
    Return the text of a synthetic Claude Code notice, or None for any other message.

    `session_messages.content` holds one raw stream-json message. We want assistant
    messages whose model is `<synthetic>` — Claude Code's marker for text it generated
    itself, which is how the session limit notice arrives.
    """
    if SYNTHETIC_MODEL not in raw:
        # Cheap reject before paying for a JSON parse; the vast majority of rows.
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None

    if data.get("type") != "assistant":
        return None
    message = data.get("message") or {}
    if message.get("model") != SYNTHETIC_MODEL:
        return None

    parts = [
        block.get("text", "")
        for block in (message.get("content") or [])
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    text = "\n".join(p for p in parts if p)
    return text or None


def _error_envelope_text(raw: str) -> Optional[str]:
    """
    Return the text of an agent error envelope, or None for any other message.

    This is how Codex's usage limit notice arrives. Errors the agent is about to retry
    on its own ("Reconnecting... 2/5") are skipped: they don't leave the session stuck.
    """
    if ERROR_ENVELOPE not in raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None

    if not isinstance(data, dict) or data.get("type") != ERROR_ENVELOPE:
        return None
    if data.get("willRetry"):
        return None
    content = data.get("content")
    return content if isinstance(content, str) and content else None


def _limit_notice_text(raw: str) -> Optional[str]:
    """Text of a harness-generated notice (Claude Code or Codex), or None."""
    return _synthetic_limit_text(raw) or _error_envelope_text(raw)


def read_content(workspace_id: str) -> Optional[str]:
    """
    Return text to scan for the limit notice, or None if there's nothing to report.

    This is the Conductor counterpart to `terminal.read_content`, but it returns *only*
    notices the agent's harness generated — Claude Code's synthetic messages, Codex's
    error envelopes — rather than everything on screen. Returning the whole transcript
    would be actively wrong: an agent that reads or writes about the limit message puts
    that exact phrase into its own transcript, and we would resume a session that was
    never limited.

    Returns None when the session's most recent activity is not a standing limit — either
    no such notice is present, or the user has already sent a message after it, which
    means the session has moved on.
    """
    session = get_session(workspace_id)
    if session is None:
        return None

    # A session that is actively producing output is not sitting at a limit. This also
    # avoids re-detecting the notice that is still in the transcript from a limit we
    # already resumed past.
    if session.status == "working":
        return None

    conn = _connect()
    if conn is None:
        return None

    try:
        rows = conn.execute(
            """
            SELECT role, content
              FROM session_messages
             WHERE session_id = ?
               AND cancelled_at IS NULL
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (session.session_id, _SCAN_DEPTH),
        ).fetchall()
    except sqlite3.Error as e:
        logger.error("Failed to read Conductor session %s: %s", session.session_id, e)
        return None
    finally:
        conn.close()

    # Newest first. A user message *after* the notice means the session was already
    # resumed — by us on an earlier cycle, or by the user by hand — so stop there.
    for role, content in rows:
        if role == "user":
            return None
        text = _limit_notice_text(content or "")
        if text:
            return text

    return None


# ─── Sending ───────────────────────────────────────────────────────────

def _run_applescript(script: str, timeout: int = 15) -> tuple[bool, str, str]:
    """Run an AppleScript, returning (ok, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-"],
            input=script,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", "osascript timed out"
    except (FileNotFoundError, OSError) as e:
        return False, "", str(e)


def _escape_applescript(text: str) -> str:
    """Escape a Python string for embedding in an AppleScript string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def has_accessibility_permission() -> bool:
    """
    True if this process may inspect and drive other apps' UIs.

    Terminal.app needs only Automation permission, which macOS prompts for on demand.
    Conductor exposes no scripting interface, so its composer can only be reached
    through Accessibility — a permission the user must grant by hand in System Settings.

    The probe deliberately reads a UI element rather than something like
    `count of processes`, which succeeds with Automation permission alone and would
    report a false pass.
    """
    ok, _, err = _run_applescript(
        'tell application "System Events" to tell process "Finder" to return '
        '(count of windows) as text',
        timeout=8,
    )
    if ok:
        return True
    if "assistive access" in err.lower():
        return False
    # Some other failure (Finder busy, timeout). Don't claim the permission is missing.
    logger.warning("Could not determine Accessibility permission: %s", err)
    return True


def _latest_user_message_at(session_id: str) -> Optional[str]:
    """Timestamp of the most recent message the user sent to a session, if any."""
    conn = _connect()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT MAX(created_at) FROM session_messages WHERE session_id = ? AND role = 'user'",
            (session_id,),
        ).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


# The embedded terminal pane's input is also an AXTextArea. Never type into it.
_TERMINAL_INPUT_DESCRIPTION = "Terminal input"

# How long to wait for Conductor to record our message before calling the send failed.
_SEND_CONFIRM_TIMEOUT = 6.0
_SEND_CONFIRM_INTERVAL = 0.5


def _read_focused_element(retries: int = 4, delay: float = 0.35) -> tuple[bool, str, str, str]:
    """
    Read Conductor's focused UI element as (found, role, description, value).

    Retries because the reference goes transiently invalid: Conductor's composer is a
    React component, and typing into it re-renders the node, so an immediate re-read
    can return `missing value` for an element that is still perfectly focused.
    """
    for attempt in range(retries):
        ok, out, err = _run_applescript(f'''
        tell application "System Events"
            tell process "{CONDUCTOR_PROCESS}"
                set el to value of attribute "AXFocusedUIElement"
                if el is missing value then return "none|-|-"
                set elRole to ""
                try
                    set elRole to role of el as text
                end try
                set elDesc to ""
                try
                    set elDesc to (description of el) as text
                end try
                set elValue to ""
                try
                    set elValue to (value of el) as text
                end try
                return elRole & "|" & elDesc & "|" & elValue
            end tell
        end tell
        ''', timeout=15)

        if ok:
            parts = out.split("|", 2)
            if len(parts) == 3 and parts[0] != "none":
                return True, parts[0], parts[1], parts[2]
        else:
            logger.debug("Reading Conductor's focused element failed: %s", err)

        if attempt < retries - 1:
            time.sleep(delay)

    return False, "", "", ""


def _focused_composer_state() -> tuple[bool, str]:
    """
    Decide whether Conductor's focused element is a composer we may type into.

    Returns (is_usable_composer, detail). `detail` is the element's current text when
    usable, and a human-readable reason when not.

    Reading `AXFocusedUIElement` is one Accessibility call. The alternative — walking
    the window looking for the composer — is both far too slow to run in a background
    app (Conductor's WKWebView tree takes over a minute to traverse) and unsafe: the PR
    description field and the terminal pane are text areas too, so a "first text area
    wins" rule types into whichever the current layout happens to surface first.
    """
    found, role, description, value = _read_focused_element()
    if not found:
        return False, "no focused element — is a workspace open?"
    if role != "AXTextArea":
        return False, f"focus is on {role or 'an unknown element'}, not the message composer"
    if description.strip() == _TERMINAL_INPUT_DESCRIPTION:
        return False, "focus is on Conductor's terminal pane, not the message composer"
    if value.strip():
        # Could be a half-written message, or a different text area such as the PR
        # description. Either way, overwriting it would destroy the user's text.
        return False, "the composer already has unsent text in it"
    return True, value


# Conductor names the visible workspace's branch in a band across the top of its
# window. Measured window-relative, since the window can be anywhere on screen.
_HEADER_BAND_HEIGHT = 80

# Depth limit for the header scan. The branch label sits six levels down; going deeper
# makes the traversal explode without finding anything more useful.
_HEADER_SCAN_DEPTH = 6


def _has_open_window() -> bool:
    """
    Does Conductor have a window we can drive?

    Conductor keeps running with no windows after its last one is closed, and a closed
    window cannot be reopened programmatically — neither a reopen event (`open -a`) nor
    clicking the Dock icon brings it back. Checking up front turns that into an accurate
    message, instead of surfacing further down as a confusing "showing a different
    workspace", where every UI read fails for a reason unrelated to workspaces.
    """
    ok, out, _ = _run_applescript(
        f'tell application "System Events" to tell process "{CONDUCTOR_PROCESS}" '
        f'to return (count of windows) as text',
        timeout=10,
    )
    return ok and out.isdigit() and int(out) > 0


def _window_header_texts() -> list[str]:
    """
    Return the labels Conductor draws across the top of its window.

    This is how we tell which workspace Conductor has open. It renders one workspace at
    a time and its window title is just "Conductor", but the header names the
    workspace's git branch, which maps to `workspaces.branch`.
    """
    ok, out, err = _run_applescript(f'''
    on headerTexts(el, depth, windowTop, acc)
        if depth > {_HEADER_SCAN_DEPTH} then return acc
        tell application "System Events"
            try
                set kids to UI elements of el
            on error
                return acc
            end try
            repeat with c in kids
                set elRole to "?"
                try
                    set elRole to role of c as text
                end try
                if elRole is "AXStaticText" then
                    try
                        set po to position of c
                        if ((item 2 of po) - windowTop) < {_HEADER_BAND_HEIGHT} then
                            set t to (value of c) as text
                            if t is not "" then set end of acc to t
                        end if
                    end try
                end if
                set acc to my headerTexts(c, depth + 1, windowTop, acc)
            end repeat
        end tell
        return acc
    end headerTexts

    tell application "System Events"
        tell process "{CONDUCTOR_PROCESS}"
            if not (exists front window) then return ""
            set w to front window
            set windowPos to position of w
            set windowTop to item 2 of windowPos
            set AppleScript's text item delimiters to "|"
            return (my headerTexts(w, 1, windowTop, {{}})) as text
        end tell
    end tell
    ''', timeout=25)

    if not ok:
        logger.warning("Could not read Conductor's header: %s", err)
        return []
    return [t.strip() for t in out.split("|") if t.strip()]


def _workspace_is_displayed(session: ConductorSession) -> Optional[bool]:
    """
    Is `session`'s workspace the one Conductor currently has open?

    This asks which workspace Conductor has *selected*, not whether Conductor is
    frontmost or unobscured — the header reads fine while the app sits behind other
    windows, which is exactly what we want. Whether the app is in front is handled
    separately, by activating it before sending.

    Returns None when it can't be determined — a workspace still being created has no
    branch yet, and there's nothing to match against.
    """
    if not session.branch:
        return None
    return session.branch in _window_header_texts()


def _switch_to_workspace(session: ConductorSession) -> bool:
    """
    Bring `session`'s workspace on screen using Conductor's ⌘K search palette.

    Type the workspace name, take the first match, and commit. Return alone doesn't
    commit — the result has to be selected with Down first. Success is confirmed by
    re-reading the header rather than assumed, so a fuzzy match landing on the wrong
    workspace is caught rather than typed into.
    """
    logger.info("Switching Conductor to workspace %s", session.workspace_name)
    ok, _, err = _run_applescript(f'''
    tell application "System Events"
        tell process "{CONDUCTOR_PROCESS}"
            key code 40 using command down -- Cmd+K
            delay 1.2
            keystroke "{_escape_applescript(session.workspace_name)}"
            delay 1.5
            key code 125 -- Down, to select the first result
            delay 0.5
            key code 36 -- Return
        end tell
    end tell
    ''', timeout=30)
    if not ok:
        logger.error("Workspace switch failed: %s", err)
        return False

    time.sleep(1.5)
    return _workspace_is_displayed(session) is True


def _dismiss_palette() -> None:
    """Close the ⌘K palette if a failed switch left it open."""
    _run_applescript(
        f'tell application "System Events" to tell process "{CONDUCTOR_PROCESS}" '
        f'to key code 53',
        timeout=10,
    )


def send_text_detailed(workspace_id: str, text: str) -> tuple[bool, Optional[str]]:
    """
    Send `text` to the Conductor session open in `workspace_id`.

    Conductor has no AppleScript dictionary and no local API, so the only supported way
    to put a message into a session is its own composer, reached over Accessibility.
    Conductor also renders one workspace at a time, so getting this right means making
    sure we're looking at the right one before typing:

    1. Bring Conductor to the front.
    2. Check the window header, which names the visible workspace's branch.
    3. If it's the wrong workspace, switch with the ⌘K palette and re-check.
    4. Confirm the focused element really is an empty composer.
    5. Type, read back what landed, and only then press Return.
    6. Confirm against Conductor's database that *this* session received the message.

    Steps 2-3 mean the watched workspace doesn't have to already be on screen, and
    step 6 is the backstop: a message that went somewhere else is reported as a
    failure rather than a silent success.

    Returns (success, error message).
    """
    session = get_session(workspace_id)
    if session is None:
        return False, "Conductor session not found (workspace closed or switched agents?)"

    if not has_accessibility_permission():
        return False, (
            "Accessibility permission is missing — grant it in System Settings → "
            "Privacy & Security → Accessibility"
        )

    before = _latest_user_message_at(session.session_id)

    ok, _, err = _run_applescript(f'tell application id "{CONDUCTOR_BUNDLE_ID}" to activate')
    if not ok:
        return False, f"could not activate Conductor: {err}"

    # Let the window come forward and the composer take focus before inspecting it.
    time.sleep(0.8)

    if not _has_open_window():
        return False, (
            "Conductor has no open window — reopen it and leave it open, since a closed "
            "window can't be restored automatically"
        )

    displayed = _workspace_is_displayed(session)
    if displayed is False:
        if not _switch_to_workspace(session):
            _dismiss_palette()
            return False, (
                f"Conductor is showing a different workspace and could not switch to "
                f"{session.workspace_name}"
            )
    elif displayed is None:
        # No branch to match on yet (a workspace still being set up). Type anyway —
        # the database confirmation below still catches a message that went elsewhere.
        logger.warning(
            "Cannot confirm workspace %s is visible; relying on post-send confirmation",
            session.workspace_name,
        )

    usable, detail = _focused_composer_state()
    if not usable:
        return False, (
            f"Could not reach Conductor's message composer — {detail}. Make sure the "
            f"watched workspace ({session.workspace_name}) is the one on screen."
        )

    # Type as real key events rather than assigning to the element's value. Conductor's
    # composer is React-controlled: writing the value straight through Accessibility
    # updates the DOM without necessarily firing React's change handler, which leaves
    # the component's state empty and submits a blank message. Keystrokes go through
    # the same path as the user's own typing, and focus was just confirmed to be the
    # composer, so they cannot land anywhere else.
    ok, _, err = _run_applescript(f'''
    tell application "System Events"
        tell process "{CONDUCTOR_PROCESS}"
            keystroke "{_escape_applescript(text)}"
        end tell
    end tell
    ''', timeout=20)
    if not ok:
        logger.error("Typing into Conductor failed for %s: %s", workspace_id, err)
        return False, f"Accessibility send failed: {err}"

    # Confirm the text actually landed before pressing Return on top of it.
    found, _, _, typed = _read_focused_element()
    if not found:
        return False, "Conductor's composer lost focus before the message could be sent"
    if text not in typed:
        return False, f"Conductor's composer did not accept the text (reads {typed!r})"

    ok, _, err = _run_applescript(f'''
    tell application "System Events"
        tell process "{CONDUCTOR_PROCESS}"
            key code 36 -- Return
        end tell
    end tell
    ''', timeout=15)
    if not ok:
        return False, f"Text was typed but the Return failed: {err}"

    # Confirm Conductor recorded the message against the session we watched, rather
    # than a different workspace that happened to be on screen.
    deadline = time.monotonic() + _SEND_CONFIRM_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(_SEND_CONFIRM_INTERVAL)
        if _latest_user_message_at(session.session_id) != before:
            logger.info("Sent %r to Conductor workspace %s", text, workspace_id)
            return True, None

    return False, (
        f"Typed {text!r} into Conductor but the {session.workspace_name} session never "
        f"received it — a different workspace may have been on screen"
    )


def send_text(workspace_id: str, text: str) -> bool:
    """Send text + Return to a Conductor session. Returns True on success."""
    ok, _ = send_text_detailed(workspace_id, text)
    return ok
