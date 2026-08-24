# Claude Auto-Resume

A lightweight macOS menu bar app that automatically resumes your Claude Code session when you hit the 5-hour session limit.

## What It Does

When Claude Code hits its session limit, it displays a message like:

```
You've hit your session limit · resets 5:00 PM EDT
```

Claude Auto-Resume watches your terminal for this message, extracts the reset time, and automatically sends `continue` one minute after the session resets — so your work picks up without you having to babysit it.

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

1. **Click the menu bar icon** (👁‍🗨) → **Watch Terminal**
2. **Select the terminal** running your Claude Code session
3. **Done.** The app will:
   - Poll the terminal every 60 seconds
   - Detect the session limit message when it appears
   - Automatically send `continue` at the reset time + 1 minute
   - Show a macOS notification when it resumes your session

### Menu Bar States

| Icon | Meaning |
|------|---------|
| 👁‍🗨 | No terminals being watched |
| 👁 | Watching terminal(s), no limit detected |
| ⏳ | Session limit detected, waiting to resume |

### Active Watches

Click **Active Watches** to see all terminals you're monitoring and their current status. Click any watch to stop monitoring it.

## Supported Terminals

Only macOS's built-in **Terminal.app** is supported.

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
    ├── terminal.py             # AppleScript terminal interaction
    ├── detector.py             # Session limit message parser
    └── watcher.py              # Per-terminal watcher state machine
```

## Troubleshooting

### "No terminals found"
- Make sure Terminal.app is open with at least one window
- If you just opened Terminal.app, click **↻ Refresh**

### App can't read terminal content
- Check **System Settings → Privacy & Security → Automation**
- Make sure your Python/Terminal is allowed to control Terminal.app
- Try removing and re-adding the permission

### "continue" not being sent
- Make sure the terminal tab is still open
- Check that the Claude Code session is actually waiting for input
- The app sends `continue` at reset time + 1 minute to ensure the session has fully reset

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and guidelines.

## License

[MIT](LICENSE)
