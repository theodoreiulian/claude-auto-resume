#!/bin/bash
# setup.sh — One-command setup for Claude Auto-Resume
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

echo "🔧 Claude Auto-Resume — Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Check Python 3
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is required but not found. Install it from https://python.org"
    exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "✓ Found Python $PYTHON_VERSION"

# Create virtual environment
if [ -d "$VENV_DIR" ]; then
    echo "✓ Virtual environment already exists"
else
    echo "→ Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
    echo "✓ Virtual environment created"
fi

# Install dependencies
echo "→ Installing dependencies..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"
echo "✓ Dependencies installed"

# Make run.sh executable
chmod +x "$SCRIPT_DIR/run.sh" "$SCRIPT_DIR/doctor.sh"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Setup complete!"
echo ""
echo "To start the app, run:"
echo "  ./run.sh"
echo ""
echo "To check permissions and see what can be watched, run:"
echo "  ./doctor.sh"
echo ""
echo "On first run, macOS will ask for Automation permissions"
echo "for Terminal.app. Grant them to allow the"
echo "app to read terminal content and send commands."
echo ""
echo "To auto-resume Conductor sessions you must also grant"
echo "Accessibility to whichever app you launch ./run.sh from"
echo "(usually Terminal), in System Settings > Privacy &"
echo "Security > Accessibility. macOS does not prompt for this."
