"""
Tests for the Codex desktop-app backend.

Reading is exercised against throwaway databases with the same shape as Codex's
`state_*.sqlite` and `thread_history_*.sqlite`. Sending is exercised against a fake IPC
router on a temporary Unix socket that speaks the app's framing — 4-byte little-endian
length, then JSON — and records what it was asked to do.
"""

import json
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_auto_resume import codex_app
from claude_auto_resume.detector import detect_session_limit
from tests.support import CODEX_LIMIT_TEXT

STATE_SCHEMA = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, cwd TEXT NOT NULL,
    title TEXT NOT NULL, name TEXT, originator TEXT, source TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0, preview TEXT NOT NULL DEFAULT '',
    recency_at_ms INTEGER NOT NULL DEFAULT 0
);
"""

HISTORY_SCHEMA = """
CREATE TABLE thread_turns (
    thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, rollout_ordinal INTEGER NOT NULL,
    status TEXT NOT NULL, error_json TEXT, PRIMARY KEY (thread_id, turn_id)
);
"""


def usage_limit_error(text: str = CODEX_LIMIT_TEXT) -> str:
    """A failed turn's error as Codex records it — verbatim shape from a real limit."""
    return json.dumps({"message": text, "codexErrorInfo": "usageLimitExceeded",
                       "additionalDetails": None, "misalignment": None})


class CodexAppDBTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        for name, schema in (("state_5.sqlite", STATE_SCHEMA),
                             ("thread_history_1.sqlite", HISTORY_SCHEMA)):
            conn = sqlite3.connect(self.home / name)
            conn.executescript(schema)
            conn.commit()
            conn.close()
        self._patch = mock.patch.object(codex_app, "CODEX_HOME", self.home)
        self._patch.start()
        codex_app._originator_cache.clear()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    # ── helpers ────────────────────────────────────────────────────────
    def _write(self, db, sql, params=()):
        conn = sqlite3.connect(self.home / db)
        conn.execute(sql, params)
        conn.commit()
        conn.close()

    def add_thread(self, thread_id="t1", originator="Codex Desktop", title="Fix the build",
                   name=None, archived=0, source="vscode", recency=1, in_column=True):
        rollout = self.home / f"rollout-{thread_id}.jsonl"
        meta = {"type": "session_meta", "payload": {
            "id": thread_id, "cwd": "/Users/me/proj", "originator": originator,
            "base_instructions": {"text": "x" * 50000}}}
        rollout.write_text(json.dumps(meta) + "\n")
        self._write(
            "state_5.sqlite",
            "INSERT INTO threads (id, rollout_path, cwd, title, name, originator, source,"
            " archived, preview, recency_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (thread_id, str(rollout), "/Users/me/proj", title, name,
             originator if in_column else None, source, archived, title, recency),
        )

    def add_turn(self, thread_id, ordinal, status, error_json=None):
        self._write(
            "thread_history_1.sqlite",
            "INSERT INTO thread_turns (thread_id, turn_id, rollout_ordinal, status, error_json)"
            " VALUES (?,?,?,?,?)",
            (thread_id, f"turn{ordinal}", ordinal, status, error_json),
        )

    # ── list_threads ───────────────────────────────────────────────────
    def test_lists_desktop_thread(self):
        self.add_thread()
        threads = codex_app.list_threads()
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0].title, "Fix the build")
        self.assertEqual(threads[0].project, "proj")

    def test_reads_originator_from_rollout_when_column_empty(self):
        """Older rows leave `threads.originator` empty; the rollout's first line has it."""
        self.add_thread(in_column=False)
        self.assertEqual(len(codex_app.list_threads()), 1)

    def test_excludes_threads_started_elsewhere(self):
        """Conductor and CLI sessions share the store but belong to other backends."""
        self.add_thread("conductor", originator="codex_sdk_ts")
        self.add_thread("cli", originator="codex_exec", in_column=False)
        self.assertEqual(codex_app.list_threads(), [])

    def test_excludes_archived_and_subagent_threads(self):
        self.add_thread("archived", archived=1)
        self.add_thread("sub", source='{"subagent":{"thread_spawn":{"depth":1}}}')
        self.assertEqual(codex_app.list_threads(), [])

    def test_prefers_custom_name_and_orders_by_recency(self):
        self.add_thread("old", title="Old", recency=1)
        self.add_thread("new", title="Generated", name="Renamed", recency=2)
        self.assertEqual([t.title for t in codex_app.list_threads()], ["Renamed", "Old"])

    def test_picks_newest_schema_version(self):
        """Codex leaves superseded databases behind; only the highest suffix is live."""
        (self.home / "state_4.sqlite").write_bytes(b"not a database")
        self.add_thread()
        self.assertEqual(len(codex_app.list_threads()), 1)

    # ── read_content ───────────────────────────────────────────────────
    def test_standing_usage_limit_is_reported(self):
        self.add_turn("t1", 1, "completed")
        self.add_turn("t1", 2, "failed", usage_limit_error())
        content = codex_app.read_content("t1")
        self.assertEqual(content, CODEX_LIMIT_TEXT)
        self.assertIsNotNone(detect_session_limit(content))

    def test_limit_followed_by_a_new_turn_is_not_reported(self):
        """A turn after the limit means it was already resumed — by us or by hand."""
        self.add_turn("t1", 1, "failed", usage_limit_error())
        for status in ("inProgress", "completed"):
            with self.subTest(status=status):
                self._write("thread_history_1.sqlite", "DELETE FROM thread_turns WHERE rollout_ordinal = 2")
                self.add_turn("t1", 2, status)
                self.assertIsNone(codex_app.read_content("t1"))

    def test_other_failures_are_not_reported(self):
        self.add_turn("t1", 1, "failed", json.dumps(
            {"message": "stream disconnected", "codexErrorInfo": "responseStreamDisconnected"}))
        self.assertIsNone(codex_app.read_content("t1"))
        self.add_turn("t2", 1, "interrupted")
        self.assertIsNone(codex_app.read_content("t2"))

    def test_message_quoting_the_limit_in_a_completed_turn_is_ignored(self):
        """Only the turn's error counts, so discussing the notice can't trigger a resume."""
        self.add_turn("t1", 1, "completed", None)
        self.assertIsNone(codex_app.read_content("t1"))

    def test_missing_databases_are_not_an_error(self):
        for path in self.home.glob("*.sqlite"):
            path.unlink()
        self.assertEqual(codex_app.list_threads(), [])
        self.assertIsNone(codex_app.read_content("t1"))
        self.assertFalse(codex_app.is_available())


