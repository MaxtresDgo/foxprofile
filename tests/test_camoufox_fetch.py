"""The agent reads Camoufox's download progress from these lines."""

from src.services.browser import camoufox_fetch


def test_downloads_report_progress_lines(monkeypatch, capsys):
    calls = []

    def fake_webdl(url, desc=None, buffer=None, bar=True, progress_callback=None):
        calls.append(bar)
        progress_callback(50, 200)
        progress_callback(200, 200)
        return buffer

    monkeypatch.setattr(camoufox_fetch, "_webdl", fake_webdl)
    camoufox_fetch._webdl_with_progress("https://example.invalid/camoufox.zip")
    assert capsys.readouterr().out.splitlines() == ["PROGRESS:50:200", "PROGRESS:200:200"]
    assert calls == [False]  # no terminal bar under the agent
