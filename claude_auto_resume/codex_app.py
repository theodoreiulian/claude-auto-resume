"""
codex_app.py — the official Codex desktop app (the ChatGPT app's "Work" mode).

The desktop app doesn't run Codex in a terminal. It drives a bundled `codex app-server`
and renders the thread itself, so reading and writing work differently from both
`terminal.py` and `conductor.py`:

- **Read** comes from Codex's own SQLite stores under `~/.codex` (read-only). The
  app-server projects every thread's turns into `thread_history_*.sqlite`, and a turn
  that hit the usage limit is recorded there as failed, with the notice in its error:

      {"message": "You've hit your usage limit. … try again at 11:54 PM.",
       "codexErrorInfo": "usageLimitExceeded", …}

- **Write** goes through the app's local IPC router (`~/.codex/ipc/ipc.sock`) rather
  than the UI. It's the channel the app's own windows use to hand work to whichever
  window owns a thread: a `thread-follower-start-turn` request starts a turn exactly as
  if the user had typed into that thread's composer. No Accessibility permission and
  no keystrokes. See `send_text_detailed`.

A thread must be loaded in the app to have an owner. When it isn't, the app is asked
to open it with its `codex://threads/<id>` deep link. That briefly brings the app
forward; focus is handed back to whatever was in front once the thread has loaded.

Only threads started in the desktop app are listed. Codex sessions from Conductor and
from the CLI share the same store, but are watched through their own backends.
"""

import json
import logging
import os
import re
import socket
import sqlite3
import struct
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("claude_auto_resume.codex_app")

CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")

# The app's IPC router. Every desktop window registers here as a client.
IPC_SOCKET = CODEX_HOME / "ipc" / "ipc.sock"

# `session_meta.originator` for threads started in the desktop app. Conductor's are
# `codex_sdk_ts`, the CLI's `codex_cli_rs` / `codex_exec`.
DESKTOP_ORIGINATOR = "Codex Desktop"

# How a usage limit is classified in a failed turn's error.
USAGE_LIMIT_ERROR = "usageLimitExceeded"

# How many of the most recently active threads to offer in the menu.
_LIST_LIMIT = 20

# Protocol versions the app expects for the requests we make. It rejects a request
# whose version doesn't match its own, so a bump shows up as a clear error rather
# than a silently dropped message.
_VERSIONS = {
    "initialize": 0,
    "thread-owner-discovery": 1,
    "thread-follower-start-turn": 2,
}

# The router gives each client this long to say whether it owns a thread, so a thread
# nobody owns takes about this long to report as such.
_DISCOVERY_TIMEOUT = 15.0
_START_TURN_TIMEOUT = 30.0
# After asking the app to open a thread, how long to wait for it to take ownership.
_OPEN_TIMEOUT = 25.0
# How long the app may take to bring itself forward after being asked to open a thread.
_FOCUS_SETTLE_TIMEOUT = 4.0

# Frames are a 4-byte little-endian length followed by UTF-8 JSON.
_MAX_FRAME = 256 * 1024 * 1024


@dataclass
class CodexAppThread:
    """A desktop-app Codex thread."""
    thread_id: str
    title: str
    project: str   # Folder name of the thread's working directory


# ─── Reading ───────────────────────────────────────────────────────────

def _versioned_db(stem: str) -> Optional[Path]:
    """
    Path to `~/.codex/<stem>_<N>.sqlite` for the highest N, or None.

    Codex bumps the suffix on incompatible schema changes (`state_5.sqlite`), leaving
    the old file behind, so the newest one is the live one.
    """
    best, best_n = None, -1
    for path in CODEX_HOME.glob(f"{stem}_*.sqlite"):
        m = re.fullmatch(rf"{re.escape(stem)}_(\d+)\.sqlite", path.name)
        if m and int(m.group(1)) > best_n:
            best, best_n = path, int(m.group(1))
    return best


def _connect(stem: str) -> Optional[sqlite3.Connection]:
    """Open one of Codex's databases read-only — never take a lock on another app's store."""
    path = _versioned_db(stem)
    if path is None:
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        conn.execute("PRAGMA busy_timeout = 3000")
        return conn
    except sqlite3.Error as e:
        logger.error("Could not open %s: %s", path, e)
        return None


