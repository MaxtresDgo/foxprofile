"""Chrome engine builds: which one a profile may run, and installing them in
the background, one thread per version.

A Chrome profile only opens once its build is installed (routes/browser.py);
the panels' Install button starts it here and shows the progress, which this
module keeps for every download running in this process.
"""

from __future__ import annotations

import pathlib
import threading
from collections.abc import Callable

from ...core.config import DATA_DIR
from ...core.logging import get_logger
from ...core.strings import get_string
from ...models.profile import Profile

logger = get_logger("browser.chrome_prefetch")

_lock = threading.Lock()
# version -> percent downloaded (-1 before the size is known), while it installs.
_progress: dict[str, int] = {}
# version -> why its last install failed, until it is tried again.
_errors: dict[str, str] = {}


def unsupported_reason() -> str | None:
    """Why this machine cannot run the Chrome engine, or None when it can."""
    from chrome_engine.release import UnsupportedPlatformError, host_platform

    try:
        host_platform()
    except UnsupportedPlatformError as e:
        return str(e)
    return None


def host() -> str | None:
    """This machine's build platform (windows-x64, linux-x64, macos), if it has one."""
    from chrome_engine.release import UnsupportedPlatformError, host_platform

    try:
        return host_platform()
    except UnsupportedPlatformError:
        return None


def version_problem(version: str, here: bool = True) -> str | None:
    """Why a profile cannot run build `version`, or None. `here`: the browser runs
    on this machine (on a hub the panel offers what the operator's agent can install)."""
    from chrome_engine import release

    if version not in release.RELEASES:
        return get_string("err_chrome_version", values=", ".join(release.RELEASES))
    if here and version not in release.available(host()):
        return get_string("err_chrome_version_host", version=version)
    return None


def upgrade_problem(
    profile: Profile, version: str, running: bool, here: bool = True
) -> tuple[int, str] | None:
    """(HTTP status, message) when `profile` cannot move to build `version`. It only
    moves to a newer one, while stopped: Chromium cannot read a newer build's data."""
    from chrome_engine import release

    if profile.engine != "chrome":
        return 400, get_string("err_chrome_version_engine")
    if problem := version_problem(version, here):
        return 400, problem
    if release.key(version) <= release.key(profile.chrome_version or release.LEGACY_VERSION):
        return 400, get_string("err_chrome_downgrade", version=profile.chrome_version)
    if running:
        return 409, get_string("err_chrome_version_running")
    return None


def move_to(pm, name: str, version: str) -> None:
    """Switch a stopped profile to a newer build (checked with upgrade_problem)."""
    from chrome_engine import persona as personas

    pm.set_chrome_version(name, version)
    # An android persona's User-Agent names the Chrome it runs.
    personas.retarget(pathlib.Path(DATA_DIR) / name, version)


def downgrade_problem(
    profile: Profile, version: str, running: bool, here: bool = True
) -> tuple[int, str] | None:
    """(HTTP status, message) when `profile` cannot go back to the older build `version`."""
    from chrome_engine import release

    if profile.engine != "chrome":
        return 400, get_string("err_chrome_version_engine")
    if problem := version_problem(version, here):
        return 400, problem
    if release.key(version) >= release.key(profile.chrome_version or release.LEGACY_VERSION):
        return 400, get_string("err_chrome_not_older", version=profile.chrome_version)
    if running:
        return 409, get_string("err_chrome_version_running")
    return None


class DowngradeError(Exception):
    """Why a downgrade did not happen; the profile is left as it was."""


