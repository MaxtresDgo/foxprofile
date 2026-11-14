"""A hub profile in conflict (hub/conflicts.py): the versions kept from uploads that
started from an older copy, next to the hub's own. Seeing them takes access to the
profile; settling it, an admin or a lead of the profile's group, with it stopped."""

from __future__ import annotations

import contextlib
import os
import pathlib
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ...core.config import DATA_DIR
from ...core.logging import get_logger
from ...core.strings import get_string
from ...hub import conflicts, cookie_history
from ...services.browser.launcher import ProfileBusyError
from ...services.profile.transfer import IntegrityError, read_meta
from ..auth import hub_store
from ..dependencies import get_browser_launcher, get_event_bus, get_profile_manager
from ..helpers import require_profile
from ..schemas.common import ErrorResponse
from .cookie_history import can_restore

if TYPE_CHECKING:
    from ...core.events import EventBus
    from ...interfaces import IBrowserLauncher, IProfileManager

logger = get_logger("api.conflicts")

_ERRORS = {
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
}


def _hub(request: Request) -> None:
    if hub_store(request) is None:
        raise HTTPException(status_code=404, detail=get_string("err_conflict_none"))


router = APIRouter(prefix="/profiles", tags=["conflicts"], dependencies=[Depends(_hub)])


@router.get("/{name}/conflicts", responses=_ERRORS)
def list_conflicts(
    name: str, request: Request, pm: IProfileManager = Depends(get_profile_manager)
) -> dict:
    """The hub's copy (who uploaded it, its cookies) and each version kept apart."""
    profile = require_profile(name, pm)
    meta = read_meta(os.path.join(DATA_DIR, name))
    packed, upload = meta.get("packed") or {}, meta.get("hub_upload") or {}
    hub = {"ts": upload.get("ts"), "by": upload.get("by"), "pc": packed.get("pc", "")}
    hub["cookies"] = packed.get("cookies")
    return {
        "hub": hub,
        "copies": conflicts.entries(profile.uid),
        "can_resolve": can_restore(request, name),
    }


class ResolveBody(BaseModel):
    keep: str  # "hub", or the id of a kept version


@router.post("/{name}/conflicts/resolve", responses=_ERRORS)
def resolve_conflict(
    name: str,
    body: ResolveBody,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    bus: EventBus = Depends(get_event_bus),
) -> dict:
    profile = require_profile(name, pm)
    if not can_restore(request, name):
        raise HTTPException(status_code=403, detail=get_string("err_admins_or_leads"))
    if not conflicts.entries(profile.uid):
        raise HTTPException(status_code=404, detail=get_string("err_conflict_none"))
    by = request.state.user["name"]
    try:
        with bl.exclusive(name):
            if body.keep == "hub":
                conflicts.drop(profile.uid)
            else:
                conflicts.use(profile.uid, body.keep, name, pathlib.Path(DATA_DIR).resolve())
                with contextlib.suppress(OSError, ValueError):  # the history is a bonus
                    cookie_history.add(profile.uid, os.path.join(DATA_DIR, name), by)
    except ProfileBusyError as e:
        raise HTTPException(status_code=409, detail=get_string("err_stop_conflict")) from e
    except conflicts.NotFound as e:
        raise HTTPException(status_code=404, detail=get_string("err_conflict_none")) from e
    except (IntegrityError, ValueError) as e:  # damaged: the hub's copy stays
        raise HTTPException(status_code=422, detail=get_string("err_bundle_corrupt")) from e
    logger.info("[sync %s] hub.conflict_resolved by=%s keep=%s", name, by, body.keep)
    bus.emit()
    return {"kept": body.keep}
