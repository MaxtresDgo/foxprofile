"""Installed as _foxprofile_update.py (+ foxprofile_update.pth) in the installers'
site-packages, so it runs at every start of their Python: the agent, its browser
runners, the log window. It puts the code the hub sent (src/hub/agent_code.py)
ahead of the installed code, as long as it was unpacked for this very install:
after a new installer, its own code runs until the hub sends a newer one.

Stdlib only and never raises: it decides which `src` gets imported.
"""

import contextlib
import os
import sys


def _home() -> str:
    # Same folder as src/agent.py --home.
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "FoxProfile")


def activate(home: str, app: str) -> str | None:
    """The update to run, put first on sys.path, or None to run the installed code."""
    try:
        with open(os.path.join(home, "agent-code", "current"), encoding="utf-8") as f:
            code, base = f.read().split()
        with open(os.path.join(app, "BUILD"), encoding="utf-8") as f:
            build = f.read().strip()
    except (OSError, ValueError):
        return None
    target = os.path.join(home, "agent-code", code)
    if base != build or not os.path.isfile(os.path.join(target, "src", "agent.py")):
        return None
    sys.path.insert(0, target)
    # The update carries no browser build: keep using the installed one.
    os.environ.setdefault("FOXPROFILE_CHROME_BUILDS", os.path.join(app, "chrome_engine", "builds"))
    return target


with contextlib.suppress(Exception):  # a broken hook must not stop Python
    activate(_home(), os.path.join(os.path.dirname(sys.prefix), "app"))
