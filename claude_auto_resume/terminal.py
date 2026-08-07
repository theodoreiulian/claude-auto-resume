"""
terminal.py — AppleScript-based terminal interaction layer.

Supports Terminal.app and iTerm2. Uses osascript via subprocess to:
- List all open terminal windows/tabs/sessions
- Read visible content from a specific terminal (by TTY)
- Send text input to a specific terminal (by TTY)

Input is injected through each app's own scripting command (`do script` for
Terminal.app, `write text` for iTerm2), which places the text in the session's
pty *input* queue where the foreground program (e.g. Claude Code) reads it.
Writing to the /dev/ttys* device instead would only paint the characters onto
the screen — the program's stdin would never see them.
"""

import logging
import subprocess
import re
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("claude_auto_resume.terminal")

# How long to let the terminal settle before re-reading it to verify a send.
SEND_SETTLE_DELAY = 1.2


@dataclass
class TerminalInfo:
    """Represents a single terminal tab or session."""
    tty: str           # e.g. "/dev/ttys001"
    app: str           # "Terminal" or "iTerm2"
    name: str          # Window/tab title
    processes: str     # Running processes description


def _run_applescript_raw(script: str, timeout: int = 10) -> tuple[bool, str, str]:
    """
    Run an AppleScript, returning (ok, stdout, stderr).

    Unlike `_run_applescript`, this exposes stderr so callers can report why a
    send failed instead of just that it did.
    """
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-"],
            input=script,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        return (
            result.returncode == 0,
            result.stdout.strip(),
            result.stderr.strip(),
        )
    except subprocess.TimeoutExpired:
        return False, "", "osascript timed out"
    except (FileNotFoundError, OSError) as e:
        return False, "", str(e)


def _run_applescript(script: str, timeout: int = 10) -> Optional[str]:
    """Run an AppleScript and return stdout, or None on error."""
    ok, out, _ = _run_applescript_raw(script, timeout=timeout)
    return out if ok else None