# ─── Sending ───────────────────────────────────────────────────────────

def _frame(message: dict) -> bytes:
    data = json.dumps(message).encode()
    return struct.pack("<I", len(data)) + data


class FakeRouter:
    """
    Stands in for the app's IPC router plus the window that owns the thread.

    `owned` controls whether the thread is loaded; `on_open` simulates the deep link
    loading it. Every request received is recorded in `requests`.
    """

    OWNER = "owner-window"

    def __init__(self, path: Path, owned=True, start_turn_error=None, turn_id="turn-new"):
        self.owned = owned
        self.start_turn_error = start_turn_error
        self.turn_id = turn_id
        self.requests: list[dict] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self):
        self._server.close()

    def _serve(self):
        try:
            conn, _ = self._server.accept()
        except OSError:
            return
        buf = b""
        with conn:
            while True:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while len(buf) >= 4:
                    (n,) = struct.unpack("<I", buf[:4])
                    if len(buf) < 4 + n:
                        break
                    msg, buf = json.loads(buf[4:4 + n]), buf[4 + n:]
                    self.requests.append(msg)
                    conn.sendall(_frame(self._respond(msg)))

    def _respond(self, msg):
        method, rid = msg["method"], msg["requestId"]
        ok = {"type": "response", "requestId": rid, "resultType": "success", "method": method}
        if method == "initialize":
            return {**ok, "handledByClientId": "me", "result": {"clientId": "me"}}
        if method == "thread-owner-discovery":
            if not self.owned:
                return {"type": "response", "requestId": rid, "resultType": "error",
                        "error": "no-client-found"}
            return {**ok, "handledByClientId": self.OWNER,
                    "result": {"supportsUntrustedAppInput": True}}
        if method == "thread-follower-start-turn":
            if self.start_turn_error:
                return {"type": "response", "requestId": rid, "resultType": "error",
                        "error": self.start_turn_error}
            return {**ok, "handledByClientId": self.OWNER, "result": {"result": {"turn": {
                "id": self.turn_id, "status": "inProgress", "error": None}}}}
        return {"type": "response", "requestId": rid, "resultType": "error",
                "error": "no-handler-for-request"}

    def sent(self, method):
        return [r for r in self.requests if r["method"] == method]


