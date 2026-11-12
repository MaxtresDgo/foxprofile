"""Small JPEG screenshots of the viewport (screenshot?quality&width)."""

import asyncio
import base64

import pytest
from fastapi.testclient import TestClient

from src.api import app as app_module
from src.api import auth
from src.api.app import create_app
from src.core.container import Container
from src.services.browser.control import ControlError, PageController, jpeg_width

TOKEN = "t" * 32
VIEW = {"width": 360, "height": 724, "x": 0, "y": 400}


def fake_jpeg(width, height=100):
    """Just enough of a JPEG for jpeg_width: SOI, then a SOF0 frame header."""
    sof = bytes([8]) + height.to_bytes(2, "big") + width.to_bytes(2, "big") + bytes([3]) + bytes(9)
    return (
        bytes([0xFF, 0xD8, 0xFF, 0xE0, 0, 4, 97, 98, 0xFF, 0xC0])
        + (len(sof) + 2).to_bytes(2, "big")
        + sof
    )


class FakePage:
    url = "https://example.test/"

    def __init__(self):
        self.shots = []
        self.on_close = []

    def is_closed(self):
        return False

    async def title(self):
        return "Example"

    async def evaluate(self, script):
        return dict(VIEW)

    def once(self, event, fn):
        self.on_close.append(fn)

    async def screenshot(self, **kwargs):
        self.shots.append(kwargs)
        return b"jpeg" if kwargs.get("type") == "jpeg" else b"png"


class FakeSession:
    """Chromium's capture: the clip times its scale, times the screen's 125%."""

    def __init__(self):
        self.clips = []

    async def send(self, method, params):
        assert method == "Page.captureScreenshot"
        clip = params["clip"]
        self.clips.append(clip)
        width = round(clip["width"] * clip["scale"] * 1.25)
        return {"data": base64.b64encode(fake_jpeg(width)).decode()}


class FakeContext:
    def __init__(self, cdp=True):
        self.pages = [FakePage()]
        self.session = FakeSession()
        self.cdp = cdp
        self.sessions = 0

    async def new_cdp_session(self, page):
        if not self.cdp:
            raise RuntimeError("CDP session is only available in Chromium")
        self.sessions += 1
        return self.session


def test_jpeg_width_reads_the_frame_header():
    assert jpeg_width(fake_jpeg(321)) == 321
    assert jpeg_width(b"not a jpeg") is None


def test_screenshot_with_quality_is_a_css_pixel_jpeg():
    context = FakeContext()
    result = asyncio.run(PageController(context).screenshot(quality=60))
    assert context.pages[0].shots == [{"type": "jpeg", "quality": 60, "scale": "css"}]
    assert base64.b64decode(result["jpeg_base64"]) == b"jpeg"
    assert result["viewport"] == {"width": 360, "height": 724}
    assert "png_base64" not in result


def test_screenshot_width_is_rendered_small_and_calibrated_once():
    context = FakeContext()
    pc = PageController(context)

    async def shots():
        return [await pc.screenshot(quality=60, width=200) for _ in range(3)]

    results = asyncio.run(shots())
    for r in results:
        assert jpeg_width(base64.b64decode(r["jpeg_base64"])) == 200
        assert r["viewport"] == {"width": 360, "height": 724}
    # The first shot measured the 125% and was taken again; later ones are right at once.
    assert len(context.session.clips) == 4
    assert context.sessions == 1
    # The clip follows the scroll.
    assert context.session.clips[-1]["y"] == 400
    assert context.pages[0].shots == []


def test_screenshot_width_falls_back_without_cdp():
    context = FakeContext(cdp=False)
    result = asyncio.run(PageController(context).screenshot(quality=60, width=200))
    assert context.pages[0].shots == [{"type": "jpeg", "quality": 60, "scale": "css"}]
    assert result["viewport"] == {"width": 360, "height": 724}


def test_screenshot_width_wider_than_the_page_stays_css():
    context = FakeContext()
    asyncio.run(PageController(context).screenshot(quality=60, width=800))
    assert context.session.clips == []
    assert len(context.pages[0].shots) == 1


def test_screenshot_without_quality_stays_a_png():
    context = FakeContext()
    result = asyncio.run(PageController(context).screenshot())
    assert context.pages[0].shots == [{"full_page": False, "type": "png"}]
    assert base64.b64decode(result["png_base64"]) == b"png"


@pytest.mark.parametrize("params", [{"quality": 5}, {"quality": 100}, {"quality": 60, "width": 10}])
def test_screenshot_quality_out_of_range(params):
    with pytest.raises(ControlError):
        asyncio.run(PageController(FakeContext()).screenshot(**params))


class FakeLauncher:
    def __init__(self):
        self.calls = []

    def is_running(self, name):
        return True

    def running_profile_names(self):
        return {"a"}

    def control(self, name, action, params=None, timeout=60):
        self.calls.append((name, action, params))
        if "quality" in (params or {}):
            return {
                "url": "https://example.test/",
                "title": "T",
                "jpeg_base64": base64.b64encode(b"img").decode(),
                "viewport": {"width": 412, "height": 839},
            }
        return {
            "url": "https://example.test/",
            "title": "T",
            "png_base64": base64.b64encode(b"img").decode(),
        }


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auth, "API_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "API_TOKEN", TOKEN)
    container = Container(hub=False)
    bl = FakeLauncher()
    container._instances["bl"] = bl
    client = TestClient(create_app(container), headers={"Authorization": f"Bearer {TOKEN}"})
    assert client.post("/api/v1/profiles", json={"name": "a"}).status_code == 201
    return client, bl


def test_api_screenshot_quality_answers_a_jpeg(api):
    client, bl = api
    r = client.get("/api/v1/browser/a/page/screenshot?quality=60")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content == b"img"
    assert (r.headers["x-viewport-width"], r.headers["x-viewport-height"]) == ("412", "839")
    assert bl.calls == [("a", "screenshot", {"full_page": False, "quality": 60})]


def test_api_screenshot_width_goes_with_quality_only(api):
    client, bl = api
    client.get("/api/v1/browser/a/page/screenshot?quality=60&width=240")
    client.get("/api/v1/browser/a/page/screenshot?width=240")
    assert bl.calls == [
        ("a", "screenshot", {"full_page": False, "quality": 60, "width": 240}),
        ("a", "screenshot", {"full_page": False}),
    ]


def test_api_screenshot_without_quality_asks_the_old_way(api):
    client, bl = api
    r = client.get("/api/v1/browser/a/page/screenshot")
    assert r.headers["content-type"] == "image/png"
    # No `quality` key: an agent on older code still answers.
    assert bl.calls == [("a", "screenshot", {"full_page": False})]


@pytest.mark.parametrize("quality", [10, 95])
def test_api_screenshot_quality_out_of_range(api, quality):
    client, bl = api
    assert client.get(f"/api/v1/browser/a/page/screenshot?quality={quality}").status_code == 422
    assert bl.calls == []
