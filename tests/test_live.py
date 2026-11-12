"""The wall's live view: Chromium's screencast over a WebSocket on 127.0.0.1."""

import asyncio
import base64
import json

import aiohttp
import pytest

from src.services.browser import control
from src.services.browser.screencast import _nth

FRAME = b"\xff\xd8frame"


class FakeMouse:
    def __init__(self):
        self.clicks = []

    async def click(self, x, y):
        self.clicks.append((x, y))


class FakePage:
    url = "https://example.test/"

    def __init__(self):
        self.mouse = FakeMouse()
        self.scripts = []

    def is_closed(self):
        return False

    async def title(self):
        return "Example"

    async def evaluate(self, script):
        self.scripts.append(script)
        return None


class FakeSession:
    """Answers Page.startScreencast with a frame, like Chromium, until stopped."""

    def __init__(self, log):
        self.log = log
        self.handlers = {}

    def on(self, event, fn):
        self.handlers[event] = fn

    async def send(self, method, params=None):
        self.log.append((method, params))
        if method == "Page.startScreencast":
            event = {
                "data": base64.b64encode(FRAME).decode(),
                "metadata": {"deviceWidth": 412, "deviceHeight": 839},
                "sessionId": 1,
            }
            asyncio.get_running_loop().call_soon(self.handlers["Page.screencastFrame"], event)
        return {}

    async def detach(self):
        self.log.append(("detach", None))


class FakeContext:
    def __init__(self, cdp=True):
        self.pages = [FakePage()]
        self.cdp = cdp
        self.log = []

    async def new_cdp_session(self, page):
        if not self.cdp:
            raise RuntimeError("CDP session is only available in Chromium")
        return FakeSession(self.log)


async def _action(http, port, token, action, params=None):
    r = await http.post(
        f"http://127.0.0.1:{port}/action",
        json={"action": action, "params": params or {}},
        headers={control.TOKEN_HEADER: token},
    )
    return (await r.json())["result"]


async def _receive(ws, kind):
    while True:
        msg = await ws.receive(timeout=5)
        if msg.type == kind:
            return msg


def run_live(test, cdp=True):
    async def main():
        ctx = FakeContext(cdp)
        runner, port, token = await control.serve(ctx)
        try:
            async with aiohttp.ClientSession() as http:
                await test(ctx, http, port, token)
        finally:
            await runner.cleanup()

    asyncio.run(main())


def test_ticket_and_frames_with_viewport():
    async def test(ctx, http, port, token):
        ticket = await _action(http, port, token, "live_ticket")
        assert ticket["available"] and ticket["port"] == port
        assert ticket["token"] != token  # its own token, not the control one
        url = f"ws://127.0.0.1:{port}/live?t={ticket['token']}"
        async with http.ws_connect(url) as ws:
            await ws.send_json({"type": "view", "width": 200, "fps": 4})
            text = await _receive(ws, aiohttp.WSMsgType.TEXT)
            assert json.loads(text.data) == {"type": "viewport", "width": 412, "height": 839}
            frame = await _receive(ws, aiohttp.WSMsgType.BINARY)
            assert frame.data == FRAME
        start = next(p for m, p in ctx.log if m == "Page.startScreencast")
        assert start["maxWidth"] == 200 and start["format"] == "jpeg"
        assert start["everyNthFrame"] == 7

    run_live(test)


def test_input_goes_through_and_scripts_do_not():
    async def test(ctx, http, port, token):
        ticket = await _action(http, port, token, "live_ticket")
        async with http.ws_connect(f"ws://127.0.0.1:{port}/live?t={ticket['token']}") as ws:
            await ws.send_json(
                {"type": "do", "id": 1, "action": "click_at", "params": {"x": 10, "y": 20}}
            )
            done = json.loads((await _receive(ws, aiohttp.WSMsgType.TEXT)).data)
            assert done["id"] == 1 and done["ok"]
            assert ctx.pages[0].mouse.clicks == [(10, 20)]
            await ws.send_json({"type": "do", "id": 2, "action": "scroll", "params": {"dy": 120}})
            assert json.loads((await _receive(ws, aiohttp.WSMsgType.TEXT)).data)["ok"]
            assert ctx.pages[0].scripts == ["window.scrollBy(0, 120)"]
            for i, action in enumerate(("evaluate", "replay", "quit"), start=3):
                await ws.send_json(
                    {"type": "do", "id": i, "action": action, "params": {"script": "1"}}
                )
                done = json.loads((await _receive(ws, aiohttp.WSMsgType.TEXT)).data)
                assert done == {"type": "done", "id": i, "ok": False, "error": done["error"]}
            assert ctx.pages[0].scripts == ["window.scrollBy(0, 120)"]

    run_live(test)


def test_wrong_token_and_foreign_host_are_refused():
    async def test(ctx, http, port, token):
        ticket = await _action(http, port, token, "live_ticket")
        assert (await http.get(f"http://127.0.0.1:{port}/live?t=nope")).status == 401
        assert (await http.get(f"http://127.0.0.1:{port}/live?t={token}")).status == 401
        r = await http.get(
            f"http://127.0.0.1:{port}/live?t={ticket['token']}",
            headers={"Host": f"evil.example:{port}"},
        )
        assert r.status == 403

    run_live(test)


def test_no_live_view_without_cdp():
    async def test(ctx, http, port, token):
        assert await _action(http, port, token, "live_ticket") == {"available": False}

    run_live(test, cdp=False)


@pytest.mark.parametrize(("fps", "nth"), [(30, 1), (15, 2), (4, 7), (0, 1)])
def test_every_nth_frame(fps, nth):
    assert _nth(fps) == nth


def test_back_waits_for_the_content_not_the_load_event():
    """Facebook fires "load" late or never: going back must not time out on it."""

    class Page(FakePage):
        async def go_back(self, **kwargs):
            self.back_kwargs = kwargs

    class Context(FakeContext):
        def __init__(self):
            super().__init__()
            self.pages = [Page()]

    ctx = Context()
    asyncio.run(control.PageController(ctx).back())
    assert ctx.pages[0].back_kwargs == {"wait_until": "domcontentloaded"}
