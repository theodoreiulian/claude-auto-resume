"""
Tests for reading Terminal.app screens: is the text we sent still waiting unsubmitted?

The Codex screens are captured from the real Codex TUI (v0.153.4) at 100 columns.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_auto_resume import terminal
from claude_auto_resume.detector import detect_session_limit
from claude_auto_resume.terminal import _codex_rate_limit_prompt_open, _text_pending_in_prompt

CODEX_HEADER = """\
╭───────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.153.4)                │
│                                           │
│ model:     gpt-6-astra   /model to change │
│ directory: /private/tmp/codex-probe-work  │
╰───────────────────────────────────────────╯

› say hi in one word


■ You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit
https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 11:54 PM.

"""

# `continue` + Return arrived in one burst, so Codex took the Return as a pasted newline.
CODEX_TYPED_NOT_SENT = CODEX_HEADER + """\

› continue


  gpt-6-astra default · /private/tmp/codex-probe-work
"""

# A later lone Return submitted it: it moved into the transcript, keeping its marker.
CODEX_SENT = CODEX_HEADER + """\

› continue


■ You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit
https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 11:54 PM.


› Ask Codex to do anything

  gpt-6-astra default · /private/tmp/codex-probe-work
"""

# Codex opened this right after the limit notice.
CODEX_PICKER = CODEX_HEADER + """\

  Approaching rate limits
  Switch to gpt-5.6-luna for lower credit usage?

› 1. Switch to gpt-5.6-luna                 Fast and affordable agentic coding model.
  2. Keep current model
  3. Keep current model (never show again)  Hide future rate limit reminders about switching
                                            models.

  Press enter to confirm or esc to go back
"""

# After Esc Esc Return: picker closed, model unchanged, nothing submitted.
CODEX_PICKER_CLOSED = CODEX_HEADER + """\

› Ask Codex to do anything

  gpt-6-astra default · /private/tmp/codex-probe-work
"""

RULE = "─" * 60

CLAUDE_TYPED_NOT_SENT = f"""\
❯ continue
  ⎿  Interrupted
{RULE}
❯ continue
{RULE}
  ⏵⏵ auto mode on
"""

CLAUDE_SENT = f"""\
❯ continue
✻ Resuming…
{RULE}
❯\xa0
{RULE}
  ⏵⏵ auto mode on
"""


class TestTextPendingInPrompt(unittest.TestCase):
    def test_codex_screen_shows_the_limit(self):
        self.assertEqual(detect_session_limit(CODEX_TYPED_NOT_SENT).reset_label, "11:54 PM")

    def test_codex_text_typed_but_not_sent(self):
        self.assertTrue(_text_pending_in_prompt(CODEX_TYPED_NOT_SENT, "continue"))

    def test_codex_text_sent_is_not_mistaken_for_pending(self):
        """The transcript's `› continue` sits above the live composer; only the latter counts."""
        self.assertIn("› continue", CODEX_SENT)
        self.assertFalse(_text_pending_in_prompt(CODEX_SENT, "continue"))

    def test_claude_ruled_input_box(self):
        self.assertTrue(_text_pending_in_prompt(CLAUDE_TYPED_NOT_SENT, "continue"))
        self.assertFalse(_text_pending_in_prompt(CLAUDE_SENT, "continue"))

    def test_rules_in_output_do_not_hide_the_codex_composer(self):
        """Tool output can draw rules; a fence with no prompt line in it isn't the input box."""
        screen = f"{RULE}\n  some table output\n{RULE}\n\n› continue\n\n  gpt-6-astra default\n"
        self.assertTrue(_text_pending_in_prompt(screen, "continue"))

    def test_empty_needle(self):
        self.assertFalse(_text_pending_in_prompt(CODEX_TYPED_NOT_SENT, "  "))


class TestCodexRateLimitPrompt(unittest.TestCase):
    def test_detects_open_picker(self):
        self.assertTrue(_codex_rate_limit_prompt_open(CODEX_PICKER))

    def test_no_picker(self):
        for screen in (CODEX_PICKER_CLOSED, CODEX_TYPED_NOT_SENT, CODEX_SENT, CLAUDE_SENT, ""):
            with self.subTest(screen=screen[-40:]):
                self.assertFalse(_codex_rate_limit_prompt_open(screen))

    def test_picker_long_since_scrolled_up_is_not_open(self):
        screen = CODEX_PICKER + "".join(f"output {i}\n" for i in range(20)) + "› Ask Codex\n"
        self.assertFalse(_codex_rate_limit_prompt_open(screen))


class TestSendToCodex(unittest.TestCase):
    """The send path over a scripted screen sequence, recording what reaches Terminal."""

    def _send(self, screens):
        scripts = []

        def run(script, timeout=10):
            scripts.append(script)
            return True, "", ""

        with mock.patch.object(terminal, "_terminal_tab_ref", return_value=(1, 2)), \
             mock.patch.object(terminal, "read_content", side_effect=screens), \
             mock.patch.object(terminal, "_run_applescript_raw", side_effect=run), \
             mock.patch.object(terminal.time, "sleep"):
            result = terminal.send_text_detailed("/dev/ttys004", "continue")
        return result, scripts

    def test_closes_picker_then_types_and_submits(self):
        (ok, err), scripts = self._send(
            [CODEX_PICKER, CODEX_PICKER_CLOSED, CODEX_TYPED_NOT_SENT, CODEX_SENT]
        )
        self.assertTrue(ok, err)
        self.assertEqual(len(scripts), 3)
        self.assertIn("(character id 27) & (character id 27)", scripts[0])
        self.assertIn('do script "continue" in tab 2 of window 1', scripts[1])
        self.assertIn('do script "" in tab 2 of window 1', scripts[2])

    def test_never_types_into_a_picker_that_stays_open(self):
        (ok, err), scripts = self._send([CODEX_PICKER, CODEX_PICKER])
        self.assertFalse(ok)
        self.assertIn("would not close", err)
        self.assertEqual(len(scripts), 1)   # only the Esc; "continue" was never sent

    def test_no_picker_means_no_escape(self):
        (ok, err), scripts = self._send([CODEX_PICKER_CLOSED, CODEX_TYPED_NOT_SENT, CODEX_SENT])
        self.assertTrue(ok, err)
        self.assertNotIn("character id 27", "".join(scripts))


if __name__ == "__main__":
    unittest.main()
