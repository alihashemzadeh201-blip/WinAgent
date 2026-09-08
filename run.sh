#!/usr/bin/env bash
# Launcher for a POSIX shell. On macOS/Linux, pass --demo explicitly to explore the simulated desktop.
set -e
cd "$(dirname "$0")"
if [ ! -x ".venv/bin/python" ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python -m winagent "$@"