def downgrade(pm, name: str, version: str, here: bool = True) -> None:
    """Move a stopped Chrome profile back to the older build `version` (checked with
    downgrade_problem). Chromium cannot read a newer build's data, so that data is
    dropped: Fox's own files (persona, tabs) stay and the cookies carry over.

    `here`: this machine runs the browsers, so the cookies move now, and a failure
    puts everything back. On a hub the agent loads the cookies it packed on the
    profile's last close (agent._restore_cookies) the next time it opens it.
    A snapshot (if they are on) is taken first, so it can be undone."""
    import json
    import shutil
    import tempfile

    from ..profile import snapshots
    from ..profile.transfer import COOKIES_GZ, COOKIES_SHA
    from . import cookies
    from .fingerprint import CHROME_PERSONA_FILE
    from .session import TABS_FILE

    profile = pm.profiles[name]
    old = profile.chrome_version
    folder = pathlib.Path(DATA_DIR) / name
    keep = {CHROME_PERSONA_FILE, TABS_FILE, COOKIES_GZ, COOKIES_SHA}
    chromium = [p for p in folder.iterdir() if p.name not in keep] if folder.is_dir() else []
    if here and not is_installed(version):
        raise DowngradeError(get_string("err_chrome_build_missing", version=version))
    if not chromium:  # never opened: nothing to drop or carry over
        move_to(pm, name, version)
        return
    text = None
    if here:
        try:
            text, _ = cookies.export_cookies(name, profile.os_type, "json", "chrome", old)
        except cookies.CookieError as e:
            raise DowngradeError(get_string("err_chrome_downgrade_cookies", error=e)) from e
    elif not (folder / COOKIES_GZ).exists():
        raise DowngradeError(get_string("err_chrome_downgrade_unpacked"))
    if snapshots.enabled():
        try:
            snapshots.create(profile, str(folder))
        except Exception as e:
            raise DowngradeError(get_string("err_snapshot_store", error=e)) from e

    # Set aside, not deleted, until the cookies are in: a failure puts it back.
    aside = pathlib.Path(tempfile.mkdtemp(prefix=".downgrade-", dir=folder.parent))
    for p in chromium:
        p.rename(aside / p.name)
    move_to(pm, name, version)
    try:
        if text and json.loads(text):
            cookies.import_cookies(name, profile.os_type, text, "chrome", version)
    except Exception as e:
        for p in folder.iterdir():
            if p.name in keep:
                continue
            if p.is_dir():  # what the older build just wrote
                shutil.rmtree(p)
            else:
                p.unlink()
        for p in aside.iterdir():
            p.rename(folder / p.name)
        aside.rmdir()
        move_to(pm, name, old)
        raise DowngradeError(get_string("err_chrome_downgrade_cookies", error=e)) from e
    shutil.rmtree(aside, ignore_errors=True)
    logger.info("%s moved back from Chrome %s to %s", name, old, version)


def is_installed(version: str | None = None) -> bool:
    from chrome_engine import fetch

    return fetch.installed_executable(version) is not None


def installing() -> dict[str, int]:
    """The builds installing now: version -> percent (-1 = not known yet)."""
    with _lock:
        return dict(_progress)


def failures() -> dict[str, str]:
    """version -> error of its last install, for the builds that failed."""
    with _lock:
        return dict(_errors)


def start(
    version: str | None = None,
    log: Callable[[str], None] | None = None,
    on_done: Callable[[], None] | None = None,
) -> bool:
    """Start installing `version` (the default build if None) unless it is
    installed or already installing. `on_done` runs when it ends, either way.

    Returns True if a download was started.
    """
    from chrome_engine.release import get_release

    version = get_release(version).version
    with _lock:
        if version in _progress:
            return False
        try:
            if is_installed(version):
                return False
        except Exception as e:  # unsupported host, unreadable cache dir...
            logger.warning("Cannot check the Chrome engine install: %s", e)
            return False
        _progress[version] = -1
        _errors.pop(version, None)
    threading.Thread(
        target=_run, args=(version, log, on_done), daemon=True, name=f"chrome-{version}"
    ).start()
    return True


def _run(version: str, log: Callable[[str], None] | None, on_done) -> None:
    from chrome_engine import fetch

    def say(message: str) -> None:
        logger.info(message)
        if log:
            log(message)

    def progress(done: int, total: int) -> None:
        with _lock:
            _progress[version] = done * 100 // total if total else -1

    say(get_string("chrome_engine_downloading", version=version))
    try:
        fetch.install(version, progress=progress)
    except Exception as e:
        logger.exception("Chrome engine download failed")
        say(get_string("chrome_engine_download_failed", error=e))
        with _lock:
            _errors[version] = str(e)
    else:
        say(get_string("chrome_engine_ready", version=version))
    finally:
        with _lock:
            _progress.pop(version, None)
        if on_done:
            on_done()
