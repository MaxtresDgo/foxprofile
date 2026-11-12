"""Live view of a running browser's active tab, for the panel's wall.

The control server (control.py) accepts a WebSocket at /live with the launch's
live token. Chromium's screencast (CDP Page.startScreencast) then pushes a JPEG
of the active tab, already as small as the tile, each time the page changes:
nothing while it is still. The panel connects straight to 127.0.0.1, so the
frames never leave the PC; where it cannot (Camoufox, the panel on another PC)
the wall keeps taking screenshots through the API.

Client -> server, JSON text:
  {"type": "view", "width": W, "fps": F}   frame width in pixels; F=0 pauses
  {"type": "do", "id": N, "action": A, "params": {...}}   an input (LIVE_ACTIONS)
Server -> client:
  binary                                   a JPEG frame
  {"type": "viewport", "width", "height"}  the page in CSS pixels (click_at)
  {"type": "done", "id": N, "ok": bool, "result"|"error"}
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

if TYPE_CHECKING:
    from .control import PageController

# What a live tile may do: input and navigation, not scripts or recordings.
LIVE_ACTIONS = ("click_at", "keyboard_type", "press", "back", "navigate", "tabs", "tab_new", "tab_select", "scroll")  # fmt: skip
MAX_FPS = 30
MIN_WIDTH, MAX_WIDTH = 80, 4096
MAX_LIVE = 3  # live views per browser at once (one panel tab, a reload, a spare)
_WATCH_S = 0.5  # how often the active tab is checked


def _nth(fps: float) -> int:
    """Chromium's everyNthFrame for `fps`. Pages animate at 30-60 frames a second;
    counting 30 keeps a busy one from dropping below `fps` (the send lock caps it)."""
    return max(1, int(30 // fps)) if fps > 0 else 1


class LiveView:
    """One WebSocket client watching one browser."""

    def __init__(self, controller: PageController, ws: web.WebSocketResponse) -> None:
        self.controller = controller
        self.ws = ws
        self.width = 0
        self.fps = 0.0
        self.page: Any = None
        self.session: Any = None
        self.viewport: tuple[int, int] | None = None
        self.last_frame = 0.0
        self._seq = 0
        self._send_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        watcher = asyncio.ensure_future(self._watch())
        try:
            async for msg in self.ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if data.get("type") == "view":
                    await self._set_view(data)
                elif data.get("type") == "do":
                    self._spawn(self._do(data))
        finally:
            watcher.cancel()
            for task in list(self._tasks):
                task.cancel()
            await self._stop()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- frames -----------------------------------------------------------

    async def _set_view(self, data: dict[str, Any]) -> None:
        try:
            width = max(MIN_WIDTH, min(MAX_WIDTH, int(data.get("width") or 0)))
            fps = max(0.0, min(float(MAX_FPS), float(data.get("fps") or 0)))
        except (TypeError, ValueError):
            return
        restart = width != self.width or _nth(fps) != _nth(self.fps) or (fps > 0) != (self.fps > 0)
        self.width, self.fps = width, fps
        if restart:
            await self._stop()
            if fps > 0:
                await self._start()

    async def _watch(self) -> None:
        """Follow the active tab: a tab switch or a closed page restarts the cast."""
        while not self.ws.closed:
            await asyncio.sleep(_WATCH_S)
            if self.fps > 0 and self.controller.active_page() is not self.page:
                await self._stop()
                await self._start()

    async def _start(self) -> None:
        page = self.controller.active_page()
        if page is None:
            return
        try:
            session = await self.controller.cdp_context.new_cdp_session(page)
            session.on("Page.screencastFrame", self._on_frame)
            await session.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": 60,
                    "maxWidth": self.width,
                    "maxHeight": MAX_WIDTH,
                    # Chromium skips the frames in between: less work for a slow tile.
                    "everyNthFrame": _nth(self.fps),
                },
            )
        except Exception:  # the page closed meanwhile: _watch tries again
            return
        self.page, self.session = page, session

    async def _stop(self) -> None:
        session, self.session, self.page = self.session, None, None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.send("Page.stopScreencast")
            with contextlib.suppress(Exception):
                await session.detach()

    def _on_frame(self, event: dict[str, Any]) -> None:
        self._seq += 1
        self._spawn(self._send_frame(self.session, event, self._seq))

    async def _send_frame(self, session: Any, event: dict[str, Any], seq: int) -> None:
        """Frames go out one at a time, at most `fps` a second. Chromium keeps two
        in flight, so one may wait here; a newer one arriving meanwhile replaces it."""
        async with self._send_lock:
            if self.fps > 0:
                wait = 1 / self.fps - (time.monotonic() - self.last_frame)
                if wait > 0:
                    await asyncio.sleep(wait)
            if seq == self._seq:
                await self._deliver(session, event)
            if session is self.session:
                with contextlib.suppress(Exception):
                    await session.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})

    async def _deliver(self, session: Any, event: dict[str, Any]) -> None:
        if session is None or session is not self.session or self.ws.closed:
            return
        meta = event.get("metadata") or {}
        viewport = (round(meta.get("deviceWidth") or 0), round(meta.get("deviceHeight") or 0))
        try:
            if viewport != self.viewport and all(viewport):
                self.viewport = viewport
                await self.ws.send_json(
                    {"type": "viewport", "width": viewport[0], "height": viewport[1]}
                )
            await self.ws.send_bytes(base64.b64decode(event["data"]))
        except (ConnectionError, RuntimeError):
            return
        self.last_frame = time.monotonic()

    # --- input ------------------------------------------------------------

    async def _do(self, data: dict[str, Any]) -> None:
        action, params = data.get("action"), data.get("params") or {}
        reply: dict[str, Any] = {"type": "done", "id": data.get("id")}
        try:
            if action not in LIVE_ACTIONS or not isinstance(params, dict):
                raise ValueError(f"Action '{action}' is not allowed here")
            if action == "scroll":
                script = f"window.scrollBy(0, {int(params.get('dy') or 0)})"
                result = await self.controller.dispatch("evaluate", {"script": script})
            else:
                result = await self.controller.dispatch(action, params)
            reply.update(ok=True, result=result)
        except Exception as e:
            reply.update(ok=False, error=" ".join(str(e).split())[:300] or type(e).__name__)
        with contextlib.suppress(ConnectionError, RuntimeError):
            await self.ws.send_json(reply)
