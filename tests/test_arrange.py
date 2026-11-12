"""Arrange windows: tile the running Chrome windows on the screen of their PC."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from src.api import app as app_module
from src.api import auth
from src.api.app import create_app
from src.core.container import Container
from src.services.browser.control import ControlError, PageController, grid_cell

TOKEN = "t" * 32
AREA = {"left": 0, "top": 0, "width": 1920, "height": 1040}


def test_grid_cell_four_desktops_make_two_by_two():
    cells = [grid_cell(AREA, (800, 600), i, 4) for i in range(4)]
    assert cells == [
        {"left": 0, "top": 0, "width": 960, "height": 520},
        {"left": 960, "top": 0, "width": 960, "height": 520},
        {"left": 0, "top": 520, "width": 960, "height": 520},
        {"left": 960, "top": 520, "width": 960, "height": 520},
    ]


def test_grid_cell_columns_and_offset_work_area():
    area = {"left": 100, "top": 30, "width": 1800, "height": 1000}
    # 5 windows in 3 columns: two rows, the last row has two.
    assert grid_cell(area, (0, 0), 4, 5, columns=3) == {
        "left": 700,
        "top": 530,
        "width": 600,
        "height": 500,
    }
    # One window takes the whole area.
    assert grid_cell(area, (0, 0), 0, 1) == {**area}


def test_grid_cell_phones_keep_their_size_in_one_centred_row():
    cells = [grid_cell(AREA, (520, 1000), i, 3, phone=True) for i in range(3)]
    assert cells == [{"left": 180, "top": 0}, {"left": 700, "top": 0}, {"left": 1220, "top": 0}]


def test_grid_cell_phones_overlap_when_the_row_is_full():
    cells = [grid_cell(AREA, (520, 1000), i, 6, phone=True) for i in range(6)]
    assert [c["left"] for c in cells] == [0, 280, 560, 840, 1120, 1400]
    assert cells[-1]["left"] + 520 == AREA["width"]


class FakeSession:
    def __init__(self):
        self.bounds = {"left": 50, "top": 60, "width": 700, "height": 500, "windowState": "normal"}
        self.sent = []
        self.detached = False

    async def send(self, method, params=None):
        self.sent.append((method, params))
        if method == "Browser.getWindowForTarget":
            return {"windowId": 7}
        if method == "Browser.setWindowBounds":
            change = params["bounds"]
            # Like Chrome: maximizing covers the work area, restoring brings the
            # window's normal bounds back.
            if change.get("windowState") == "maximized":
                self.normal = self.bounds
                self.bounds = {**AREA, "windowState": "maximized"}
            elif change.get("windowState") == "normal" and self.bounds["windowState"] != "normal":
                self.bounds = self.normal
            else:
                self.bounds = {**self.bounds, **change}
            return {}
        if method == "Browser.getWindowBounds":
            return {"bounds": dict(self.bounds)}
        raise AssertionError(method)

    async def detach(self):
        self.detached = True


class FakePage:
    url = "https://example.test/"

    def __init__(self):
        self.front = False

    def is_closed(self):
        return False

    async def bring_to_front(self):
        self.front = True


class FakeContext:
    def __init__(self, cdp=True):
        self.pages = [FakePage()]
        self.session = FakeSession()
        self.cdp = cdp

    async def new_cdp_session(self, page):
        if not self.cdp:
            raise RuntimeError("CDP session is only available in Chromium")
        return self.session


@pytest.fixture
def fast_sleep(monkeypatch):
    async def no_wait(_):
        return None

    monkeypatch.setattr("src.services.browser.control.asyncio.sleep", no_wait)


def test_arrange_moves_the_window_into_its_cell(fast_sleep):
    ctx = FakeContext()
    result = asyncio.run(PageController(ctx).dispatch("arrange", {"index": 1, "count": 2}))
    assert result == {"left": 960, "top": 0, "width": 960, "height": 1040}
    assert ctx.session.detached and ctx.pages[0].front


def test_arrange_a_phone_only_moves_it(fast_sleep):
    ctx = FakeContext()
    result = asyncio.run(
        PageController(ctx, touch=True).dispatch("arrange", {"index": 0, "count": 2})
    )
    # Two 700px phones fit: centred, and the window keeps its size.
    assert result == {"left": 260, "top": 0, "width": 700, "height": 500}


def test_arrange_without_cdp_is_a_control_error(fast_sleep):
    with pytest.raises(ControlError):
        asyncio.run(PageController(FakeContext(cdp=False)).arrange(0, 1))


@pytest.mark.parametrize("params", [{"index": 2, "count": 2}, {"index": 0, "count": 51}])
def test_arrange_rejects_a_cell_outside_the_grid(params):
    with pytest.raises(ControlError):
        asyncio.run(PageController(FakeContext()).arrange(**params))


class FakeLauncher:
    def __init__(self):
        self.calls = []

    def is_running(self, name):
        return True

    def running_profile_names(self):
        return {"c", "f"}

    def control(self, name, action, params=None, timeout=60):
        self.calls.append((name, action, params))
        return {"left": 0, "top": 0, "width": 960, "height": 1040}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auth, "API_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "API_TOKEN", TOKEN)
    container = Container(hub=False)
    bl = FakeLauncher()
    container._instances["bl"] = bl
    pm = container.profile_manager
    assert pm.add_profile("c", "", "windows", engine="chrome")
    assert pm.add_profile("f", "", "windows")
    client = TestClient(create_app(container), headers={"Authorization": f"Bearer {TOKEN}"})
    return client, bl


def test_api_arrange_forwards_to_the_browser(api):
    client, bl = api
    r = client.post("/api/v1/browser/c/page/arrange", json={"index": 1, "count": 2})
    assert r.status_code == 200, r.text
    assert bl.calls == [("c", "arrange", {"index": 1, "count": 2})]


def test_api_arrange_refuses_camoufox(api):
    client, bl = api
    r = client.post(
        "/api/v1/browser/f/page/arrange",
        json={"index": 0, "count": 1},
        headers={"Accept-Language": "es"},
    )
    assert r.status_code == 400
    assert "Chrome" in r.json()["detail"]
    assert bl.calls == []


@pytest.mark.parametrize("body", [{"index": 2, "count": 2}, {"index": 0, "count": 0}])
def test_api_arrange_validates_the_cell(api, body):
    client, bl = api
    assert client.post("/api/v1/browser/c/page/arrange", json=body).status_code == 422
    assert bl.calls == []
