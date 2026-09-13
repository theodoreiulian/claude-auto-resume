"""Tests for Conductor message classification."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_auto_resume.conductor import _limit_notice_text, _synthetic_limit_text
from claude_auto_resume.detector import detect_session_limit
from tests.support import CODEX_LIMIT_TEXT, LIMIT_TEXT, assistant_prose, codex_error


def _msg(**overrides) -> str:
    """A stream-json assistant message, synthetic by default."""
    message = {
        "role": "assistant",
        "model": "<synthetic>",
        "content": [{"type": "text", "text": LIMIT_TEXT}],
    }
    message.update(overrides.pop("message", {}))
    envelope = {"type": "assistant", "message": message}
    envelope.update(overrides)
    # Conductor stores the SDK's JSON with non-ASCII intact, including the '·'.
    return json.dumps(envelope, ensure_ascii=False)


class TestSyntheticLimitText(unittest.TestCase):
    def test_real_limit_notice_is_extracted(self):
        self.assertEqual(_synthetic_limit_text(_msg()), LIMIT_TEXT)
        self.assertIsNotNone(detect_session_limit(_synthetic_limit_text(_msg())))

    def test_model_authored_text_is_ignored(self):
        """
        The decisive case. An agent discussing the limit puts the exact phrase in its
        own transcript; only Claude Code's synthetic notice means a session is limited.
        """
        prose = _msg(message={"model": "claude-opus-5"})
        self.assertIn(LIMIT_TEXT, prose)          # the phrase really is in there
        self.assertIsNone(_synthetic_limit_text(prose))

    def test_tool_result_quoting_the_phrase_is_ignored(self):
        tool_result = json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": f"README says: {LIMIT_TEXT}"}],
            },
        }, ensure_ascii=False)
        self.assertIsNone(_synthetic_limit_text(tool_result))

    def test_result_and_system_envelopes_are_ignored(self):
        for kind in ("result", "system", "error"):
            with self.subTest(kind=kind):
                self.assertIsNone(_synthetic_limit_text(json.dumps({"type": kind})))

    def test_synthetic_thinking_block_yields_no_text(self):
        blocks = _msg(message={"content": [{"type": "thinking", "thinking": LIMIT_TEXT}]})
        self.assertIsNone(_synthetic_limit_text(blocks))

    def test_malformed_rows_are_ignored(self):
        for raw in ("", "not json", "{", "<synthetic> but not json", "null", "[]"):
            with self.subTest(raw=raw):
                self.assertIsNone(_synthetic_limit_text(raw))


class TestLimitNoticeText(unittest.TestCase):
    def test_claude_notice_still_extracted(self):
        self.assertEqual(_limit_notice_text(_msg()), LIMIT_TEXT)

    def test_codex_usage_limit_error_is_extracted(self):
        text = _limit_notice_text(codex_error())
        self.assertEqual(text, CODEX_LIMIT_TEXT)
        self.assertIsNotNone(detect_session_limit(text))

    def test_errors_codex_will_retry_are_ignored(self):
        """'Reconnecting... 2/5' resolves itself; it doesn't leave the session stuck."""
        self.assertIsNone(_limit_notice_text(codex_error("Reconnecting... 2/5", will_retry=True)))

    def test_codex_prose_quoting_the_phrase_is_ignored(self):
        prose = assistant_prose(f"The error said: {CODEX_LIMIT_TEXT}")
        self.assertIn(CODEX_LIMIT_TEXT, prose)
        self.assertIsNone(_limit_notice_text(prose))

    def test_codex_tool_result_quoting_the_phrase_is_ignored(self):
        tool_result = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "content": f'{{"type":"error","content":"{CODEX_LIMIT_TEXT}"}}'},
            ]},
        }, ensure_ascii=False)
        self.assertIsNone(_limit_notice_text(tool_result))

    def test_malformed_error_rows_are_ignored(self):
        for raw in ('{"type":"error"}', '{"type":"error","content":42}', '"error"', "error"):
            with self.subTest(raw=raw):
                self.assertIsNone(_limit_notice_text(raw))


if __name__ == "__main__":
    unittest.main()