def _escape_applescript(text: str) -> str:
    """Escape a Python string for embedding in an AppleScript string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _app_is_running(app_name: str) -> bool:
    """Check if a macOS application is currently running."""
    script = f'''
    tell application "System Events"
        set appRunning to (name of every process) contains "{app_name}"
    end tell
    return appRunning
    '''
    result = _run_applescript(script)
    return result == "true"


def _list_terminal_app() -> list[TerminalInfo]:
    """List all open tabs in Terminal.app."""
    if not _app_is_running("Terminal"):
        return []

    script = '''
    tell application "Terminal"
        set output to ""
        repeat with w in windows
            set winName to name of w
            repeat with t in tabs of w
                set tabTTY to tty of t
                set tabProcs to processes of t
                -- Build a delimited string: TTY|||windowName|||processes
                set procStr to ""
                repeat with p in tabProcs
                    if procStr is not "" then set procStr to procStr & ", "
                    set procStr to procStr & (p as text)
                end repeat
                set output to output & tabTTY & "|||" & winName & "|||" & procStr & "\\n"
            end repeat
        end repeat
        return output
    end tell
    '''
    raw = _run_applescript(script)
    if not raw:
        return []

    terminals = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line or "|||" not in line:
            continue
        parts = line.split("|||")
        if len(parts) >= 3:
            terminals.append(TerminalInfo(
                tty=parts[0].strip(),
                app="Terminal",
                name=parts[1].strip(),
                processes=parts[2].strip(),
            ))
    return terminals


def _list_iterm2() -> list[TerminalInfo]:
    """List all open sessions in iTerm2."""
    if not _app_is_running("iTerm2"):
        return []

    script = '''
    tell application "iTerm2"
        set output to ""
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    set sessionTTY to tty of s
                    set sessionName to name of s
                    set output to output & sessionTTY & "|||" & sessionName & "|||" & "\\n"
                end repeat
            end repeat
        end repeat
        return output
    end tell
    '''
    raw = _run_applescript(script)
    if not raw:
        return []

    terminals = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line or "|||" not in line:
            continue
        parts = line.split("|||")
        if len(parts) >= 2:
            terminals.append(TerminalInfo(
                tty=parts[0].strip(),
                app="iTerm2",
                name=parts[1].strip(),
                processes="",
            ))
    return terminals


def list_terminals() -> list[TerminalInfo]:
    """List all open terminal windows/tabs/sessions across supported apps."""
    terminals = []
    terminals.extend(_list_terminal_app())
    terminals.extend(_list_iterm2())
    return terminals


def _terminal_tab_ref(tty: str) -> Optional[tuple[int, int]]:
    """
    Resolve a Terminal.app TTY to (window index, tab index), or None if not found.

    Both `read_content` and `send_text` need to address a tab by *index*
    (`tab i of window w`) rather than by a `repeat with t in ...` loop reference —
    see the `contents` gotcha noted in `read_content`, and `do script`, which needs
    a real tab specifier as its target.
    """
    script = f'''
    tell application "Terminal"
        set wi to 0
        repeat with w in windows
            set wi to wi + 1
            set ti to 0
            repeat with t in tabs of w
                set ti to ti + 1
                if tty of t is "{tty}" then
                    return (wi as text) & "," & (ti as text)
                end if
            end repeat
        end repeat
        return ""
    end tell
    '''
    raw = _run_applescript(script)
    if not raw or "," not in raw:
        return None
    try:
        wi, ti = raw.split(",", 1)
        return int(wi.strip()), int(ti.strip())
    except ValueError:
        return None


def read_content(tty: str, app: str) -> Optional[str]:
    """
    Read the visible content of a terminal tab/session identified by TTY.

    For Terminal.app, reads the `contents` property (visible screen).
    For iTerm2, reads the `contents` property of the matching session.
    """
    if app == "Terminal":
        # NOTE: `contents of t` where `t` is a `repeat with t in ...` loop
        # reference does NOT return the tab's text — `contents` collides with
        # AppleScript's built-in dereference operator, so it yields the tab's
        # object specifier (e.g. "tab 1 of window id 6393") instead. We must
        # reference the tab by index (`contents of tab i of w`) to read text.
        ref = _terminal_tab_ref(tty)
        if ref is None:
            return None
        wi, ti = ref
        script = f'''
        tell application "Terminal"
            return contents of tab {ti} of window {wi}
        end tell
        '''
    elif app == "iTerm2":
        # Same `contents` dereference gotcha as Terminal.app above: reference
        # the session by index (`contents of session i of t`) so `contents`
        # reads the text property rather than returning the object specifier.
        script = f'''
        tell application "iTerm2"
            repeat with w in windows
                repeat with t in tabs of w
                    set i to 0
                    repeat with s in sessions of t
                        set i to i + 1
                        if tty of s is "{tty}" then
                            return contents of session i of t
                        end if
                    end repeat
                end repeat
            end repeat
            return ""
        end tell
        '''
    else:
        return None

    return _run_applescript(script, timeout=15)


# Characters Claude Code draws around its input box, stripped before looking
# for the prompt marker. \xa0 is the non-breaking space it pads the input with.
_BOX_CHARS = "│┃║╎┆┊╭╮╰╯─━┌┐└┘| \t\xa0"

# Prompt markers Claude Code has used for its input line across versions.
_PROMPT_MARKERS = (">", "❯", "›", "»")

# Characters that make up the horizontal rules Claude Code draws above and
# below its input box.
_RULE_CHARS = set("─━═-_╌┄┈")


def _input_box_lines(content: str) -> Optional[list[str]]:
    """
    Return the lines inside Claude Code's input box, or None if it isn't found.

    Current builds fence the input between two horizontal rules:

        ──────────────────────────────
        ❯ continue
        ──────────────────────────────
          ⏵⏵ auto mode on …

    Isolating that region matters: a *submitted* prompt stays on screen in the
    transcript as `❯ continue`, so matching anywhere in the visible content
    cannot tell "typed but not sent" from "already sent".
    """
    lines = content.splitlines()
    rules = [
        i for i, line in enumerate(lines)
        if len(line.strip()) >= 10 and set(line.strip()) <= _RULE_CHARS
    ]
    if len(rules) < 2:
        return None

    top, bottom = rules[-2], rules[-1]
    if bottom - top < 2:
        return None
    return lines[top + 1:bottom]


def _text_pending_in_prompt(content: str, text: str) -> bool:
    """
    True if `text` looks like it is sitting unsubmitted in Claude Code's input box.

    Prefers the fenced input box (see `_input_box_lines`); falls back to scanning
    the last few lines for a `│ > continue`-style prompt marker on older builds
    that don't draw the rules.
    """
    needle = text.strip()
    if not needle:
        return False

    region = _input_box_lines(content)
    if region is not None:
        return any(needle in line for line in region)

    lines = [ln for ln in content.splitlines() if ln.strip(_BOX_CHARS)]
    for line in lines[-10:]:
        stripped = line.strip(_BOX_CHARS)
        if stripped.startswith(_PROMPT_MARKERS) and needle in stripped[1:]:
            return True
    return False


def _send_terminal(tty: str, text: str) -> tuple[bool, Optional[str]]:
    """
    Inject `text` + Return into a Terminal.app tab.

    1. `do script ... in tab` — real input injection.
    2. `do script "" in tab` — a bare Return, if the text landed but wasn't submitted
       (Claude Code's input can treat a fast trailing newline as a literal newline).

    Deliberately AppleScript-only: no System Events / `keystroke` path, so this never
    triggers a macOS Accessibility permission prompt and never steals keyboard focus.
    """
    ref = _terminal_tab_ref(tty)
    if ref is None:
        return False, "Terminal tab not found (closed?)"
    wi, ti = ref

    before = read_content(tty, "Terminal") or ""

    # ── Stage 1: native injection ──────────────────────────────────────
    ok, _, err = _run_applescript_raw(f'''
    tell application "Terminal"
        do script "{_escape_applescript(text)}" in tab {ti} of window {wi}
    end tell
    ''')

    if not ok:
        logger.error("Stage 1 (`do script`) failed on %s: %s", tty, err)
        return False, f"`do script` failed: {err}"

    logger.info("Stage 1: injected %r into %s via `do script`", text, tty)
    time.sleep(SEND_SETTLE_DELAY)
    after = read_content(tty, "Terminal") or ""

    if _text_pending_in_prompt(after, text):
        # ── Stage 2: the text landed but was never submitted ───────────
        logger.info("Stage 2: %r is in the input box unsubmitted; sending bare Return", text)
        ok2, _, err2 = _run_applescript_raw(f'''
        tell application "Terminal"
            do script "" in tab {ti} of window {wi}
        end tell
        ''')
        if not ok2:
            logger.error("Stage 2 (bare Return) failed on %s: %s", tty, err2)
            return False, f"Text was typed but the Return failed: {err2}"

        time.sleep(SEND_SETTLE_DELAY)
        if _text_pending_in_prompt(read_content(tty, "Terminal") or "", text):
            return False, "Text was typed but Claude Code did not submit it"
        logger.info("Stage 2 succeeded on %s", tty)
        return True, None

    if after != before:
        logger.info("Stage 1 succeeded on %s", tty)
        return True, None

    # Nothing visibly changed. Give the UI one more beat before concluding the
    # send didn't land — re-sending would risk duplicating the prompt.
    time.sleep(SEND_SETTLE_DELAY)
    after = read_content(tty, "Terminal") or ""
    if _text_pending_in_prompt(after, text):
        logger.info("Stage 2 (delayed): %r still in the input box; sending bare Return", text)
        _run_applescript_raw(f'''
        tell application "Terminal"
            do script "" in tab {ti} of window {wi}
        end tell
        ''')
        time.sleep(SEND_SETTLE_DELAY)
        if _text_pending_in_prompt(read_content(tty, "Terminal") or "", text):
            return False, "Text was typed but Claude Code did not submit it"
        return True, None

    if after != before:
        logger.info("Stage 1 succeeded on %s (delayed redraw)", tty)
        return True, None

    logger.error("`do script` produced no change on %s", tty)
    return False, "Sent the text but the terminal did not react — is the session still alive?"


def send_text_detailed(tty: str, app: str, text: str) -> tuple[bool, Optional[str]]:
    """
    Send text followed by Enter to a terminal tab/session identified by TTY.

    For Terminal.app: injects input via `do script` and verifies it by re-reading
    the session. For iTerm2: uses the `write text` AppleScript command.

    Returns (success, error message).
    """
    if app == "iTerm2":
        # iTerm2 has a reliable `write text` command
        escaped_text = _escape_applescript(text)
        script = f'''
        tell application "iTerm2"
            repeat with w in windows
                repeat with t in tabs of w
                    repeat with s in sessions of t
                        if tty of s is "{tty}" then
                            tell s to write text "{escaped_text}"
                            return "ok"
                        end if
                    end repeat
                end repeat
            end repeat
            return "not_found"
        end tell
        '''
        result = _run_applescript(script)
        if result == "ok":
            return True, None
        if result == "not_found":
            return False, "iTerm2 session not found (closed?)"
        return False, "iTerm2 `write text` failed"

    elif app == "Terminal":
        return _send_terminal(tty, text)

    return False, f"Unsupported terminal app: {app}"


def send_text(tty: str, app: str, text: str) -> bool:
    """Send text + Enter to a terminal. Returns True on success."""
    ok, _ = send_text_detailed(tty, app, text)
    return ok
