#!/bin/bash
# run.sh — Launch Claude Auto-Resume menu bar app
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "❌ Virtual environment not found. Run ./setup.sh first."
    exit 1
fi

exec "$VENV_DIR/bin/python3" -m claude_auto_resume.app
