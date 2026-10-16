"""`camoufox fetch` that reports its download progress.

    python -m src.services.browser.camoufox_fetch

Camoufox downloads its ~1.3 GB browser into memory and shows progress only as a
terminal bar. This runs the same command with its progress hook set, printing
`PROGRESS:<done>:<total>` lines that the agent turns into a percentage in its tray.
"""

import sys

from camoufox import pkgman
from camoufox.__main__ import cli

_webdl = pkgman.webdl


def _report(done: int, total: int) -> None:
    print(f"PROGRESS:{done}:{total}", flush=True)


def _webdl_with_progress(url, desc=None, buffer=None, bar=True, progress_callback=None):
    return _webdl(
        url, desc=desc, buffer=buffer, bar=False, progress_callback=progress_callback or _report
    )


def main() -> None:
    # CamoufoxFetcher.download_file looks webdl up in pkgman at call time.
    pkgman.webdl = _webdl_with_progress
    cli(["fetch"])


if __name__ == "__main__":
    sys.exit(main())
