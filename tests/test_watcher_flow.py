"""
End-to-end test of the watch → detect → schedule → resume flow.

Drives a Watcher over a fake Conductor database so the whole path is exercised:
the SQL read, the synthetic-message gate, the detector, the state machine, and the
resume send.
"""

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_auto_resume import conductor
from claude_auto_resume.targets import Target, TargetKind
from claude_auto_resume.watcher import Watcher, WatcherState
from tests.support import SCHEMA, synthetic


class WatcherFlowTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "conductor.db"

        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO sessions (id, agent_type, title, status) VALUES"
            " ('s1','claude','Strip iTerm2 Support','idle')"
        )
        conn.execute(
            "INSERT INTO workspaces (id, workspace_name, directory_name,"
            " active_session_id, state, updated_at, branch) VALUES"
            " ('ws1','daegu','daegu','s1','active','2026-08-24T20:00:00Z','strip-iterm2-support')"
        )
        conn.commit()
        conn.close()

        self._orig = conductor.CONDUCTOR_DB
        conductor.CONDUCTOR_DB = self.db_path
        self.addCleanup(lambda: setattr(conductor, "CONDUCTOR_DB", self._orig))

        self.target = Target(
            kind=TargetKind.CONDUCTOR, ref="ws1",
            name="daegu", detail="Strip iTerm2 Support",
        )

    def _hit_limit(self, at="2026-08-24T17:18:55Z"):
        """Write the synthetic notice Claude Code emits on hitting the limit."""
        reset = (datetime.now() + timedelta(hours=2)).strftime("%-I:%M%p").lower()
        text = f"You've hit your session limit · resets {reset} (Europe/Paris)"
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO session_messages (id, session_id, role, content, created_at)"
            " VALUES (?,?,?,?,?)",
            (f"m{at}", "s1", "assistant", synthetic(text), at),
        )
        conn.commit()
        conn.close()

    def test_quiet_session_stays_watching(self):
        watcher = Watcher(target=self.target)
        self.assertIsNone(watcher.poll())
        self.assertEqual(watcher.state, WatcherState.WATCHING)
        # Conductor reports nothing on nearly every poll; that must not look like an error.
        self.assertIsNone(watcher.last_error)

    def test_closed_terminal_tab_is_reported(self):
        """The same empty read *is* an error for Terminal.app, where it means gone."""
        tab = Target(kind=TargetKind.TERMINAL, ref="/dev/ttys999", name="gone", detail="")
        watcher = Watcher(target=tab)
        with mock.patch("claude_auto_resume.watcher.read_content", return_value=None):
            self.assertIsNone(watcher.poll())
        self.assertIn("not found", watcher.last_error)

    def test_terminal_notice_left_on_screen_is_not_resumed_twice(self):
        """
        A terminal keeps showing the notice after the resume. Read again, a same-day time
        would roll to tomorrow and send a second, unwanted "continue".
        """
        tab = Target(kind=TargetKind.TERMINAL, ref="/dev/ttys004", name="codex", detail="")
        watcher = Watcher(target=tab)
        notice = (
            "■ You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
            "visit\nhttps://chatgpt.com/codex/settings/usage to purchase more credits or try "
            "again at 11:54 PM.\n"
        )
        screens = [
            f"› fix the bug\n\n{notice}\n› Ask Codex to do anything\n",
            f"› fix the bug\n\n{notice}\n› continue\n\n• Working (3s • esc to interrupt)\n",
        ]

        with mock.patch("claude_auto_resume.watcher.read_content", side_effect=screens), \
             mock.patch("claude_auto_resume.watcher.send_text_detailed",
                        return_value=(True, None)):
            self.assertIsNotNone(watcher.poll())
            self.assertTrue(watcher.fire_resume())
            self.assertIsNone(watcher.poll())

        self.assertEqual(watcher.state, WatcherState.WATCHING)

        # A new, different notice further down is a new limit.
        later = notice.replace("11:54 PM", "Sep 11th, 2026 4:54 AM")
        with mock.patch("claude_auto_resume.watcher.read_content",
                        return_value=screens[1] + f"\n{later}\n› Ask Codex to do anything\n"):
            info = watcher.poll()
        self.assertIsNotNone(info)
        self.assertEqual(info.reset_label, "Sep 11th, 2026 4:54 AM")

    def test_same_notice_counts_again_once_it_has_left_the_screen(self):
        tab = Target(kind=TargetKind.TERMINAL, ref="/dev/ttys004", name="codex", detail="")
        watcher = Watcher(target=tab)
        notice = "You've hit your usage limit or try again at 11:54 PM."

        with mock.patch("claude_auto_resume.watcher.read_content",
                        side_effect=[notice, "all quiet", f"later\n{notice}"]), \
             mock.patch("claude_auto_resume.watcher.send_text_detailed",
                        return_value=(True, None)):
            self.assertIsNotNone(watcher.poll())
            watcher.fire_resume()
            self.assertIsNone(watcher.poll())       # notice gone: guard released
            self.assertIsNotNone(watcher.poll())    # so its return is a real limit

    def test_limit_moves_watcher_to_waiting_and_schedules_ahead(self):
        watcher = Watcher(target=self.target)
        self._hit_limit()

        info = watcher.poll()
        self.assertIsNotNone(info)
        self.assertEqual(watcher.state, WatcherState.WAITING_TO_RESUME)

        # Resume is one minute after the reset, and still in the future.
        self.assertEqual(info.resume_time, info.reset_time + timedelta(minutes=1))
        delay = watcher.seconds_until_resume()
        self.assertGreater(delay, 0)
        self.assertLess(delay, 3 * 3600)
        self.assertIn("Resumes at", watcher.status_text)

    def test_successful_resume_returns_to_watching(self):
        watcher = Watcher(target=self.target)
        self._hit_limit()
        watcher.poll()

        with mock.patch(
            "claude_auto_resume.watcher.send_text_detailed", return_value=(True, None)
        ) as send:
            self.assertTrue(watcher.fire_resume())

        send.assert_called_once_with(self.target, "continue")
        self.assertEqual(watcher.state, WatcherState.WATCHING)
        self.assertEqual(watcher.resume_count, 1)
        self.assertIsNone(watcher.reset_info)

    def test_failed_resume_keeps_state_and_records_error(self):
        watcher = Watcher(target=self.target)
        self._hit_limit()
        watcher.poll()

        with mock.patch(
            "claude_auto_resume.watcher.send_text_detailed",
            return_value=(False, "a different workspace may have been on screen"),
        ):
            self.assertFalse(watcher.fire_resume())

        self.assertEqual(watcher.state, WatcherState.WAITING_TO_RESUME)
        self.assertEqual(watcher.resume_count, 0)
        self.assertIn("different workspace", watcher.last_error)

    def test_limit_is_not_redetected_after_resuming(self):
        """The notice stays in the transcript; a later user message must retire it."""
        watcher = Watcher(target=self.target)
        self._hit_limit()
        watcher.poll()

        with mock.patch(
            "claude_auto_resume.watcher.send_text_detailed", return_value=(True, None)
        ):
            watcher.fire_resume()

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO session_messages (id, session_id, role, content, created_at)"
            " VALUES ('m-resume','s1','user','continue','2026-08-24T22:30:00Z')"
        )
        conn.commit()
        conn.close()

        self.assertIsNone(watcher.poll())
        self.assertEqual(watcher.state, WatcherState.WATCHING)


if __name__ == "__main__":
    unittest.main()
