"""
terminal.py — AppleScript-based terminal interaction layer.

Supports Terminal.app and iTerm2. Uses osascript via subprocess to:
- List all open terminal windows/tabs/sessions
- Read visible content from a specific terminal (by TTY)
- Send text input to a specific terminal (by TTY)
"""

import subprocess
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class TerminalInfo:
    """Represents a single terminal tab or session."""
    tty: str           # e.g. "/dev/ttys001"
    app: str           # "Terminal" or "iTerm2"
    name: str          # Window/tab title
    processes: str     # Running processes description


def _run_applescript(script: str, timeout: int = 10) -> Optional[str]:
    """Run an AppleScript and return stdout, or None on error."""
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-"],
            input=script,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


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


def read_content(tty: str, app: str) -> Optional[str]:
    """
    Read the visible content of a terminal tab/session identified by TTY.

    For Terminal.app, reads the `contents` property (visible screen).
    For iTerm2, reads the `contents` property of the matching session.
    """
    if app == "Terminal":
        script = f'''
        tell application "Terminal"
            repeat with w in windows
                repeat with t in tabs of w
                    if tty of t is "{tty}" then
                        return contents of t
                    end if
                end repeat
            end repeat
            return ""
        end tell
        '''
    elif app == "iTerm2":
        script = f'''
        tell application "iTerm2"
            repeat with w in windows
                repeat with t in tabs of w
                    repeat with s in sessions of t
                        if tty of s is "{tty}" then
                            return contents of s
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


def send_text(tty: str, app: str, text: str) -> bool:
    """
    Send text followed by Enter to a terminal tab/session identified by TTY.

    For Terminal.app: writes directly to the TTY device.
    For iTerm2: uses the `write text` AppleScript command.

    Returns True on success, False on failure.
    """
    if app == "iTerm2":
        # iTerm2 has a reliable `write text` command
        escaped_text = text.replace("\\", "\\\\").replace('"', '\\"')
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
        return result == "ok"

    elif app == "Terminal":
        # For Terminal.app, write directly to the TTY device
        # This works reliably for interactive processes (like Claude Code)
        try:
            result = subprocess.run(
                ["bash", "-c", f'echo "{text}" > {tty}'],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    return False
