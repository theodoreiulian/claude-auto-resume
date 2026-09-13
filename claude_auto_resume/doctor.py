"""
doctor.py — Check that everything Claude Auto-Resume needs is in place.

Run with `./doctor.sh` (or `python3 -m claude_auto_resume.doctor`). Useful because
the interesting failure modes are all environmental — a missing permission, an app
that isn't running — and they otherwise only show up the moment a session hits its
limit, which is exactly when nobody is watching.
"""

import sys

from . import conductor, terminal
from .targets import TargetKind, list_targets

OK = "✅"
WARN = "⚠️ "
FAIL = "❌"


def main() -> int:
    print("Claude Auto-Resume — environment check")
    print("━" * 46)

    targets = list_targets()
    terminals = [t for t in targets if t.kind is TargetKind.TERMINAL]
    sessions = [t for t in targets if t.kind is TargetKind.CONDUCTOR]

    # ── Terminal.app ───────────────────────────────────────────────────
    print("\nTerminal.app")
    if terminals:
        print(f"  {OK} {len(terminals)} tab(s) visible")
        for t in terminals:
            print(f"     • {t.menu_label}")
    else:
        print(f"  {WARN} No tabs found — is Terminal.app open?")
        print("     If it is, grant Automation permission in System Settings →")
        print("     Privacy & Security → Automation.")

    # ── Conductor ──────────────────────────────────────────────────────
    print("\nConductor")
    if not conductor.is_available():
        print(f"  {WARN} Not installed (no database at {conductor.CONDUCTOR_DB})")
    else:
        print(f"  {OK} Installed")
        if sessions:
            print(f"  {OK} {len(sessions)} Claude Code / Codex session(s) open")
            for t in sessions:
                limited = conductor.read_content(t.ref)
                state = f"limited — {limited}" if limited else "no limit detected"
                print(f"     • {t.menu_label} — {state}")

            if not conductor._has_open_window():
                print(f"  {FAIL} Conductor has no open window — resumes will fail.")
                print("     Reopen it and leave it open; a closed window can't be")
                print("     restored automatically.")
            else:
                displayed = [
                    s.workspace_name for s in conductor.list_sessions()
                    if conductor._workspace_is_displayed(s)
                ]
                if displayed:
                    print(f"  {OK} Workspace open in Conductor: {', '.join(displayed)}")
                else:
                    print(f"  {WARN} Could not tell which workspace Conductor has open —")
                    print("     a resume will try to switch to it with \u2318K.")
        else:
            print(f"  {WARN} No open Claude Code or Codex sessions")
            print("     Cursor and OpenCode sessions are not supported.")

        if conductor.has_accessibility_permission():
            print(f"  {OK} Accessibility permission granted (needed to send 'continue')")
        else:
            print(f"  {FAIL} Accessibility permission missing — auto-resume will fail.")
            print("     Grant it in System Settings → Privacy & Security → Accessibility,")
            print("     adding the app you launch ./run.sh from (usually Terminal).")

    print("\n" + "━" * 46)
    return 0


if __name__ == "__main__":
    sys.exit(main())
