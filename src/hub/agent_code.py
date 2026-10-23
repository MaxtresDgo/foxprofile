"""Agent updates from the hub: the hub hands its own code to installed agents.

The agent and the hub run the same code (src/ and chrome_engine/), so deploying a
new hub is the update. `code_id` names a tree by its content; an installed agent
whose code differs downloads the hub's (`zip_bytes`), unpacks it into
<home>/agent-code/<id>/ and restarts. Its installer's Python then loads that
code instead of the installed one (installer/agent_update_hook.py, a .pth hook,
so the browsers' runner processes follow too).

Only the code travels: Python and the libraries come with the installer. An
agent whose installer had other requirements (`requirements_id`) gets no code,
it is told to install the new installer, which the panel serves.
"""

from __future__ import annotations

import functools
import hashlib
import io
import os
import pathlib
import shutil
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKAGES = ("src", "chrome_engine")
# Not shipped by the installers either (tests) or never part of the code (caches,
# the bundled browser).
_SKIP_DIRS = {"__pycache__", "tests", "e2e", "builds"}
# The installed app (installer/*): <prefix>/../app, with a BUILD file naming its code.
INSTALLED = pathlib.Path(sys.prefix).parent / "app"
_DATE = (2020, 1, 1, 0, 0, 0)


def _files(root: pathlib.Path) -> list[str]:
    found = []
    for package in PACKAGES:
        for dirpath, dirnames, filenames in os.walk(root / package):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            rel = pathlib.Path(dirpath).relative_to(root)
            found += [
                (rel / f).as_posix()
                for f in filenames
                if not f.endswith(".pyc") and f != ".DS_Store" and not f.startswith("._")
            ]
    return sorted(found)


def code_id(root: pathlib.Path = ROOT) -> str:
    digest = hashlib.sha256()
    for name in _files(root):
        digest.update(name.encode() + b"\0" + (root / name).read_bytes() + b"\0")
    return digest.hexdigest()[:16]


@functools.cache
def hub_code() -> tuple[str, bytes]:
    """This hub's code id and the ZIP agents download (built once per process)."""
    return code_id(), zip_bytes(ROOT)


def zip_bytes(root: pathlib.Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in _files(root):
            z.writestr(zipfile.ZipInfo(name, _DATE), (root / name).read_bytes())
    return buf.getvalue()


def requirements_id(text: str) -> str:
    """Names a requirements.txt by its requirements, ignoring comments and order."""
    lines = sorted({line.split("#")[0].strip() for line in text.splitlines()} - {""})
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


def hub_requirements() -> str | None:
    try:
        return requirements_id((ROOT / "requirements.txt").read_text(encoding="utf-8"))
    except OSError:
        return None


def installed_version() -> str:
    """The installer's own version (installer/agent-version.sh), or "" when this
    agent was not installed by one."""
    try:
        return (INSTALLED / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def installed_requirements() -> str | None:
    """The installer's requirements, or None when this agent was not installed by
    one (a script or a git checkout updates itself some other way)."""
    try:
        (INSTALLED / "BUILD").read_text()
        return requirements_id((INSTALLED / "requirements.txt").read_text(encoding="utf-8"))
    except OSError:
        return None


def unpack(data: bytes, code: str, home: pathlib.Path) -> pathlib.Path:
    """Unpack an update into home/agent-code/<code> and make it the one the hook
    loads. Raises ValueError when the ZIP is not that code."""
    base = home / "agent-code"
    base.mkdir(parents=True, exist_ok=True)
    target = base / code
    tmp = base / f".{code}-{os.getpid()}"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            parts = pathlib.PurePosixPath(name).parts
            if name.startswith("/") or ".." in parts or parts[0] not in PACKAGES:
                raise ValueError(name)
        z.extractall(tmp)
    if code_id(tmp) != code:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError(code)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    tmp.rename(target)
    build = (INSTALLED / "BUILD").read_text().strip()
    (base / "current").write_text(f"{code} {build}\n")
    for old in base.iterdir():  # earlier updates; the running one is already loaded
        if old.is_dir() and old.name != code:
            shutil.rmtree(old, ignore_errors=True)
    return target
