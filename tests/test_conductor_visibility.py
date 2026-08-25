"""
Tests for deciding which Conductor workspace is on screen.

Conductor renders one workspace at a time, so this is what stands between a resume
and typing "continue" into the wrong agent.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_auto_resume import conductor
from tests.support import SCHEMA


class VisibilityTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        db = Path(self._tmp.name) / "conductor.db"
        conn = sqlite3.connect(db)
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO sessions (id, agent_type, title, status) VALUES"
                     " ('s1','claude','Strip iTerm2 Support','idle')")
        conn.execute("INSERT INTO workspaces (id, workspace_name, directory_name,"
                     " active_session_id, state, updated_at, branch) VALUES"
                     " ('ws1','daegu','daegu','s1','active','2026-08-24T20:00:00Z',"
                     " 'strip-iterm2-support')")
        conn.commit()
        conn.close()
        self._orig = conductor.CONDUCTOR_DB
        conductor.CONDUCTOR_DB = db
        self.addCleanup(lambda: setattr(conductor, "CONDUCTOR_DB", self._orig))
        self.session = conductor.list_sessions()[0]

    def test_branch_in_header_means_visible(self):
        with mock.patch.object(conductor, "_window_header_texts",
                               return_value=["strip-iterm2-support"]):
            self.assertIs(conductor._workspace_is_displayed(self.session), True)

    def test_different_branch_means_not_visible(self):
        with mock.patch.object(conductor, "_window_header_texts",
                               return_value=["some-other-branch"]):
            self.assertIs(conductor._workspace_is_displayed(self.session), False)

    def test_unreadable_header_means_not_visible(self):
        """A header we can't read is not evidence that the right workspace is up."""
        with mock.patch.object(conductor, "_window_header_texts", return_value=[]):
            self.assertIs(conductor._workspace_is_displayed(self.session), False)

    def test_workspace_without_branch_is_undetermined(self):
        """A workspace still being created has no branch to match on."""
        session = conductor.ConductorSession(
            workspace_id="ws2", session_id="s2", workspace_name="new",
            session_title="Untitled", status="idle", branch="",
        )
        self.assertIsNone(conductor._workspace_is_displayed(session))

    def test_switch_confirms_by_rereading_the_header(self):
        """A switch that lands on the wrong workspace must report failure."""
        with mock.patch.object(conductor, "_run_applescript", return_value=(True, "", "")), \
             mock.patch.object(conductor, "_window_header_texts",
                               return_value=["a-different-branch"]), \
             mock.patch.object(conductor.time, "sleep"):
            self.assertFalse(conductor._switch_to_workspace(self.session))

        with mock.patch.object(conductor, "_run_applescript", return_value=(True, "", "")), \
             mock.patch.object(conductor, "_window_header_texts",
                               return_value=["strip-iterm2-support"]), \
             mock.patch.object(conductor.time, "sleep"):
            self.assertTrue(conductor._switch_to_workspace(self.session))

    def test_send_aborts_when_switch_fails(self):
        """Never type into a workspace we couldn't confirm."""
        with mock.patch.object(conductor, "has_accessibility_permission", return_value=True), \
             mock.patch.object(conductor, "_run_applescript", return_value=(True, "", "")), \
             mock.patch.object(conductor, "_has_open_window", return_value=True), \
             mock.patch.object(conductor, "_workspace_is_displayed", return_value=False), \
             mock.patch.object(conductor, "_switch_to_workspace", return_value=False), \
             mock.patch.object(conductor, "_dismiss_palette") as dismiss, \
             mock.patch.object(conductor, "_focused_composer_state") as composer, \
             mock.patch.object(conductor.time, "sleep"):
            ok, err = conductor.send_text_detailed("ws1", "continue")

        self.assertFalse(ok)
        self.assertIn("different workspace", err)
        composer.assert_not_called()   # never got as far as typing
        dismiss.assert_called_once()   # and cleaned up the palette

    def test_send_aborts_when_conductor_has_no_window(self):
        """
        Conductor keeps running with no windows, and a closed one can't be reopened.
        That must read as a window problem, not a workspace mix-up.
        """
        with mock.patch.object(conductor, "has_accessibility_permission", return_value=True), \
             mock.patch.object(conductor, "_run_applescript", return_value=(True, "", "")), \
             mock.patch.object(conductor, "_has_open_window", return_value=False), \
             mock.patch.object(conductor, "_workspace_is_displayed") as displayed, \
             mock.patch.object(conductor.time, "sleep"):
            ok, err = conductor.send_text_detailed("ws1", "continue")

        self.assertFalse(ok)
        self.assertIn("no open window", err)
        displayed.assert_not_called()  # never even looked at workspaces

    def test_send_reports_failure_when_database_never_confirms(self):
        """Typing succeeded but the watched session didn't receive it."""
        with mock.patch.object(conductor, "has_accessibility_permission", return_value=True), \
             mock.patch.object(conductor, "_run_applescript", return_value=(True, "", "")), \
             mock.patch.object(conductor, "_has_open_window", return_value=True), \
             mock.patch.object(conductor, "_workspace_is_displayed", return_value=True), \
             mock.patch.object(conductor, "_focused_composer_state", return_value=(True, "")), \
             mock.patch.object(conductor, "_read_focused_element",
                               return_value=(True, "AXTextArea", "", "continue")), \
             mock.patch.object(conductor, "_latest_user_message_at", return_value=None), \
             mock.patch.object(conductor.time, "sleep"), \
             mock.patch.object(conductor.time, "monotonic", side_effect=[0, 1, 99]):
            ok, err = conductor.send_text_detailed("ws1", "continue")

        self.assertFalse(ok)
        self.assertIn("never", err)

    def test_send_succeeds_when_database_confirms(self):
        with mock.patch.object(conductor, "has_accessibility_permission", return_value=True), \
             mock.patch.object(conductor, "_run_applescript", return_value=(True, "", "")), \
             mock.patch.object(conductor, "_has_open_window", return_value=True), \
             mock.patch.object(conductor, "_workspace_is_displayed", return_value=True), \
             mock.patch.object(conductor, "_focused_composer_state", return_value=(True, "")), \
             mock.patch.object(conductor, "_read_focused_element",
                               return_value=(True, "AXTextArea", "", "continue")), \
             mock.patch.object(conductor, "_latest_user_message_at",
                               side_effect=[None, "2026-08-24T22:21:00Z"]), \
             mock.patch.object(conductor.time, "sleep"), \
             mock.patch.object(conductor.time, "monotonic", side_effect=[0, 1]):
            ok, err = conductor.send_text_detailed("ws1", "continue")

        self.assertTrue(ok)
        self.assertIsNone(err)


if __name__ == "__main__":
    unittest.main()
