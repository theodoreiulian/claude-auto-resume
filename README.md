# Claude Auto-Resume

A lightweight macOS menu bar app that automatically resumes your Claude Code or Codex session when you hit its usage limit — in Terminal.app, in [Conductor](https://conductor.build), or in the
Codex desktop app (the ChatGPT app's **Work** mode).

## What It Does

When Claude Code hits its session limit, it displays a message like:

```
You've hit your session limit · resets 5:00 PM EDT
```

Codex says it like this:

```
You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit
https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 11:54 PM.
```

Claude Auto-Resume watches your session for either message, extracts the reset time, and automatically sends `continue` one minute after the limit resets — so your work picks up without you having to babysit it.

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
2. **Select the session** running Claude Code or Codex — a Terminal.app tab, a
   Conductor workspace or a Codex app thread, grouped by which app they're in
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
| **Codex app** | Reads Codex's local thread database | None |

Conductor runs agents headlessly rather than in a terminal, so there is no screen
to scrape. Instead the app reads Conductor's own SQLite store and looks for the limit
notice the agent emits — a synthetic message from Claude Code, a turn error from
Codex — which means detection keeps working while Conductor is in the background or
minimised.

**Codex notes:**

- Codex gives the reset in your Mac's local time, and adds the date when it isn't
  today (`try again at Sep 10th, 2026 2:22 AM`). A dated reset that has already passed
  resumes straight away.
- A notice that ends `try again later.` has no time to schedule against, so it's not
  acted on.
- In Terminal.app, the Codex CLI may open an **Approaching rate limits** prompt offering
  to switch to a cheaper model. It swallows typed text and Return would accept the
  switch, so the app dismisses it with Esc — never accepting it — before typing
  `continue`.

**Conductor caveats:**

- Only **Claude Code** and **Codex** sessions are watched. Conductor can also drive
  Cursor and OpenCode; those are skipped.
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

**Codex app caveats:**

- This is the Codex that lives in the ChatGPT desktop app's **Work** mode. The menu
  lists the 20 most recently active threads **started in the app**. Codex sessions
  from Conductor or the Codex CLI share the same store, but they're watched through
  their own hosts.
- A limit is read from the thread's own record of its latest turn: a turn that failed
  with `usageLimitExceeded`. A thread that only *mentions* the limit message can't
  trigger a resume.
- `continue` is sent through the app's local IPC socket (`~/.codex/ipc/ipc.sock`),
  the same channel the app's windows use to hand each other messages. It shows up
  in the thread as an ordinary message from you. Nothing is typed, and no
  Accessibility permission is needed.
- **The app must be running** when a resume fires. It doesn't need to be in front,
  and the thread doesn't need to be the one on screen. If the thread isn't loaded,
  the app is asked to open it (`codex://threads/<id>`). That can briefly bring the
  app forward; if it does, focus is handed back to the app you were using.
- The IPC protocol is internal to the Codex app and could change in an update. If it
  does, you get a "Resume Failed" notification that says so, rather than a silent
  miss.

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
    ├── codex_app.py            # Codex desktop app, over its SQLite store + IPC socket
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
- Codex app threads appear only if they were started in the app (not in Conductor or
  the CLI), and only the 20 most recently active are listed
- If you just opened the app, click **↻ Refresh**
- Conductor sessions only appear for **Claude Code** and **Codex** — not Cursor or OpenCode

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

### Codex app resumes fail
- Keep the Codex (ChatGPT) app running. Quitting it closes the socket the app sends
  through.
- A "rejected the message: request-version-mismatch" error means the Codex app was
  updated in a way this app doesn't support yet.

### "continue" not being sent
- Make sure the terminal tab, Conductor workspace or Codex thread is still open
- Check that the Claude Code or Codex session is actually waiting for input
- The app sends `continue` at reset time + 1 minute to ensure the session has fully reset

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and guidelines.

## License

[MIT](LICENSE)
