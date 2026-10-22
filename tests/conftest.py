import pytest


@pytest.fixture(autouse=True)
def _no_chrome_download(monkeypatch):
    """Creating a Chrome profile starts a ~150 MB background download of the Chrome
    engine (chrome_prefetch). In a test it would also install the browser that
    chrome_engine/tests then run against. Tests of the prefetch itself set
    is_installed to False."""
    from src.services.browser import chrome_prefetch

    monkeypatch.setattr(chrome_prefetch, "is_installed", lambda: True)