def is_available() -> bool:
    """True if the Codex desktop app appears to be installed and has been used."""
    return _versioned_db("state") is not None and _versioned_db("thread_history") is not None


def is_running() -> bool:
    """True if the app's IPC router is up — i.e. the desktop app is running."""
    return IPC_SOCKET.is_socket()


_originator_cache: dict[str, Optional[str]] = {}


def _originator(thread_id: str, column: Optional[str], rollout_path: str) -> Optional[str]:
    """
    Which client started a thread.

    Recent Codex versions store it in `threads.originator`, but older rows leave it
    empty, so fall back to the rollout file's first line (`session_meta`). The value
    is fixed at creation, so it's cached.
    """
    if column:
        return column
    if thread_id in _originator_cache:
        return _originator_cache[thread_id]

    originator = None
    try:
        with open(rollout_path, "rb") as f:
            # `originator` comes early in session_meta, well before the (long) base
            # instructions, so the head of the line is enough.
            head = f.read(16384).decode("utf-8", errors="replace")
        m = re.search(r'"originator"\s*:\s*"((?:[^"\\]|\\.)*)"', head)
        if m:
            originator = json.loads(f'"{m.group(1)}"')
    except OSError:
        pass
    _originator_cache[thread_id] = originator
    return originator


def _display_title(name: Optional[str], title: str) -> str:
    """The thread's name as the sidebar shows it: a custom name, else the generated title."""
    text = (name or "").strip() or (title or "").strip() or "Untitled"
    text = text.splitlines()[0]
    return text if len(text) <= 60 else text[:59] + "…"


