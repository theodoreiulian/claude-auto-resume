# Contributing

Thanks for considering a contribution to Claude Auto-Resume.

## Development setup

```bash
./setup.sh
./run.sh
```

## Project structure

See the "Project Structure" section in [README.md](README.md).

## Submitting changes

1. Fork the repo and create a branch from `main`.
2. Make your change, keeping it focused and scoped.
3. Run `python3 -m unittest discover -s tests`.
4. Test manually against Terminal.app, Conductor and the Codex app if your change
   touches `terminal.py`, `conductor.py`, `codex_app.py`, `targets.py`,
   `detector.py`, or `watcher.py`.
   `./doctor.sh` lists what the app can currently see.
5. Open a pull request describing what changed and why.

## Reporting issues

Use the bug report template and include your macOS version, terminal app, and Python version.
