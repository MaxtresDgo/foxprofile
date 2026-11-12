"""Remote control of a running profile's pages.

runner.py starts `serve()` next to the browser. It listens on 127.0.0.1 on a
random port, requires a random per-launch token, and announces itself on
stdout as "CONTROL:<port>:<token>" so the launcher can forward API calls.
"""

from __future__ import annotations

import asyncio
import base64
import math
import re
import secrets
from typing import Any
from urllib.parse import unquote, urlparse

from aiohttp import web

from .recorder import REPLAYABLE, Recorder
from .recorder import origin as origin_of
from .screencast import MAX_LIVE, LiveView

ALLOWED_SCHEMES = ("http", "https", "about")
MAX_FULL_PAGE_HEIGHT = 16_384
MAX_TEXT = 40_000
TOKEN_HEADER = "X-Control-Token"


class ControlError(Exception):
    pass


def jpeg_width(data: bytes) -> int | None:
    """A JPEG's width in pixels, from its frame header (SOFn), or None."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return int.from_bytes(data[i + 7 : i + 9], "big")
        i += 2 + length
    return None


MAX_ARRANGE = 50
MIN_SHOT_WIDTH, MAX_SHOT_WIDTH = 80, 4096
# The viewport in CSS pixels and where it is on the page (for a CDP clip).
_VIEWPORT_JS = """() => ({
  width: window.innerWidth, height: window.innerHeight,
  x: window.visualViewport ? window.visualViewport.pageLeft : window.scrollX,
  y: window.visualViewport ? window.visualViewport.pageTop : window.scrollY,
})"""


def grid_cell(
    area: dict[str, int],
    size: tuple[int, int],
    index: int,
    count: int,
    columns: int | None = None,
    phone: bool = False,
) -> dict[str, int]:
    """Window bounds for cell `index` of `count` windows tiled over `area` (the
    screen's work area: left, top, width, height).

    A desktop window takes its cell: `columns` per row, by default as many as
    rows. A phone keeps its `size`, since its page is the phone's and would be
    cut off, not shrunk: phones stand in one row, overlapping when it is full."""
    left, top, width, height = area["left"], area["top"], area["width"], area["height"]
    if phone:
        w = size[0]
        step = w if count * w <= width else (width - w) // max(count - 1, 1)
        start = left + max(0, (width - count * w) // 2) if step == w else left
        return {"left": start + index * max(step, 0), "top": top}
    cols = max(1, min(columns or math.ceil(math.sqrt(count)), count))
    rows = math.ceil(count / cols)
    w, h = width // cols, height // rows
    return {
        "left": left + (index % cols) * w,
        "top": top + (index // cols) * h,
        "width": w,
        "height": h,
    }


_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):(.*)$", re.S)


def _check_url(url: str) -> str:
    url = url.strip()
    # "%66ile:" or "java%73cript:" would miss _SCHEME and get https:// prepended;
    # decode a percent-encoded scheme and check it like any other.
    colon = url.find(":")
    slash = url.find("/")
    if colon != -1 and "%" in url[:colon] and (slash == -1 or colon < slash):
        decoded_scheme = unquote(url[:colon]).lower()
        if decoded_scheme not in ALLOWED_SCHEMES:
            raise ControlError(f"URL scheme '{decoded_scheme}' is not allowed")

    match = _SCHEME.match(url)
    # "localhost:8080/x" looks like a scheme but is host:port; bare hosts get https.
    if not match or re.match(r"\d+(/|$)", match.group(2)):
        url = "https://" + url
    try:
        parsed = urlparse(url)
        host = parsed.hostname
    except ValueError as e:  # e.g. an unclosed IPv6 bracket: "[::1"
        raise ControlError(f"Malformed URL: {e}") from e
    scheme = parsed.scheme.lower()
    if scheme in ("http", "https") and not host:
        # Older Python versions parse "https://[]" without raising.
        raise ControlError("Malformed URL: no host")
    if scheme not in ALLOWED_SCHEMES:
        # file:, view-source:, chrome: etc. would let a remote caller read
        # local files or browser internals through the snapshot.
        raise ControlError(f"URL scheme '{scheme}' is not allowed")
    if scheme == "about" and url.lower() != "about:blank":
        # about:config, about:logins etc. expose or change browser internals.
        raise ControlError("Only about:blank is allowed among about: pages")
    return url


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


class PageController:
    """Executes control actions against a Playwright BrowserContext."""

    def __init__(self, context: Any, touch: bool = False) -> None:
        self._context = context
        # A phone persona turns the mouse into a finger (ChromeEngine._phone), and
        # Playwright's mouse then waits forever for its own events: tap instead.
        self._touch = touch
        self._current: Any = None
        # Set by serve(): {"port", "token"} for the live view's WebSocket.
        self.live: dict[str, Any] | None = None
        self._live_ok: bool | None = None
        self._live_count = 0
        # Chromium: per page, a CDP session for the wall's small screenshots and
        # the screen scaling it applies to them (_small_jpeg).
        self._cdp: dict[Any, tuple[Any, float | None]] = {}
        self._recorder: Recorder | None = None

    # --- page selection ---------------------------------------------------

    def _open_pages(self) -> list[Any]:
        return [p for p in self._context.pages if not p.is_closed()]

    def active_page(self) -> Any:
        """The tab actions go to, without opening one (the live view follows it)."""
        if self._current is not None and not self._current.is_closed():
            return self._current
        pages = self._open_pages()
        return pages[-1] if pages else None

    @property
    def cdp_context(self) -> Any:
        return self._context

    async def _page(self) -> Any:
        pages = self._open_pages()
        if self._current is not None and not self._current.is_closed():
            return self._current
        if pages:
            self._current = pages[-1]
            return self._current
        self._current = await self._context.new_page()
        return self._current

    async def _describe(self, page: Any) -> dict[str, Any]:
        return {"url": page.url, "title": await page.title()}

    # --- actions ----------------------------------------------------------

    async def navigate(self, url: str, wait_until: str = "load") -> dict[str, Any]:
        page = await self._page()
        await page.goto(_check_url(url), wait_until=wait_until)
        return await self._describe(page)

    async def back(self) -> dict[str, Any]:
        """Go back a page. It waits for the page's content, not its "load" event:
        sites such as Facebook keep loading media and fire it late or never, and
        the step back had long happened by then."""
        page = await self._page()
        await page.go_back(wait_until="domcontentloaded")
        return await self._describe(page)

    async def snapshot(self, max_chars: int = MAX_TEXT) -> dict[str, Any]:
        """Accessibility tree of the page: roles, names and text, compact."""
        page = await self._page()
        aria = await page.locator("body").aria_snapshot()
        aria, truncated = _truncate(aria, max_chars)
        return {**await self._describe(page), "snapshot": aria, "truncated": truncated}

    async def text(self, selector: str = "body", max_chars: int = MAX_TEXT) -> dict[str, Any]:
        page = await self._page()
        content = await page.locator(selector).first.inner_text()
        content, truncated = _truncate(content, max_chars)
        return {"text": content, "truncated": truncated}

    async def click(self, selector: str, timeout: int = 10_000) -> dict[str, Any]:
        page = await self._page()
        target = page.locator(selector).first
        await (target.tap if self._touch else target.click)(timeout=timeout)
        return await self._describe(page)

    async def type(
        self,
        selector: str,
        text: str,
        submit: bool = False,
        clear: bool = True,
        timeout: int = 10_000,
    ) -> dict[str, Any]:
        page = await self._page()
        field = page.locator(selector).first
        if clear:
            await field.fill(text, timeout=timeout)
        else:
            await field.press_sequentially(text, timeout=timeout)
        if submit:
            await field.press("Enter")
        return await self._describe(page)

    async def select(self, selector: str, value: str, timeout: int = 10_000) -> dict[str, Any]:
        """Pick an <option> of a <select> by value or label."""
        page = await self._page()
        await page.locator(selector).first.select_option(value, timeout=timeout)
        return await self._describe(page)

    async def press(self, key: str, selector: str | None = None) -> dict[str, Any]:
        page = await self._page()
        if selector:
            await page.locator(selector).first.press(key)
        else:
            await page.keyboard.press(key)
        return await self._describe(page)

    async def click_at(self, x: float, y: float) -> dict[str, Any]:
        """Click viewport coordinates, as seen on a screenshot of the active tab."""
        page = await self._page()
        await (page.touchscreen.tap if self._touch else page.mouse.click)(x, y)
        return await self._describe(page)

    async def keyboard_type(self, text: str) -> dict[str, Any]:
        """Type into whatever element has focus."""
        page = await self._page()
        await page.keyboard.type(text)
        return await self._describe(page)

    async def wait_for(self, selector: str, timeout: int = 15_000) -> dict[str, Any]:
        page = await self._page()
        await page.locator(selector).first.wait_for(timeout=timeout)
        return await self._describe(page)

    async def screenshot(
        self, full_page: bool = False, quality: int | None = None, width: int | None = None
    ) -> dict[str, Any]:
        """A PNG of the active tab; with `quality`, a JPEG of the viewport instead.

        The JPEG is what the panel's wall refreshes for up to 20 browsers: a
        fraction of the PNG's size (a phone's PNG is in device pixels, 2-3x wider).
        `width` shrinks it further to the tile it is shown in (Chromium renders it
        that small; Camoufox's stays in CSS pixels). `viewport` gives its size in
        CSS pixels, the coordinates click_at takes."""
        page = await self._page()
        if quality is not None:
            if not 20 <= int(quality) <= 90:
                raise ControlError("JPEG quality must be between 20 and 90")
            if width is not None and not MIN_SHOT_WIDTH <= int(width) <= MAX_SHOT_WIDTH:
                raise ControlError(f"width must be between {MIN_SHOT_WIDTH} and {MAX_SHOT_WIDTH}")
            view = await page.evaluate(_VIEWPORT_JS)
            jpeg = None
            if width is not None and int(width) < view["width"]:
                jpeg = await self._small_jpeg(page, view, int(quality), int(width))
            if jpeg is None:
                jpeg = await page.screenshot(type="jpeg", quality=int(quality), scale="css")
            return {
                **await self._describe(page),
                "jpeg_base64": base64.b64encode(jpeg).decode(),
                "viewport": {"width": view["width"], "height": view["height"]},
            }
        if full_page:
            height = await page.evaluate("() => document.documentElement.scrollHeight")
            if height > MAX_FULL_PAGE_HEIGHT:
                raise ControlError(
                    f"Page is {height}px tall; full-page screenshots are limited to "
                    f"{MAX_FULL_PAGE_HEIGHT}px. Scroll and take viewport screenshots instead.",
                )
        png = await page.screenshot(full_page=full_page, type="png")
        return {**await self._describe(page), "png_base64": base64.b64encode(png).decode()}

    async def _small_jpeg(
        self, page: Any, view: dict[str, Any], quality: int, width: int
    ) -> bytes | None:
        """The viewport as a JPEG about `width` pixels wide, rendered at that size
        by Chromium (CDP). None where there is no CDP (Camoufox) or it fails.

        Chromium multiplies the clip's scale by the screen's own scaling (125% on
        Windows), which a phone's page cannot see: the first shot of a page
        measures it and every later one is corrected."""
        session, factor = self._cdp.get(page, (None, None))
        try:
            if session is None:
                session = await self._context.new_cdp_session(page)
                self._cdp[page] = (session, None)
                page.once("close", lambda p: self._cdp.pop(p, None))

            async def capture(scale: float) -> bytes:
                clip = {k: view[k] for k in ("x", "y", "width", "height")}
                shot = await session.send(
                    "Page.captureScreenshot",
                    {"format": "jpeg", "quality": quality, "clip": {**clip, "scale": scale}},
                )
                return base64.b64decode(shot["data"])

            wanted = width / view["width"]
            data = await capture(wanted / (factor or 1))
            if factor is None:
                got = jpeg_width(data)
                factor = got / width if got else 1.0
                self._cdp[page] = (session, factor)
                if abs(factor - 1) > 0.05:
                    data = await capture(wanted / factor)
            return data
        except Exception:
            self._cdp.pop(page, None)
            return None

    async def evaluate(self, script: str) -> dict[str, Any]:
        page = await self._page()
        return {"result": await page.evaluate(script)}

    async def tabs(self) -> dict[str, Any]:
        current = await self._page()
        pages = self._open_pages()
        return {
            "tabs": [
                {"index": i, "url": p.url, "title": await p.title(), "active": p is current}
                for i, p in enumerate(pages)
            ],
        }

    async def tab_new(self, url: str | None = None) -> dict[str, Any]:
        self._current = await self._context.new_page()
        if url:
            await self._current.goto(_check_url(url))
        return await self.tabs()

    async def tab_select(self, index: int) -> dict[str, Any]:
        pages = self._open_pages()
        if not 0 <= index < len(pages):
            raise ControlError(f"No tab at index {index}")
        self._current = pages[index]
        await self._current.bring_to_front()
        return await self.tabs()

    async def tab_close(self, index: int) -> dict[str, Any]:
        pages = self._open_pages()
        if not 0 <= index < len(pages):
            raise ControlError(f"No tab at index {index}")
        if len(pages) == 1:
            # Closing the last tab closes the profile's browser.
            raise ControlError("Cannot close the last tab; stop the profile instead")
        await pages[index].close()
        self._current = None
        return await self.tabs()

    # --- sync: record on the leader, replay on the followers -------------

    async def quit(self) -> dict[str, Any]:
        """Close the browser the way its window does, so it writes its cookies to disk."""
        await self._context.close()
        return {}

    def _rec(self) -> Recorder:
        if self._recorder is None:
            self._recorder = Recorder(self._context)
        return self._recorder

    async def record_start(self) -> dict[str, Any]:
        return await self._rec().start()

    async def record_poll(self, timeout: float = 20) -> dict[str, Any]:
        return await self._rec().poll(timeout)

    async def record_stop(self) -> dict[str, Any]:
        return self._rec().stop()

    async def replay(self, step: dict[str, Any], origin: str) -> dict[str, Any]:
        """Run a step recorded on a leader, only on a page of the origin it came from."""
        page = await self._page()
        if origin_of(page.url) != origin:
            raise ControlError(f"Not replayed: this tab is on {page.url}, the leader on {origin}")
        if step.get("action") not in REPLAYABLE:
            raise ControlError(f"Action '{step.get('action')}' cannot be replayed")
        return await getattr(self, step["action"])(**(step.get("params") or {}))

    async def arrange(self, index: int, count: int, columns: int | None = None) -> dict[str, Any]:
        """Move and size this browser's window to cell `index` of a grid of
        `count` windows on its screen (grid_cell). Chromium only: it goes through
        CDP, and Playwright gives Firefox (Camoufox) no window control."""
        index, count = int(index), int(count)
        if not 0 <= index < count <= MAX_ARRANGE:
            raise ControlError(f"index must be below count, count at most {MAX_ARRANGE}")
        if columns is not None and not 1 <= int(columns) <= MAX_ARRANGE:
            raise ControlError("columns must be a positive number")
        page = await self._page()
        try:
            session = await self._context.new_cdp_session(page)
        except Exception as e:
            raise ControlError("Arranging windows needs the Chrome engine") from e
        try:
            window = (await session.send("Browser.getWindowForTarget"))["windowId"]

            async def bounds(**change: Any) -> dict[str, Any]:
                if change:
                    await session.send(
                        "Browser.setWindowBounds", {"windowId": window, "bounds": change}
                    )
                    await asyncio.sleep(0.2)  # the window manager applies it later
                result = await session.send("Browser.getWindowBounds", {"windowId": window})
                return result["bounds"]

            # Size and position only apply to a normal window.
            before = await bounds(windowState="normal")
            # CDP has no screen query: a maximized window covers the work area.
            area = await bounds(windowState="maximized")
            await bounds(windowState="normal")
            cell = grid_cell(
                area,
                (before["width"], before["height"]),
                index,
                count,
                int(columns) if columns else None,
                phone=self._touch,
            )
            placed = await bounds(**cell)
        finally:
            await session.detach()
        await page.bring_to_front()
        return {k: placed[k] for k in ("left", "top", "width", "height")}

    async def live_ticket(self) -> dict[str, Any]:
        """Where the panel can watch this browser live (screencast.py): the control
        server's port on 127.0.0.1 and the live token. Chromium only."""
        if self.live is None:
            return {"available": False}
        if self._live_ok is None:
            page = await self._page()
            try:
                session = await self._context.new_cdp_session(page)
                await session.detach()
                self._live_ok = True
            except Exception:
                self._live_ok = False
        return {"available": True, **self.live} if self._live_ok else {"available": False}

    ACTIONS = (
        "navigate",
        "back",
        "snapshot",
        "text",
        "click",
        "type",
        "select",
        "press",
        "click_at",
        "keyboard_type",
        "wait_for",
        "screenshot",
        "evaluate",
        "tabs",
        "tab_new",
        "tab_select",
        "tab_close",
        "record_start",
        "record_poll",
        "record_stop",
        "replay",
        "arrange",
        "live_ticket",
        "quit",
    )

    async def dispatch(self, action: str, params: dict[str, Any]) -> Any:
        if action not in self.ACTIONS:
            raise ControlError(f"Unknown action '{action}'")
        try:
            return await getattr(self, action)(**params)
        except TypeError as e:
            raise ControlError(f"Invalid parameters for '{action}': {e}") from e


def build_app(controller: PageController, token: str) -> web.Application:
    async def handle(request: web.Request) -> web.Response:
        if not secrets.compare_digest(request.headers.get(TOKEN_HEADER, ""), token):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            body = await request.json()
            result = await controller.dispatch(body.get("action", ""), body.get("params") or {})
        except ControlError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except Exception as e:  # Playwright timeouts, navigation errors, JS errors
            message = " ".join(str(e).split())
            if "Call log:" in message:
                message = message.split("Call log:", 1)[0].strip()
            return web.json_response(
                {"ok": False, "error": f"{type(e).__name__}: {message[:500]}"},
                status=422,
            )
        return web.json_response({"ok": True, "result": result})

    async def live(request: web.Request) -> web.StreamResponse:
        info = controller.live
        # Browsers cannot set headers on a WebSocket: the token is in the query.
        # The Host check stops a website from reaching it through DNS rebinding.
        if info is None or not secrets.compare_digest(request.query.get("t", ""), info["token"]):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        if request.host not in (f"127.0.0.1:{info['port']}", f"localhost:{info['port']}"):
            return web.json_response({"ok": False, "error": "bad host"}, status=403)
        if controller._live_count >= MAX_LIVE:
            return web.json_response({"ok": False, "error": "too many live views"}, status=429)
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024)
        await ws.prepare(request)
        controller._live_count += 1
        try:
            await LiveView(controller, ws).run()
        finally:
            controller._live_count -= 1
        return ws

    app = web.Application(client_max_size=4 * 1024 * 1024)
    app.router.add_post("/action", handle)
    app.router.add_get("/live", live)
    return app


async def serve(context: Any, touch: bool = False) -> tuple[web.AppRunner, int, str]:
    """Start the control server for a context. Returns (runner, port, token)."""
    token = secrets.token_urlsafe(24)
    controller = PageController(context, touch)
    runner = web.AppRunner(build_app(controller, token), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    # Its own token: the panel gets it, and it only reaches the live view.
    controller.live = {"port": port, "token": secrets.token_urlsafe(24)}
    return runner, port, token
