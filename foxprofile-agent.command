#!/bin/bash
# FoxProfile agent: opens the hub's profiles on this Mac. Leave this window open.
# First run installs everything (needs Python 3.10+ from python.org) and asks for the hub and your user.
set -e
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -m pip install -q -r requirements.txt
if [ ! -f .venv/camoufox-fetched ]; then
    .venv/bin/python -m camoufox fetch
    touch .venv/camoufox-fetched
fi
exec .venv/bin/python -m src.agent
