"""Tests for session limit detection and reset-time parsing."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_auto_resume.detector import detect_session_limit, _resolve_timezone
from tests.support import CODEX_LIMIT_TEXT, LIMIT_TEXT


class TestDetectSessionLimit(unittest.TestCase):
    def test_terminal_format_with_abbreviation(self):
        info = detect_session_limit("You've hit your session limit · resets 5:00 PM EDT")
        self.assertIsNotNone(info)
        self.assertEqual(info.resume_time, info.reset_time + timedelta(minutes=1))

    def test_conductor_format_with_iana_zone(self):
        """The exact string Claude Code emits under Conductor."""
        info = detect_session_limit(
            "You've hit your session limit · resets 10:20pm (Europe/Paris)"
        )
        self.assertIsNotNone(info)
        # 10:20pm in Paris, expressed as local wall-clock time.
        expected = (
            datetime.now(timezone.utc)
            .astimezone(ZoneInfo("Europe/Paris"))
            .replace(hour=22, minute=20, second=0, microsecond=0)
        )
        if expected <= datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Paris")):
            expected += timedelta(days=1)
        self.assertEqual(info.reset_time, expected.astimezone().replace(tzinfo=None))

    def test_spelled_out_zone_falls_back_to_local(self):
        info = detect_session_limit(
            "You've hit your session limit · resets 5pm (Eastern Daylight Time)"
        )
        self.assertIsNotNone(info)
        self.assertEqual(info.reset_time.hour, 17)

    def test_reset_time_is_always_in_the_future(self):
        for text in (
            "You've hit your session limit · resets 12:01am (Europe/Paris)",
            "You've hit your session limit · resets 11:59pm (America/New_York)",
            "You've hit your session limit · resets 5:00 PM PDT",
        ):
            with self.subTest(text=text):
                info = detect_session_limit(text)
                self.assertIsNotNone(info)
                self.assertGreater(info.reset_time, datetime.now())

    def test_no_match(self):
        self.assertIsNone(detect_session_limit("nothing to see here"))
        self.assertIsNone(detect_session_limit(""))


def _local_today_at(hour: int, minute: int) -> datetime:
    """The next local occurrence of hour:minute, as the detector computes it."""
    now = datetime.now()
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


class TestDetectCodexUsageLimit(unittest.TestCase):
    def test_same_day_format(self):
        """Verbatim from a Conductor Codex session."""
        info = detect_session_limit(CODEX_LIMIT_TEXT)
        self.assertIsNotNone(info)
        self.assertEqual(info.reset_time, _local_today_at(23, 54))
        self.assertEqual(info.resume_time, info.reset_time + timedelta(minutes=1))
        self.assertEqual(info.reset_label, "11:54 PM")

    def test_error_prefix_and_curly_apostrophe(self):
        info = detect_session_limit(
            "Error: You’ve hit your usage limit. Upgrade to Pro "
            "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage "
            "to purchase more credits or try again at 11:54 PM."
        )
        self.assertIsNotNone(info)
        self.assertEqual(info.reset_label, "11:54 PM")

    def test_dated_format_is_exact(self):
        """Codex adds the date when the reset isn't today. It's exact — never rolled forward."""
        info = detect_session_limit(
            "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
            "visit https://chatgpt.com/codex/settings/usage to purchase more credits or try "
            "again at Sep 10th, 2026 2:22 AM."
        )
        self.assertIsNotNone(info)
        self.assertEqual(info.reset_time, datetime(2026, 9, 10, 2, 22))
        self.assertEqual(info.reset_label, "Sep 10th, 2026 2:22 AM")

    def test_other_plan_wordings(self):
        for text in (
            "You've hit your usage limit. Upgrade to Plus to continue using Codex "
            "(https://chatgpt.com/explore/plus), or try again at 3:05 AM.",
            "You've hit your usage limit. To get more access now, send a request to your "
            "admin or try again at 3:05 AM.",
            "You've hit your usage limit for GPT-5.5. Switch to another model now, "
            "or try again at 3:05 AM.",
        ):
            with self.subTest(text=text):
                info = detect_session_limit(text)
                self.assertIsNotNone(info)
                self.assertEqual(info.reset_time, _local_today_at(3, 5))

    def test_wrapped_across_terminal_lines(self):
        """The Codex TUI wraps the notice, indenting continuation lines."""
        screen = (
            "■ You've hit your usage limit. Upgrade to Pro\n"
            "  (https://chatgpt.com/explore/pro), visit\n"
            "  https://chatgpt.com/codex/settings/usage to purchase more credits or try again at Sep\n"
            "  1st, 2027 11:54\n"
            "  PM.\n"
        )
        info = detect_session_limit(screen)
        self.assertIsNotNone(info)
        self.assertEqual(info.reset_time, datetime(2027, 9, 1, 23, 54))
        self.assertEqual(info.reset_label, "Sep 1st, 2027 11:54 PM")

    def test_try_again_later_has_nothing_to_schedule(self):
        self.assertIsNone(detect_session_limit(
            "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro) "
            "or try again later."
        ))

    def test_try_again_later_does_not_borrow_a_later_time(self):
        self.assertIsNone(detect_session_limit(
            "You've hit your usage limit. Try again later.\n"
            "Something unrelated. Try again at 5:00 PM."
        ))

    def test_impossible_date_is_ignored(self):
        self.assertIsNone(detect_session_limit(
            "You've hit your usage limit or try again at Feb 30th, 2027 1:00 AM."
        ))

    def test_last_notice_on_screen_wins(self):
        """A terminal can show an old notice above a new one."""
        screen = (
            f"{LIMIT_TEXT}\n...\n"
            "You've hit your usage limit or try again at Sep 1st, 2027 1:00 AM.\n...\n"
            "You've hit your usage limit or try again at Sep 2nd, 2027 6:30 AM.\n"
        )
        self.assertEqual(detect_session_limit(screen).reset_label, "Sep 2nd, 2027 6:30 AM")

        self.assertEqual(
            detect_session_limit(f"{CODEX_LIMIT_TEXT}\n...\n{LIMIT_TEXT}").reset_label,
            "10:20pm (Europe/Paris)",
        )


class TestResolveTimezone(unittest.TestCase):
    def test_iana_name(self):
        self.assertEqual(_resolve_timezone("Europe/Paris"), ZoneInfo("Europe/Paris"))

    def test_abbreviation(self):
        self.assertEqual(_resolve_timezone("EDT"), timezone(timedelta(hours=-4)))

    def test_unknown_returns_none(self):
        self.assertIsNone(_resolve_timezone("Eastern Daylight Time"))
        self.assertIsNone(_resolve_timezone("Not/AZone"))
        self.assertIsNone(_resolve_timezone(None))


if __name__ == "__main__":
    unittest.main()