def list_threads() -> list[CodexAppThread]:
    """List the desktop app's most recently active, non-archived threads."""
    conn = _connect("state")
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT id, rollout_path, cwd, title, name, originator, source
              FROM threads
             WHERE archived = 0
               AND preview <> ''
             ORDER BY recency_at_ms DESC
             LIMIT 200
            """
        ).fetchall()
    except sqlite3.Error as e:
        logger.error("Failed to list Codex threads: %s", e)
        return []
    finally:
        conn.close()

    threads = []
    for thread_id, rollout_path, cwd, title, name, originator, source in rows:
        # Subagents a thread spawned record their parent in `source` as JSON. They
        # aren't threads the user talks to.
        if (source or "").startswith("{"):
            continue
        if _originator(thread_id, originator, rollout_path) != DESKTOP_ORIGINATOR:
            continue
        threads.append(CodexAppThread(
            thread_id=thread_id,
            title=_display_title(name, title),
            project=Path(cwd).name if cwd else "",
        ))
        if len(threads) >= _LIST_LIMIT:
            break
    return threads


def _usage_limit_message(error_json: Optional[str]) -> Optional[str]:
    """The notice text if a turn's error is a usage limit, else None."""
    if not error_json:
        return None
    try:
        error = json.loads(error_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(error, dict) or error.get("codexErrorInfo") != USAGE_LIMIT_ERROR:
        return None
    message = error.get("message")
    return message if isinstance(message, str) and message else None


def _latest_turn(conn: sqlite3.Connection, thread_id: str) -> Optional[tuple]:
    return conn.execute(
        """
        SELECT turn_id, status, error_json, rollout_ordinal
          FROM thread_turns
         WHERE thread_id = ?
         ORDER BY rollout_ordinal DESC
         LIMIT 1
        """,
        (thread_id,),
    ).fetchone()


def read_content(thread_id: str) -> Optional[str]:
    """
    Return the usage limit notice if the thread is standing at one, else None.

    Only the thread's *latest* turn counts. A limit in an earlier turn has been moved
    past — by us or by the user — and a turn that's still running isn't limited. And
    because the notice comes from the turn's error, never from its transcript, a thread
    that merely discusses the limit text can't trigger a resume.
    """
    conn = _connect("thread_history")
    if conn is None:
        return None
    try:
        row = _latest_turn(conn, thread_id)
    except sqlite3.Error as e:
        logger.error("Failed to read Codex thread %s: %s", thread_id, e)
        return None
    finally:
        conn.close()

    if row is None:
        return None
    _turn_id, status, error_json, _ordinal = row
    if status != "failed":
        return None
    return _usage_limit_message(error_json)


def _latest_turn_id(thread_id: str) -> Optional[str]:
    conn = _connect("thread_history")
    if conn is None:
        return None
    try:
        row = _latest_turn(conn, thread_id)
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


# ─── Sending ───────────────────────────────────────────────────────────

class IpcError(Exception):
    """The app's IPC router couldn't be reached or refused a request."""


class _IpcClient:
    """
    A minimal client for the desktop app's IPC router.

    The router forwards each request to whichever registered client says it can handle
    it. We register too, so the router will also ask *us* about other clients' requests;
    those get an immediate "can't handle" so we never hold anyone up.
    """

    def __init__(self, path: Path, timeout: float = 10.0):
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        try:
            self._sock.connect(str(path))
        except OSError as e:
            self._sock.close()
            raise IpcError(f"could not connect to the Codex app ({e})") from e
        self._buf = b""
        self.client_id: Optional[str] = None

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _send(self, message: dict):
        data = json.dumps(message).encode("utf-8")
        self._sock.sendall(struct.pack("<I", len(data)) + data)

    def _recv(self, deadline: float) -> dict:
        while True:
            if len(self._buf) >= 4:
                (length,) = struct.unpack("<I", self._buf[:4])
                if length == 0 or length > _MAX_FRAME:
                    raise IpcError(f"invalid frame length {length}")
                if len(self._buf) >= 4 + length:
                    frame, self._buf = self._buf[4:4 + length], self._buf[4 + length:]
                    return json.loads(frame.decode("utf-8"))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IpcError("timed out waiting for the Codex app")
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(1 << 16)
            except socket.timeout:
                raise IpcError("timed out waiting for the Codex app") from None
            if not chunk:
                raise IpcError("the Codex app closed the connection")
            self._buf += chunk

    def request(self, method: str, params: dict, timeout: float,
                target_client_id: Optional[str] = None) -> dict:
        """Send a request and return the router's response envelope."""
        request_id = str(uuid.uuid4())
        message = {
            "type": "request",
            "requestId": request_id,
            "sourceClientId": self.client_id,
            "version": _VERSIONS.get(method, 0),
            "method": method,
            "params": params,
        }
        if target_client_id:
            message["targetClientId"] = target_client_id
        self._send(message)

        deadline = time.monotonic() + timeout
        while True:
            reply = self._recv(deadline)
            kind = reply.get("type")
            if kind == "response" and reply.get("requestId") == request_id:
                return reply
            if kind == "client-discovery-request":
                self._send({"type": "client-discovery-response",
                            "requestId": reply.get("requestId"),
                            "response": {"canHandle": False}})
            elif kind == "request":
                self._send({"type": "response", "requestId": reply.get("requestId"),
                            "resultType": "error", "error": "no-handler-for-request"})
            # Broadcasts (thread state changes etc.) are none of our business.

    def initialize(self):
        reply = self.request("initialize", {"clientType": "claude-auto-resume"}, timeout=10.0)
        if reply.get("resultType") != "success":
            raise IpcError(f"the Codex app refused the connection: {reply.get('error')}")
        self.client_id = (reply.get("result") or {}).get("clientId")

    def find_owner(self, thread_id: str) -> Optional[str]:
        """Client id of the app window that has this thread loaded, or None."""
        reply = self.request(
            "thread-owner-discovery",
            {"hostId": "local", "conversationId": thread_id},
            timeout=_DISCOVERY_TIMEOUT,
        )
        if reply.get("resultType") == "success":
            return reply.get("handledByClientId")
        if reply.get("error") == "no-client-found":
            return None
        raise IpcError(f"owner lookup failed: {reply.get('error')}")


def _frontmost_app() -> Optional[tuple[int, str]]:
    """(pid, bundle id) of the frontmost application, or None if it can't be told."""
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None or not app.bundleIdentifier():
            return None
        return app.processIdentifier(), str(app.bundleIdentifier())
    except Exception:
        return None


def _restore_focus(previous: Optional[tuple[int, str]]) -> None:
    """
    Hand focus back to `previous` if opening a thread pulled the Codex app forward.

    Goes through LaunchServices (`open -b`): macOS ignores activation requests made
    directly by a background process like this one.
    """
    if previous is None:
        return
    # The app brings itself forward a moment after the deep link, not immediately,
    # so wait to see focus move before moving it back.
    deadline = time.monotonic() + _FOCUS_SETTLE_TIMEOUT
    while True:
        current = _frontmost_app()
        if current is not None and current[0] != previous[0]:
            break
        if time.monotonic() >= deadline:
            return
        time.sleep(0.25)
    try:
        subprocess.run(["/usr/bin/open", "-b", previous[1]],
                       capture_output=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug("Could not restore focus to %s: %s", previous[1], e)


def _open_thread(thread_id: str) -> bool:
    """
    Ask the app to load a thread via its deep link.

    `open -g` asks for it to happen in the background, but the app brings itself
    forward to show the thread anyway; `_find_or_load_owner` hands focus back.
    """
    try:
        result = subprocess.run(
            ["/usr/bin/open", "-g", f"codex://threads/{thread_id}"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("Could not open Codex thread %s: %s", thread_id, e)
        return False


def _find_or_load_owner(client: _IpcClient, thread_id: str) -> tuple[Optional[str], bool]:
    """(owner client id or None, whether the thread had to be opened to get one)."""
    owner = client.find_owner(thread_id)
    if owner:
        return owner, False

    logger.info("Codex thread %s isn't loaded in the app; opening it", thread_id)
    if not _open_thread(thread_id):
        return None, True

    deadline = time.monotonic() + _OPEN_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(1.0)
        owner = client.find_owner(thread_id)
        if owner:
            return owner, True
    return None, True


def send_text_detailed(thread_id: str, text: str) -> tuple[bool, Optional[str]]:
    """
    Send `text` to a desktop-app thread as a new user turn.

    1. Connect to the app's IPC router and register.
    2. Find the app window that owns the thread, opening the thread if none does.
    3. Ask that window to start a turn with `text` — the same request the app makes
       when you type into a thread from a window that doesn't own it.
    4. Confirm the app reported a new turn, not merely an acknowledgement.

    Returns (success, error message).
    """
    if not is_running():
        return False, "The Codex app isn't running"

    before = _latest_turn_id(thread_id)
    previous_front = _frontmost_app()
    opened = False

    try:
        with _IpcClient(IPC_SOCKET) as client:
            client.initialize()

            owner, opened = _find_or_load_owner(client, thread_id)
            if owner is None:
                # A protocol bump also looks like this: the app's windows decline
                # requests of a version they don't speak.
                return False, ("The Codex app didn't open the thread (if the app was "
                               "just updated, this app may need updating too)")

            reply = client.request(
                "thread-follower-start-turn",
                {
                    "conversationId": thread_id,
                    "turnStart": {
                        "request": {
                            "threadId": thread_id,
                            "input": [{"type": "text", "text": text, "text_elements": []}],
                        },
                        "context": {},
                    },
                },
                timeout=_START_TURN_TIMEOUT,
                target_client_id=owner,
            )
    except (IpcError, OSError, ValueError) as e:
        return False, f"Could not reach the Codex app: {e}"
    finally:
        if opened:
            _restore_focus(previous_front)

    if reply.get("resultType") != "success":
        error = reply.get("error") or "unknown error"
        if "version-mismatch" in error:
            error += " (the Codex app was updated; this app needs updating too)"
        return False, f"The Codex app rejected the message: {error}"

    turn = ((reply.get("result") or {}).get("result") or {}).get("turn") or {}
    turn_id = turn.get("id")
    if not turn_id or turn_id == before:
        return False, "The Codex app accepted the message but didn't start a turn"

    logger.info("Started turn %s on Codex thread %s", turn_id, thread_id)
    return True, None