class CodexAppSendTest(unittest.TestCase):
    def setUp(self):
        # Unix socket paths are length-limited, so keep the directory short.
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.sock = Path(self._tmp.name) / "ipc.sock"
        patches = [
            mock.patch.object(codex_app, "IPC_SOCKET", self.sock),
            mock.patch.object(codex_app, "_latest_turn_id", return_value="turn-old"),
            mock.patch.object(codex_app, "_frontmost_app", return_value=None),
            mock.patch.object(codex_app.time, "sleep"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def router(self, **kwargs) -> FakeRouter:
        router = FakeRouter(self.sock, **kwargs)
        self.addCleanup(router.close)
        return router

    def test_sends_turn_to_the_owning_window(self):
        router = self.router()
        ok, err = codex_app.send_text_detailed("t1", "continue")
        self.assertTrue(ok, err)

        [start] = router.sent("thread-follower-start-turn")
        self.assertEqual(start["targetClientId"], FakeRouter.OWNER)
        self.assertEqual(start["version"], 2)
        self.assertEqual(start["params"]["conversationId"], "t1")
        request = start["params"]["turnStart"]["request"]
        self.assertEqual(request["threadId"], "t1")
        self.assertEqual(request["input"], [{"type": "text", "text": "continue", "text_elements": []}])

        [discovery] = router.sent("thread-owner-discovery")
        self.assertEqual(discovery["version"], 1)
        self.assertEqual(discovery["params"], {"hostId": "local", "conversationId": "t1"})

    def test_opens_thread_that_is_not_loaded(self):
        router = self.router(owned=False)

        def load(_thread_id):
            router.owned = True
            return True

        with mock.patch.object(codex_app, "_open_thread", side_effect=load) as opened:
            ok, err = codex_app.send_text_detailed("t1", "continue")
        self.assertTrue(ok, err)
        opened.assert_called_once_with("t1")
        self.assertEqual(len(router.sent("thread-follower-start-turn")), 1)

    def test_fails_when_thread_never_loads(self):
        router = self.router(owned=False)
        with mock.patch.object(codex_app, "_open_thread", return_value=True), \
             mock.patch.object(codex_app, "_OPEN_TIMEOUT", 0.05):
            ok, err = codex_app.send_text_detailed("t1", "continue")
        self.assertFalse(ok)
        self.assertIn("didn't open the thread", err)
        self.assertEqual(router.sent("thread-follower-start-turn"), [])

    def test_rejected_turn_is_a_failure(self):
        self.router(start_turn_error="request-version-mismatch")
        ok, err = codex_app.send_text_detailed("t1", "continue")
        self.assertFalse(ok)
        self.assertIn("request-version-mismatch", err)
        self.assertIn("updated", err)

    def test_no_new_turn_is_a_failure(self):
        """An acknowledgement that names the turn we already had isn't a resume."""
        self.router(turn_id="turn-old")
        ok, err = codex_app.send_text_detailed("t1", "continue")
        self.assertFalse(ok)
        self.assertIn("didn't start a turn", err)

    def test_app_not_running(self):
        ok, err = codex_app.send_text_detailed("t1", "continue")
        self.assertFalse(ok)
        self.assertIn("isn't running", err)

    def test_answers_other_clients_discovery_requests(self):
        """We're registered with the router, so it may ask us about other clients' requests."""
        client_side, server_side = socket.socketpair()
        self.addCleanup(client_side.close)
        self.addCleanup(server_side.close)

        client = codex_app._IpcClient.__new__(codex_app._IpcClient)
        client._sock, client._buf, client.client_id = client_side, b"", "me"

        def router():
            buf = b""
            while len(buf) < 4 or len(buf) < 4 + struct.unpack("<I", buf[:4])[0]:
                buf += server_side.recv(65536)
            ours = json.loads(buf[4:])
            server_side.sendall(_frame({"type": "client-discovery-request", "requestId": "x",
                                        "request": {"method": "thread-follower-start-turn"}}))
            buf = b""
            while len(buf) < 4 or len(buf) < 4 + struct.unpack("<I", buf[:4])[0]:
                buf += server_side.recv(65536)
            self.answer = json.loads(buf[4:])
            server_side.sendall(_frame({"type": "response", "requestId": ours["requestId"],
                                        "resultType": "success", "result": {}}))

        t = threading.Thread(target=router, daemon=True)
        t.start()
        reply = client.request("thread-owner-discovery", {}, timeout=5)
        t.join(5)
        self.assertEqual(reply["resultType"], "success")
        self.assertEqual(self.answer, {"type": "client-discovery-response", "requestId": "x",
                                       "response": {"canHandle": False}})


if __name__ == "__main__":
    unittest.main()
