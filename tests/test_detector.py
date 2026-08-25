"""Tests for session limit detection and reset-time parsing."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_auto_resume.detector import detect_session_limit, _resolve_timezone


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
