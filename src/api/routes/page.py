"""Control the pages of a running profile: navigate, read, click, type, capture."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ...core.strings import get_string
from ...services.browser.launcher import BrowserControlError
from ..dependencies import get_browser_launcher, get_profile_manager
from ..helpers import require_profile
from ..schemas.page import (
    ArrangeRequest,
    ClickAtRequest,
    ClickRequest,
    EvaluateRequest,
    KeyboardTypeRequest,
    NavigateRequest,
    PressRequest,
    SelectRequest,
    TabNewRequest,
    TypeRequest,
    WaitRequest,
)

if TYPE_CHECKING:
    from ...interfaces import IBrowserLauncher, IProfileManager

router = APIRouter(prefix="/browser/{name}/page", tags=["page"])


def _run(
    name: str,
    pm: IProfileManager,
    bl: IBrowserLauncher,
    action: str,
    params: dict[str, Any] | None = None,
) -> Any:
    require_profile(name, pm)
    try:
        return bl.control(name, action, params or {})
    except BrowserControlError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e


_PM = Depends(get_profile_manager)
_BL = Depends(get_browser_launcher)


@router.post("/navigate")
def navigate(name: str, body: NavigateRequest, pm=_PM, bl=_BL) -> dict:
    """Open a URL (http, https or about:) in the active tab."""
    return _run(name, pm, bl, "navigate", body.model_dump())


@router.post("/back")
def back(name: str, pm=_PM, bl=_BL) -> dict:
    return _run(name, pm, bl, "back")


@router.get("/snapshot")
def snapshot(name: str, max_chars: int = Query(40_000, ge=500, le=200_000), pm=_PM, bl=_BL) -> dict:
    """Accessibility tree of the active tab, the best view for choosing selectors."""
    return _run(name, pm, bl, "snapshot", {"max_chars": max_chars})


@router.get("/text")
def text(
    name: str,
    selector: str = "body",
    max_chars: int = Query(40_000, ge=500, le=200_000),
    pm=_PM,
    bl=_BL,
) -> dict:
    return _run(name, pm, bl, "text", {"selector": selector, "max_chars": max_chars})


@router.post("/click")
def click(name: str, body: ClickRequest, pm=_PM, bl=_BL) -> dict:
    """Click the first element matching a Playwright selector (CSS, text=, role=)."""
    return _run(name, pm, bl, "click", body.model_dump())


@router.post("/type")
def type_text(name: str, body: TypeRequest, pm=_PM, bl=_BL) -> dict:
    return _run(name, pm, bl, "type", body.model_dump())


@router.post("/select")
def select(name: str, body: SelectRequest, pm=_PM, bl=_BL) -> dict:
    """Pick an option of a <select> by value or label."""
    return _run(name, pm, bl, "select", body.model_dump())


@router.post("/press")
def press(name: str, body: PressRequest, pm=_PM, bl=_BL) -> dict:
    return _run(name, pm, bl, "press", body.model_dump())


@router.post("/click-at")
def click_at(name: str, body: ClickAtRequest, pm=_PM, bl=_BL) -> dict:
    """Click viewport coordinates (as on a non-full-page screenshot)."""
    return _run(name, pm, bl, "click_at", body.model_dump())


@router.post("/keyboard")
def keyboard_type(name: str, body: KeyboardTypeRequest, pm=_PM, bl=_BL) -> dict:
    """Type text into the focused element."""
    return _run(name, pm, bl, "keyboard_type", body.model_dump())


@router.post("/wait")
def wait_for(name: str, body: WaitRequest, pm=_PM, bl=_BL) -> dict:
    return _run(name, pm, bl, "wait_for", body.model_dump())


@router.get("/screenshot", responses={200: {"content": {"image/png": {}}}})
def screenshot(
    name: str,
    full_page: bool = False,
    format: str = Query("png", pattern="^(png|json)$"),
    quality: int | None = Query(
        None, ge=20, le=90, description="A JPEG of the viewport of this quality instead of a PNG"
    ),
    width: int | None = Query(
        None, ge=80, le=4096, description="With quality: shrink the JPEG to this width (Chrome)"
    ),
    pm=_PM,
    bl=_BL,
) -> Any:
    """PNG of the active tab; `format=json` returns it base64-encoded with url and title.

    With `quality` it is a JPEG of the viewport (the panel's wall), at most `width`
    pixels wide; `X-Viewport-Width`/`-Height` give the page's size in CSS pixels,
    the coordinates `click-at` takes."""
    params: dict[str, Any] = {"full_page": full_page}
    # Only when asked: an agent still on older code rejects unknown parameters.
    if quality is not None:
        params["quality"] = quality
        if width is not None:
            params["width"] = width
    result = _run(name, pm, bl, "screenshot", params)
    if format == "json":
        return result
    headers = {"Cache-Control": "no-store"}
    if "jpeg_base64" not in result:
        return Response(
            base64.b64decode(result["png_base64"]), media_type="image/png", headers=headers
        )
    view = result.get("viewport")
    if view:
        headers["X-Viewport-Width"] = str(view["width"])
        headers["X-Viewport-Height"] = str(view["height"])
    return Response(
        base64.b64decode(result["jpeg_base64"]), media_type="image/jpeg", headers=headers
    )


@router.get("/live")
def live(name: str, pm=_PM, bl=_BL) -> dict:
    """Where to watch the active tab live: a WebSocket on 127.0.0.1 of the PC
    running the browser (`ws://127.0.0.1:{port}/live?t={token}`), so only a panel
    on that PC can use it. `{"available": false}` for Camoufox; the panel then
    takes screenshots instead."""
    return _run(name, pm, bl, "live_ticket")


@router.post("/evaluate")
def evaluate(name: str, body: EvaluateRequest, pm=_PM, bl=_BL) -> dict:
    """Run JavaScript in the active tab and return its JSON-serialisable result."""
    return _run(name, pm, bl, "evaluate", body.model_dump())


@router.get("/tabs")
def tabs(name: str, pm=_PM, bl=_BL) -> dict:
    return _run(name, pm, bl, "tabs")


@router.post("/tabs")
def tab_new(name: str, body: TabNewRequest, pm=_PM, bl=_BL) -> dict:
    return _run(name, pm, bl, "tab_new", body.model_dump())


@router.post("/tabs/{index}/select")
def tab_select(name: str, index: int, pm=_PM, bl=_BL) -> dict:
    return _run(name, pm, bl, "tab_select", {"index": index})


@router.delete("/tabs/{index}")
def tab_close(name: str, index: int, pm=_PM, bl=_BL) -> dict:
    return _run(name, pm, bl, "tab_close", {"index": index})


@router.post("/arrange")
def arrange(name: str, body: ArrangeRequest, pm=_PM, bl=_BL) -> dict:
    """Tile this browser's window: cell `index` of `count` on the screen of the PC
    running it (call it for each window). Chrome engine only. A phone keeps its
    size and stands in a row with the other phones."""
    if require_profile(name, pm).engine != "chrome":
        raise HTTPException(status_code=400, detail=get_string("err_arrange_chrome_only"))
    return _run(name, pm, bl, "arrange", body.model_dump(exclude_none=True))
