"""Records what the user does in a sync leader's pages as replayable steps.

A script injected into every page reports trusted clicks, committed text,
<select> changes and Enter/Tab/Escape through a Playwright binding. The API
long-polls `poll()` and replays the steps on the follower profiles. Each step
carries the origin it was recorded on, and followers refuse it on any other
origin, so a page that calls the binding itself can only act on its own site.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from typing import Any
from urllib.parse import urlparse

MAX_POLL_S = 25
MAX_VALUE = 20_000
# ponytail: a navigation within this window of an input event is taken as caused
# by it (followers get there by replaying the input); a later JS redirect is
# replayed as a navigate. Upgrade path: track navigation initiators if it misfires.
NAV_GRACE_S = 3.0

# The flag lives on `document`, not `window`: set_content()/document.write()
# replace the document but keep the window, and the listeners go with the document.
RECORDER_JS = r"""
(() => {
  if (document.__fpRec) return;
  document.__fpRec = true;
  const SKIP = ['checkbox', 'radio', 'button', 'submit', 'reset', 'file', 'image', 'range', 'color', 'hidden'];
  const CLICKABLE = 'a,button,label,summary,[role=button],[role=link],[role=tab],[role=menuitem],[onclick]';
  const send = (action, params) => { try { window.__fpRec({ action, params }); } catch (_) {} };
  const textual = (el) => el.localName === 'textarea' || (el.localName === 'input' && !SKIP.includes(el.type));
  const stableId = (id) => id && !/\d{4,}/.test(id);
  const quote = (v) => '"' + v.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
  const sel = (el) => {
    if (stableId(el.id)) return '#' + CSS.escape(el.id);
    for (const a of ['data-testid', 'data-test', 'name', 'aria-label', 'placeholder']) {
      const v = el.getAttribute(a);
      if (!v) continue;
      const s = el.localName + '[' + a + '=' + quote(v) + ']';
      if (document.querySelectorAll(s).length === 1) return s;
    }
    const parts = [];
    for (let e = el; e && e.nodeType === 1 && e !== document.body; e = e.parentElement) {
      if (stableId(e.id)) { parts.unshift('#' + CSS.escape(e.id)); break; }
      let i = 1;
      for (let s = e.previousElementSibling; s; s = s.previousElementSibling) if (s.localName === e.localName) i++;
      parts.unshift(e.localName + ':nth-of-type(' + i + ')');
    }
    if (!parts.length || parts[0][0] !== '#') parts.unshift('body');
    return parts.join(' > ');
  };
  const typed = (el) => {
    if (el.value === el.__fpSent) return;
    el.__fpSent = el.value;
    send('type', { selector: sel(el), text: el.value });
  };
  document.addEventListener('click', (e) => {
    if (!e.isTrusted || !(e.target instanceof Element)) return;
    // A <select> and its <option>s (Firefox clicks those) send their value on change.
    if (e.target.closest('select')) return;
    const el = e.target.closest(CLICKABLE) || e.target;
    if (textual(el)) return;
    send('click', { selector: sel(el) });
  }, true);
  document.addEventListener('change', (e) => {
    // Not isTrusted: Chromium fires a synthetic change when automation picks an option.
    if (!(e.target instanceof Element)) return;
    const el = e.target;
    if (el.localName === 'select') send('select', { selector: sel(el), value: el.value });
    else if (textual(el)) typed(el);
  }, true);
  document.addEventListener('keydown', (e) => {
    if (!e.isTrusted || !['Enter', 'Tab', 'Escape'].includes(e.key)) return;
    const el = e.target;
    // Enter submits before the field fires `change`: send its value first.
    if (el instanceof Element && textual(el)) typed(el);
    const onBody = !(el instanceof Element) || el === document.body;
    send('press', onBody ? { key: e.key } : { key: e.key, selector: sel(el) });
  }, true);
})();
"""

# Fields each recorded action may carry; the first ones are required.
_FIELDS = {
    "click": (("selector",), ()),
    "type": (("selector", "text"), ()),
    "select": (("selector", "value"), ()),
    "press": (("key",), ("selector",)),
}
REPLAYABLE = tuple(_FIELDS)


def origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def clean_step(step: Any) -> dict[str, Any] | None:
    """The recorded step with only known string fields, or None if malformed.

    It comes from page JavaScript, so a hostile page may have shaped it."""
    if not isinstance(step, dict) or not isinstance(step.get("params"), dict):
        return None
    action, params = step.get("action"), step["params"]
    if action not in _FIELDS:
        return None
    required, optional = _FIELDS[action]
    out = {}
    for key in (*required, *optional):
        value = params.get(key)
        if isinstance(value, str) and len(value) <= MAX_VALUE:
            out[key] = value
        elif key in required:
            return None
    return {"action": action, "params": out}


class Recorder:
    """Queues the steps recorded in one browser context while recording is on."""

    def __init__(self, context: Any) -> None:
        self._context = context
        self._queue: asyncio.Queue | None = None
        self._installed = False
        self._watched: set[int] = set()
        self._last_input = 0.0

    async def start(self) -> dict[str, Any]:
        self._queue = asyncio.Queue()  # a new session never sees stale steps
        if not self._installed:
            # Neither can be undone: after the first sync the script stays until the
            # browser restarts, and its events are dropped while not recording.
            await self._context.expose_binding("__fpRec", self._on_event)
            await self._context.add_init_script(RECORDER_JS)
            self._context.on("page", self._watch)
            self._installed = True
        for page in self._context.pages:
            self._watch(page)
            # Pages loaded before the init script; one mid-navigation gets it from that script.
            with suppress(Exception):
                await page.evaluate(RECORDER_JS)
        return {"recording": True}

    def stop(self) -> dict[str, Any]:
        if self._queue is not None:
            self._queue.put_nowait(None)  # wakes a pending poll
            self._queue = None
        return {"recording": False}

    async def poll(self, timeout: float = 20) -> dict[str, Any]:
        """Wait up to `timeout` seconds for steps, then return all queued ones."""
        queue = self._queue
        if queue is None:
            return {"recording": False, "steps": []}
        try:
            first = await asyncio.wait_for(queue.get(), min(max(timeout, 0), MAX_POLL_S))
        except asyncio.TimeoutError:
            return {"recording": True, "steps": []}
        steps = [first]
        while not queue.empty():
            steps.append(queue.get_nowait())
        return {"recording": self._queue is queue, "steps": [s for s in steps if s]}

    def _watch(self, page: Any) -> None:
        if id(page) in self._watched:
            return
        self._watched.add(id(page))
        page.on("framenavigated", lambda frame: self._on_navigated(page, frame))

    def _on_event(self, source: dict, step: Any) -> None:
        page, frame = source.get("page"), source.get("frame")
        if self._queue is None or page is None or frame is not page.main_frame:
            return  # iframes: their selectors would not match the followers' pages
        step = clean_step(step)
        if step is None:
            return
        self._last_input = time.monotonic()
        replay = {"step": step, "origin": origin(frame.url)}
        self._queue.put_nowait({"action": "replay", "params": replay})

    def _on_navigated(self, page: Any, frame: Any) -> None:
        if self._queue is None or frame is not page.main_frame:
            return
        if time.monotonic() - self._last_input < NAV_GRACE_S:
            return
        if frame.url.startswith(("http://", "https://")):
            self._queue.put_nowait({"action": "navigate", "params": {"url": frame.url}})
