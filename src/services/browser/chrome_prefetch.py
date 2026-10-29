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
