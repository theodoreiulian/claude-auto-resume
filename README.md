# Claude Auto-Resume

A lightweight macOS menu bar app that automatically resumes your Claude Code session when you hit the 5-hour session limit — in Terminal.app or in [Conductor](https://conductor.build).

## What It Does

When Claude Code hits its session limit, it displays a message like:

```
You've hit your session limit · resets 5:00 PM EDT
```

Claude Auto-Resume watches your session for this message, extracts the reset time, and automatically sends `continue` one minute after the session resets — so your work picks up without you having to babysit it.

## Quick Start

```bash
# 1. Setup (one time)
chmod +x setup.sh
./setup.sh

# 2. Run
./run.sh
```

That's it. A 👁‍🗨 icon appears in your menu bar.

## How to Use

1. **Click the menu bar icon** (👁‍🗨) → **Watch Session**
2. **Select the session** running Claude Code — a Terminal.app tab or a Conductor
   workspace, grouped by which app they're in
3. **Done.** The app will:
   - Poll the session every 15 seconds
   - Detect the session limit message when it appears
   - Automatically send `continue` at the reset time + 1 minute
   - Show a macOS notification when it resumes your session

### Menu Bar States

| Icon | Meaning |
|------|---------|
| 👁‍🗨 | No sessions being watched |
| 👁 | Watching session(s), no limit detected |
| ⏳ | Session limit detected, waiting to resume |

### Active Watches

Click **Active Watches** to see everything you're monitoring and its current status. Click any watch to stop monitoring it.

## Supported Hosts

| Host | How it's watched | Extra permission |
|------|------------------|------------------|
| **Terminal.app** | Reads the visible tab contents over AppleScript | Automation (prompted on first use) |
| **Conductor** | Reads Conductor's local session database | Accessibility (grant by hand — see below) |

Conductor runs Claude Code headlessly rather than in a terminal, so there is no screen
to scrape. Instead the app reads Conductor's own SQLite store and looks for the limit
notice that Claude Code emits, which means detection keeps working while Conductor is
in the background or minimised.

**Conductor caveats:**

- Only **Claude Code** sessions are watched. Conductor can also drive Codex, Cursor and
  OpenCode; those use different limits and are skipped.
- Only the session **currently open in a workspace** is watched — Conductor keeps one
  active session per workspace, and that's the one you see.
- Resuming **takes over the screen briefly**. Conductor renders one workspace at a time
  and has no way to address a background one, so the app brings Conductor to the front,
  switches to the watched workspace if a different one is showing (via the ⌘K palette),
  and types into the composer. Expect a short focus interruption when a resume fires.
- Every step is **verified rather than assumed**: the workspace is confirmed by reading
  the branch name from Conductor's window header, the typed text is read back before
  Return is pressed, and the send is confirmed against Conductor's database. If a
  message ends up anywhere other than the watched session you get a "Resume Failed"
  notification rather than a silent mis-send.
- The app **never writes to Conductor's database** — it opens it read-only.
- **Keep a Conductor window open.** Conductor keeps running after its last window
  is closed, and a closed window can't be reopened programmatically, so resumes
  will fail with a clear message until you reopen it. Minimising is fine.

## Permissions

On first run, macOS will ask you to grant **Automation** permissions so the app can read terminal content and send commands. You'll see a prompt like:

> "python3" wants to control "Terminal". Allow?

Click **OK** — this is required for the app to function. You can manage these in:
**System Settings → Privacy & Security → Automation**

## Requirements

- macOS 10.15+
- Python 3.9+

## Project Structure

```
claude-auto-resume/
├── setup.sh                    # One-command setup
├── run.sh                      # Launch the app
├── requirements.txt            # Python dependencies
└── claude_auto_resume/
    ├── __init__.py
    ├── app.py                  # Menu bar app (rumps)
    ├── targets.py              # Common target type + backend dispatch
    ├── terminal.py             # Terminal.app, over AppleScript
    ├── conductor.py            # Conductor, over its SQLite store + Accessibility
    ├── detector.py             # Session limit message parser
    ├── watcher.py              # Per-target watcher state machine
    └── doctor.py               # Environment/permission check
```

Run the tests with:

```bash
python3 -m unittest discover -s tests
```

## Troubleshooting

### Checking your setup

Run `./doctor.sh` to see every watchable session and confirm permissions are granted.

### "No sessions found"
- Make sure Terminal.app is open with at least one window, or Conductor has a workspace open
- If you just opened the app, click **↻ Refresh**
- Conductor sessions only appear for **Claude Code** — not Codex, Cursor or OpenCode

### App can't read terminal content
- Check **System Settings → Privacy & Security → Automation**
- Make sure your Python/Terminal is allowed to control Terminal.app
- Try removing and re-adding the permission

### Conductor resumes fail
- Grant **Accessibility** to the app you launch `./run.sh` from (usually Terminal), in
  **System Settings → Privacy & Security → Accessibility**. Unlike Automation, macOS
  won't prompt for this — you have to add it yourself. `./doctor.sh` will tell you
  whether it's granted.
- Clear any half-written text out of the composer — the app won't overwrite a draft.
- Keep the Conductor window open. The app can switch workspaces for you, but it can't
  reopen a closed window.

### "continue" not being sent
- Make sure the terminal tab or Conductor workspace is still open
- Check that the Claude Code session is actually waiting for input
- The app sends `continue` at reset time + 1 minute to ensure the session has fully reset

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and guidelines.

## License

[MIT](LICENSE)
