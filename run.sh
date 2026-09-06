#!/usr/bin/env bash
# Development launcher for macOS/Linux (runs with the simulated desktop backend).
set -e
cd "$(dirname "$0")"
if [ ! -x ".venv/bin/python" ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python -m winagent --demo "$@"
