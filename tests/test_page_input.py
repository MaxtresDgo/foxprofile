"""Page input that needs care: Facebook's late load event and long presses."""

import asyncio

import pytest

from src.services.browser import control


class FakeMouse:
    def __init__(self, log):
        self.log = log

    async def click(self, x, y):
        self.log.append(("click", x, y))

    async def move(self, x, y):
        self.log.append(("move", x, y))


class FakePage:
    url = "https://example.test/"

    def __init__(self, log):
        self.mouse = FakeMouse(log)

    def is_closed(self):
        return False

    async def title(self):
        return "Example"

    async def go_back(self, **kwargs):
        self.back_kwargs = kwargs


class FakeSession:
    def __init__(self, log):
        self.log = log

    async def send(self, method, params=None):
        self.log.append((method, params))
        return {}

    async def detach(self):
        self.log.append(("detach", None))


class FakeContext:
    def __init__(self):
        self.log = []
        self.pages = [FakePage(self.log)]

    async def new_cdp_session(self, page):
        return FakeSession(self.log)


def test_back_waits_for_the_content_not_the_load_event():
    """Facebook fires "load" late or never: going back must not time out on it."""
    ctx = FakeContext()
    asyncio.run(control.PageController(ctx).back())
    assert ctx.pages[0].back_kwargs == {"wait_until": "domcontentloaded"}


def test_long_press_with_the_mouse():
    """A held mouse hovers there (Facebook's reactions on desktop), never clicks."""
    ctx = FakeContext()
    asyncio.run(control.PageController(ctx).click_at(30, 40, hold=50))
    assert ctx.log == [("move", 30, 40)]


def test_long_press_with_a_finger():
    """A phone long-presses through Chromium's tap gesture: no click after it."""
    ctx = FakeContext()
    asyncio.run(control.PageController(ctx, touch=True).click_at(30, 40, hold=50))
    assert ctx.log == [
        (
            "Input.synthesizeTapGesture",
            {"x": 30, "y": 40, "duration": 50, "gestureSourceType": "touch"},
        ),
        ("detach", None),
    ]


def test_plain_click():
    ctx = FakeContext()
    asyncio.run(control.PageController(ctx).click_at(30, 40))
    assert ctx.log == [("click", 30, 40)]


@pytest.mark.parametrize("hold", [-1, control.MAX_HOLD_MS + 1])
def test_long_press_is_bounded(hold):
    with pytest.raises(control.ControlError):
        asyncio.run(control.PageController(FakeContext()).click_at(1, 1, hold=hold))


class SlowPage(FakePage):
    def __init__(self, log, title, delay):
        super().__init__(log)
        self._title, self._delay = title, delay

    async def title(self):
        await asyncio.sleep(self._delay)
        return self._title


def test_a_slow_background_tab_does_not_hold_up_the_tab_list(monkeypatch):
    monkeypatch.setattr(control, "TITLE_WAIT_S", 0.05)
    ctx = FakeContext()
    ctx.pages = [SlowPage(ctx.log, "fast", 0), SlowPage(ctx.log, "slow", 5)]
    tabs = asyncio.run(control.PageController(ctx).tabs())["tabs"]
    assert [t["title"] for t in tabs] == ["fast", ""]


def test_the_wheel_goes_to_the_point_through_cdp():
    ctx = FakeContext()
    asyncio.run(control.PageController(ctx).wheel(10, 20, 300))
    assert ctx.log[0] == (
        "Input.dispatchMouseEvent",
        {"type": "mouseWheel", "x": 10, "y": 20, "deltaX": 0, "deltaY": 300},
    )


class TabPage(FakePage):
    def __init__(self, log, name):
        super().__init__(log)
        self.log, self.name, self.closed = log, name, False

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True

    async def bring_to_front(self):
        self.log.append(("front", self.name))


def test_closing_the_active_tab_shows_the_one_actions_now_go_to():
    ctx = FakeContext()
    ctx.pages = [TabPage(ctx.log, "a"), TabPage(ctx.log, "b"), TabPage(ctx.log, "c")]
    controller = control.PageController(ctx)
    asyncio.run(controller.tab_select(1))
    ctx.log.clear()
    tabs = asyncio.run(controller.tab_close(1))["tabs"]
    assert [t["active"] for t in tabs] == [False, True]
    assert ctx.log == [("front", "c")]
    # A background tab closes without moving the view.
    ctx.log.clear()
    asyncio.run(controller.tab_close(0))
    assert ctx.log == []


def test_the_last_tab_is_not_closed():
    ctx = FakeContext()
    ctx.pages = [TabPage(ctx.log, "a")]
    try:
        asyncio.run(control.PageController(ctx).tab_close(0))
    except control.ControlError:
        pass
    else:
        raise AssertionError("closed the last tab")
    assert not ctx.pages[0].closed
