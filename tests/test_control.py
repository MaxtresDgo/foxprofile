import asyncio

import pytest

from src.services.browser.control import ControlError, PageController, _check_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("example.com", "https://example.com"),
        ("http://a.vn/x", "http://a.vn/x"),
        ("about:blank", "about:blank"),
        ("localhost:8080/x", "https://localhost:8080/x"),
    ],
)
def test_allowed_urls(url, expected):
    assert _check_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/win.ini",
        "view-source:https://a.com",
        "javascript:alert(1)",
        "chrome://x",
        "about:config",
        "about:logins",
        "data:text/html,<script>alert(1)</script>",
    ],
)
def test_blocked_urls(url):
    with pytest.raises(ControlError):
        _check_url(url)


class FakePage:
    def __init__(self, url="about:blank"):
        self.url = url
        self.closed = False

    def is_closed(self):
        return self.closed

    async def title(self):
        return "T"

    async def goto(self, url, wait_until="load"):
        self.url = url

    async def close(self):
        self.closed = True

    async def bring_to_front(self):
        pass


class FakeContext:
    def __init__(self, n=1):
        self.pages = [FakePage(f"https://p{i}.test/") for i in range(n)]

    async def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page


def run(coro):
    return asyncio.run(coro)


def test_dispatch_rejects_unknown_action_and_bad_params():
    c = PageController(FakeContext())
    with pytest.raises(ControlError, match="Unknown action"):
        run(c.dispatch("rm_rf", {}))
    with pytest.raises(ControlError, match="Invalid parameters"):
        run(c.dispatch("navigate", {"nope": 1}))


def test_navigate_uses_active_tab():
    ctx = FakeContext(2)
    c = PageController(ctx)
    result = run(c.dispatch("navigate", {"url": "example.com"}))
    assert result == {"url": "https://example.com", "title": "T"}
    assert ctx.pages[-1].url == "https://example.com"


def test_tabs_select_and_close():
    ctx = FakeContext(2)
    c = PageController(ctx)
    tabs = run(c.dispatch("tab_select", {"index": 0}))["tabs"]
    assert [t["active"] for t in tabs] == [True, False]
    tabs = run(c.dispatch("tab_close", {"index": 0}))["tabs"]
    assert [t["url"] for t in tabs] == ["https://p1.test/"]
    with pytest.raises(ControlError, match="last tab"):
        run(c.dispatch("tab_close", {"index": 0}))
    with pytest.raises(ControlError, match="No tab"):
        run(c.dispatch("tab_select", {"index": 5}))


# ---------------------------------------------------------------------------
# Percent-encoded scheme bypass (fix 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "%66ile:///C:/Windows/win.ini",  # decodes to file:///...
        "java%73cript:alert(1)",  # decodes to javascript:...
        "%76iew-source:https://a.com",  # decodes to view-source:...
    ],
)
def test_percent_encoded_blocked_schemes(url):
    """Percent-encoded disallowed schemes must be caught before scheme detection."""
    with pytest.raises(ControlError):
        _check_url(url)


@pytest.mark.parametrize("url", ["[", "[]", "[::1", "http://[::1"])
def test_malformed_url_is_a_control_error(url):
    with pytest.raises(ControlError):
        _check_url(url)


# ---------------------------------------------------------------------------
# Sync: select, replay guard, recorded-step cleaning
# ---------------------------------------------------------------------------


class FakeLocator:
    def __init__(self, log):
        self.first = self
        self._log = log

    async def click(self, timeout=None):
        self._log.append("click")

    async def select_option(self, value, timeout=None):
        self._log.append(("select", value))


def _with_locators(ctx):
    log = []
    for page in ctx.pages:
        page.locator = lambda selector, log=log: FakeLocator(log)
    return log


def test_select_picks_an_option():
    ctx = FakeContext()
    log = _with_locators(ctx)
    run(PageController(ctx).dispatch("select", {"selector": "#c", "value": "VN"}))
    assert log == [("select", "VN")]


def test_replay_only_on_the_leaders_origin_and_only_safe_actions():
    ctx = FakeContext()  # active tab: https://p0.test/
    log = _with_locators(ctx)
    c = PageController(ctx)
    click = {"action": "click", "params": {"selector": "#go"}}
    run(c.dispatch("replay", {"step": click, "origin": "https://p0.test"}))
    assert log == ["click"]
    with pytest.raises(ControlError, match="Not replayed"):
        run(c.dispatch("replay", {"step": click, "origin": "https://evil.test"}))
    evaluate = {"action": "evaluate", "params": {"script": "1"}}
    with pytest.raises(ControlError, match="cannot be replayed"):
        run(c.dispatch("replay", {"step": evaluate, "origin": "https://p0.test"}))
    with pytest.raises(ControlError, match="Invalid parameters"):
        bad = {"action": "click", "params": {"selector": "#a", "x": 1}}
        run(c.dispatch("replay", {"step": bad, "origin": "https://p0.test"}))


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        ({"action": "click", "params": {"selector": "#a", "x": 1}}, {"selector": "#a"}),
        ({"action": "press", "params": {"key": "Enter"}}, {"key": "Enter"}),
        ({"action": "type", "params": {"selector": "#a"}}, None),  # text missing
        ({"action": "type", "params": {"selector": "#a", "text": 5}}, None),
        ({"action": "evaluate", "params": {"script": "x"}}, None),
        ({"action": "click", "params": {"selector": "x" * 20_001}}, None),
        ("click", None),
    ],
)
def test_recorded_steps_are_cleaned(step, expected):
    from src.services.browser.recorder import clean_step

    cleaned = clean_step(step)
    assert (cleaned and cleaned["params"]) == expected


def _camoufox_installed():
    try:
        from camoufox.pkgman import installed_verstr

        installed_verstr()
        return True
    except Exception:
        return False


FORM = b"""<!doctype html><title>f</title>
<input id="email"><select name="country"><option>US</option><option>VN</option></select>
<label><input type="checkbox" id="terms"> ok</label><button onclick="this.textContent='done'">Go</button>"""


@pytest.mark.skipif(not _camoufox_installed(), reason="Camoufox not fetched")
def test_recorder_round_trip_in_camoufox():
    import http.server
    import threading

    from camoufox.async_api import AsyncCamoufox

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(FORM)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"

    async def scenario():
        async with AsyncCamoufox(headless=True) as browser:
            leader_ctx, follower_ctx = await browser.new_context(), await browser.new_context()
            leader, follower = await leader_ctx.new_page(), await follower_ctx.new_page()
            await leader.goto(url)
            await follower.goto(url)
            rec = PageController(leader_ctx)
            await rec.dispatch("record_start", {})
            await leader.click("#email")
            await leader.keyboard.type("a@example.com")
            await leader.keyboard.press("Tab")
            await leader.select_option("select", "VN")
            await leader.click("#terms")
            await leader.click("button")
            await asyncio.sleep(0.5)  # bindings deliver asynchronously
            steps = (await rec.dispatch("record_poll", {"timeout": 1}))["steps"]
            await rec.dispatch("record_stop", {})
            replayer = PageController(follower_ctx)
            for step in steps:
                await replayer.dispatch(step["action"], step["params"])
            return steps, await follower.evaluate(
                "() => { const q = (s) => document.querySelector(s);"
                " return [q('#email').value, q('select').value, q('#terms').checked,"
                " q('button').textContent]; }"
            )

    try:
        steps, state = asyncio.run(asyncio.wait_for(scenario(), 120))
    finally:
        server.shutdown()
    assert all(s["action"] == "replay" for s in steps)
    assert state == ["a@example.com", "VN", True, "done"]
