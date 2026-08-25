"""
Integration tests for reading Conductor's SQLite store.

These build a throwaway database with the same shape as Conductor's and point the
module at it, so the SQL, the agent-type filter and the ordering guards are all
exercised without touching a real Conductor install.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_auto_resume import conductor
from claude_auto_resume.detector import detect_session_limit
from tests.support import LIMIT_TEXT, SCHEMA, assistant_prose, synthetic


class ConductorDBTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "conductor.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        self._orig_db = conductor.CONDUCTOR_DB
        conductor.CONDUCTOR_DB = self.db_path

    def tearDown(self):
        conductor.CONDUCTOR_DB = self._orig_db
        self._tmp.cleanup()

    # ── helpers ────────────────────────────────────────────────────────
    def _write(self, sql, params=()):
        conn = sqlite3.connect(self.db_path)
        conn.execute(sql, params)
        conn.commit()
        conn.close()

    def add_workspace(self, ws="ws1", session="s1", agent_type="claude",
                      status="idle", state="active", name="daegu", hidden=0):
        self._write(
            "INSERT INTO sessions (id, agent_type, title, status, is_hidden) VALUES (?,?,?,?,?)",
            (session, agent_type, "Strip iTerm2 Support", status, hidden),
        )
        self._write(
            "INSERT INTO workspaces (id, workspace_name, directory_name, active_session_id,"
            " state, updated_at, branch) VALUES (?,?,?,?,?,?,?)",
            (ws, name, name, session, state, "2026-08-24T20:00:00Z", f"{name}-branch"),
        )

    def add_message(self, session, role, content, created_at):
        self._write(
            "INSERT INTO session_messages (id, session_id, role, content, created_at)"
            " VALUES (?,?,?,?,?)",
            (f"m{created_at}", session, role, content, created_at),
        )

    # ── list_sessions ──────────────────────────────────────────────────
    def test_lists_claude_session(self):
        self.add_workspace()
        sessions = conductor.list_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].workspace_name, "daegu")
        self.assertEqual(sessions[0].session_title, "Strip iTerm2 Support")

    def test_excludes_non_claude_agents(self):
        """Codex and Cursor sessions have their own limit semantics; skip them."""
        for i, agent in enumerate(("codex", "cursor", "opencode")):
            self.add_workspace(ws=f"ws{i}", session=f"s{i}", agent_type=agent, name=agent)
        self.assertEqual(conductor.list_sessions(), [])

        # ...but a Claude session in the same store is still found.
        self.add_workspace(ws="wsc", session="sc", agent_type="claude", name="daegu")
        self.assertEqual([s.workspace_name for s in conductor.list_sessions()], ["daegu"])

    def test_excludes_archived_workspaces_and_hidden_sessions(self):
        self.add_workspace(ws="a", session="sa", state="archived")
        self.add_workspace(ws="b", session="sb", hidden=1, name="other")
        self.assertEqual(conductor.list_sessions(), [])

    def test_missing_database_is_not_an_error(self):
        conductor.CONDUCTOR_DB = Path("/nonexistent/conductor.db")
        self.assertFalse(conductor.is_available())
        self.assertEqual(conductor.list_sessions(), [])
        self.assertIsNone(conductor.read_content("ws1"))

    # ── read_content ───────────────────────────────────────────────────
    def test_detects_standing_limit(self):
        self.add_workspace()
        self.add_message("s1", "assistant", synthetic(), "2026-08-24T17:18:55Z")
        text = conductor.read_content("ws1")
        self.assertEqual(text, LIMIT_TEXT)
        self.assertIsNotNone(detect_session_limit(text))

    def test_ignores_limit_already_resumed_past(self):
        """A user message after the notice means the session moved on."""
        self.add_workspace()
        self.add_message("s1", "assistant", synthetic(), "2026-08-24T17:18:55Z")
        self.add_message("s1", "user", "continue", "2026-08-24T22:21:00Z")
        self.add_message("s1", "assistant", assistant_prose("back at it"), "2026-08-24T22:21:05Z")
        self.assertIsNone(conductor.read_content("ws1"))

    def test_ignores_agent_prose_quoting_the_phrase(self):
        self.add_workspace()
        self.add_message("s1", "assistant", assistant_prose(f"The README shows: {LIMIT_TEXT}"),
                         "2026-08-24T17:18:55Z")
        self.assertIsNone(conductor.read_content("ws1"))

    def test_ignores_working_session(self):
        self.add_workspace(status="working")
        self.add_message("s1", "assistant", synthetic(), "2026-08-24T17:18:55Z")
        self.assertIsNone(conductor.read_content("ws1"))

    def test_unknown_workspace(self):
        self.add_workspace()
        self.assertIsNone(conductor.read_content("does-not-exist"))

    def test_empty_session(self):
        self.add_workspace()
        self.assertIsNone(conductor.read_content("ws1"))

    # ── send confirmation ──────────────────────────────────────────────
    def test_latest_user_message_tracks_new_sends(self):
        """
        Backs the check that a resume reached the session we watched rather than
        whichever workspace happened to be on screen.
        """
        self.add_workspace()
        self.add_message("s1", "assistant", synthetic(), "2026-08-24T17:18:55Z")
        self.assertIsNone(conductor._latest_user_message_at("s1"))

        self.add_message("s1", "user", "continue", "2026-08-24T22:21:00Z")
        self.assertEqual(conductor._latest_user_message_at("s1"), "2026-08-24T22:21:00Z")

    def test_latest_user_message_ignores_other_sessions(self):
        self.add_workspace(ws="a", session="sa")
        self.add_workspace(ws="b", session="sb", name="other")
        self.add_message("sb", "user", "continue", "2026-08-24T22:21:00Z")
        self.assertIsNone(conductor._latest_user_message_at("sa"))


if __name__ == "__main__":
    unittest.main()
