from src.utils import proxy_checker


class _Response:
    status_code = 200

    @staticmethod
    def json():
        return {"origin": "203.0.113.7"}


def test_socks5_proxy_is_checked_through_socks(monkeypatch):
    # aiohttp used to send HTTP to SOCKS proxies, failing every socks5:// check.
    seen = {}

    def fake_get(url, proxies, timeout):
        seen.update(proxies)
        return _Response()

    monkeypatch.setattr(proxy_checker.requests, "get", fake_get)
    ok, message = proxy_checker.check_proxy_sync("socks5://user:pass@198.51.100.4:1080")
    assert ok and "203.0.113.7" in message
    assert seen["https"] == "socks5h://user:pass@198.51.100.4:1080"
    assert proxy_checker.check_proxy_sync("not a proxy") == (False, "Invalid proxy format")
