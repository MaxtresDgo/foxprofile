import os
import subprocess
import sys

import pytest

from src.services.browser import taskbar


def test_icon_has_the_number_at_every_size(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    from PIL import Image

    monkeypatch.setattr(taskbar.tempfile, "gettempdir", lambda: str(tmp_path))
    path = taskbar.icon_file("5672", "shop-01")
    sizes = Image.open(path).info["sizes"]
    assert {(s, s) for s in taskbar._SIZES} <= set(sizes)
    # The plate is drawn: the bottom of the icon is the badge violet, not the logo.
    assert taskbar.draw("5672", 64).getpixel((2, 60))[:3] == taskbar._PLATE[:3]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows taskbar")
def test_finds_the_processes_started_under_it():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        assert child.pid in taskbar._descendants(os.getpid())
    finally:
        child.kill()
