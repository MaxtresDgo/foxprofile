"""Run steps on many profiles at once, and sync a leader's actions to followers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...core.strings import get_string
from ...services.browser.launcher import BrowserControlError
from ...services.browser.steps import StepError, parse_script, run_steps, validate_steps
from ...services.browser.sync import SyncError
from ..auth import can_see
from ..dependencies import get_browser_launcher, get_profile_manager, get_sync_manager
from ..helpers import require_profile

if TYPE_CHECKING:
    from ...interfaces import IBrowserLauncher, IProfileManager
    from ...services.browser.sync import SyncManager

router = APIRouter(prefix="/automation", tags=["automation"])

MAX_PROFILES = 200


class RunRequest(BaseModel):
    profiles: list[str] = Field(min_length=1, max_length=MAX_PROFILES)
    script: str | None = Field(
        None,
        max_length=100_000,
        description="One step per line, e.g. 'navigate https://example.com', "
        "'type #email | user@example.com'. Takes precedence over `steps`.",
    )
    steps: list[dict[str, Any]] | None = Field(
        None,
        description='JSON steps: [{"action": "click", "params": {"selector": "#go"}}]',
    )
    stop_on_error: bool = Field(True, description="Stop a profile's run at its first failed step")


class SyncRequest(BaseModel):
    leader: str
    followers: list[str] = Field(min_length=1, max_length=MAX_PROFILES)


def _running(names: list[str], request: Request, pm: IProfileManager, bl: IBrowserLauncher):
    """Each profile, checked like a `{name}` route would: visible, existing, running."""
    for name in names:
        if not can_see(request, name):
            # Body names skip require_token's path check: same 404 as for others' profiles.
            raise HTTPException(status_code=404, detail=get_string("err_not_found", name=name))
        require_profile(name, pm)
        if not bl.is_running(name):
            raise HTTPException(status_code=409, detail=get_string("err_not_running", name=name))


@router.post("/run")
def run(
    body: RunRequest,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
) -> dict:
    """Run the steps on every profile in parallel (in order within each one).

    Answers when all are done, with {name: {ok, done, error}} per profile."""
    try:
        steps = (
            parse_script(body.script)
            if body.script is not None
            else validate_steps(body.steps or [])
        )
    except StepError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    names = list(dict.fromkeys(body.profiles))
    _running(names, request, pm, bl)
    return {"results": run_steps(bl, names, steps, body.stop_on_error)}


@router.get("/sync")
def list_syncs(request: Request, sync: SyncManager = Depends(get_sync_manager)) -> dict:
    return {
        "sessions": [s.as_dict() for s in sync.sessions() if can_see(request, s.leader)],
    }


@router.post("/sync", status_code=201)
def start_sync(
    body: SyncRequest,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    sync: SyncManager = Depends(get_sync_manager),
) -> dict:
    """Repeat what the user does in the leader's pages in the followers' active tabs."""
    _running([body.leader, *body.followers], request, pm, bl)
    try:
        return sync.start(body.leader, body.followers).as_dict()
    except SyncError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except BrowserControlError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e


@router.delete("/sync/{name}")
def stop_sync(name: str, sync: SyncManager = Depends(get_sync_manager)) -> dict:
    if not sync.stop(name):
        raise HTTPException(status_code=404, detail=get_string("err_not_leading", name=name))
    return {"success": True, "message": f"Sync from '{name}' stopped"}
