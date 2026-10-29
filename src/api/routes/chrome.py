"""The Chrome engine builds (chrome_engine/release.py) of the PC that runs the browsers.

That PC is this machine, or on a hub the operator's agent, which reports its
platform and installed builds on every poll (hub/routes.poll). A Chrome profile
opens only once its build is installed there (routes/browser.py).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ...core.strings import get_string
from ...services.browser import chrome_prefetch
from ..auth import hub_store
from ..dependencies import get_browser_launcher, get_event_bus

if TYPE_CHECKING:
    from ...core.events import EventBus
    from ...interfaces import IBrowserLauncher

router = APIRouter(prefix="/chrome", tags=["chrome"])


def builds_state(request: Request, bl: IBrowserLauncher) -> dict:
    """{online, platform, installed, installing, errors} of the PC that runs the browsers."""
    if hub_store(request) is None:
        from chrome_engine import fetch

        return {
            "online": True,
            "platform": chrome_prefetch.host(),
            "installed": fetch.installed_versions() if chrome_prefetch.host() else [],
            "installing": chrome_prefetch.installing(),
            "errors": chrome_prefetch.failures(),
        }
    user = request.state.user["name"]
    agent = bl.agents.get(user, {})
    activity = agent.get("activity") or {}
    building = activity.get("build")
    return {
        "online": bl.online(user),
        "platform": agent.get("os") or None,
        "installed": agent.get("builds") or [],
        "installing": {building: activity.get("pct", -1)} if building else {},
        "errors": agent.get("build_errors") or {},
    }


@router.get("/builds")
def list_builds(request: Request, bl: IBrowserLauncher = Depends(get_browser_launcher)) -> dict:
    """Every pinned build, and which of them that PC can install or has."""
    from chrome_engine import release

    state = builds_state(request, bl)
    return {
        "online": state["online"],
        "platform": state["platform"],
        "default": release.DEFAULT_VERSION,
        "installing": [{"version": v, "pct": p} for v, p in state["installing"].items()],
        "builds": [
            {
                "version": v,
                "platforms": sorted(release.RELEASES[v].assets),
                "available": state["platform"] in release.RELEASES[v].assets,
                "installed": v in state["installed"],
                # Why its last install failed (cleared when it is tried again).
                "error": state["errors"].get(v),
            }
            for v in sorted(release.RELEASES, key=release.key, reverse=True)
        ],
    }


@router.post("/builds/{version}/install", status_code=202)
def install_build(
    version: str,
    request: Request,
    response: Response,
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    bus: EventBus = Depends(get_event_bus),
) -> dict:
    """Start installing a build on that PC; GET /chrome/builds shows the progress."""
    from chrome_engine import release

    state = builds_state(request, bl)
    if version not in release.RELEASES or state["platform"] not in release.RELEASES[version].assets:
        raise HTTPException(
            status_code=400, detail=get_string("err_chrome_version_host", version=version)
        )
    if version in state["installed"]:
        response.status_code = 200
        return {"ok": True, "installed": True}
    if hub_store(request) is not None:
        from ...hub.remote import AgentOfflineError

        try:
            bl.install_chrome(request.state.user["name"], version)
        except AgentOfflineError as e:
            raise HTTPException(status_code=409, detail=get_string("err_agent_offline")) from e
    else:
        chrome_prefetch.start(version, on_done=bus.emit)
    return {"ok": True, "installed": False}


def require_build(request: Request, bl: IBrowserLauncher, version: str | None) -> None:
    """Refuse to open a Chrome profile whose build that PC has not installed."""
    if version and version not in builds_state(request, bl)["installed"]:
        raise HTTPException(
            status_code=409, detail=get_string("err_chrome_build_missing", version=version)
        )
