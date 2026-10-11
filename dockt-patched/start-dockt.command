#!/bin/bash
# Dockt on macOS: double-click this in Finder, or run ./start-dockt.command [--mock] [--no-capture].
# The Mac version of start-dockt.bat: the first run creates .venv and installs the requirements.
# macOS asks once (for Terminal) for Screen Recording, Accessibility (window titles) and Microphone (voice).
cd "$(dirname "$0")" || exit 1

fail() { echo; echo "Dockt could not start: $1"; read -r -p "Press Return to close."; exit 1; }

if [ ! -x .venv/bin/python ]; then
  PY="${PYTHON:-python3}"
  "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null ||
    fail "Dockt needs Python 3.11 or newer (macOS's built-in python3 is older). Install it from python.org, or set PYTHON=/path/to/python3.12."
  echo "Creating the Dockt Python environment..."
  "$PY" -m venv .venv || fail "couldn't create .venv"
fi

# the quick chat and the pet are Tk windows; Homebrew's Python leaves Tk out
.venv/bin/python -c 'import tkinter' 2>/dev/null ||
  fail "this Python has no Tk. Use Python from python.org, or run: brew install python-tk@3.12 (match your version), then delete .venv and start again."

# keep the marker in step with start-dockt.bat, so both reinstall when requirements change
if [ ! -f .venv/.dockt-deps-installed-v6 ]; then
  echo "Installing Dockt desktop requirements. This can take a few minutes the first time..."
  .venv/bin/python -m pip install -r requirements-server.txt -r requirements-agent.txt -r requirements-desktop.txt ||
    fail "installing the requirements failed (see above)"
  touch .venv/.dockt-deps-installed-v6
fi

echo "Starting the Dockt desktop app, capture service and quick chat..."
exec .venv/bin/python desktop/dockt_desktop.py "$@"
