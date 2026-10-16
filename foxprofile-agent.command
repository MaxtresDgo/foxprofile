#!/bin/bash
# FoxProfile agent: opens the hub's profiles on this Mac.
# First run installs everything (needs Python 3.10+ from python.org). The agent then
# runs in the menu bar and signs in through your browser; this window can be closed.
set -e
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -m pip install -q -r requirements.txt
if [ ! -f .venv/camoufox-fetched ]; then
    .venv/bin/python -m camoufox fetch
    touch .venv/camoufox-fetched
fi
nohup .venv/bin/python -m src.agent >/dev/null 2>&1 &
echo "FoxProfile Agent is running in the menu bar (fox icon). You can close this window."
