"""A hub profile's cookie history (hub/cookie_history.py): list the packs kept on
each upload and put one back. Seeing it takes access to the profile (require_token
404s the others); restoring, an admin or a lead of the profile's group."""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request

from ...core.config import COOKIE_HISTORY, DATA_DIR
from ...core.logging import get_logger
from ...core.strings import get_string
from ...hub import cookie_history
from ...services.browser.launcher import ProfileBusyError
from ..auth import hub_store
from ..dependencies import get_browser_launcher, get_event_bus, get_profile_manager
from ..helpers import require_profile
from ..schemas.common import ErrorResponse

if TYPE_CHECKING:
    from ...core.events import EventBus
    from ...interfaces import IBrowserLauncher, IProfileManager

logger = get_logger("api.cookie_history")

_ERRORS = {
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
}


def _enabled(request: Request) -> None:
    if hub_store(request) is None or not COOKIE_HISTORY:
        raise HTTPException(status_code=404, detail=get_string("err_cookie_history_off"))


router = APIRouter(prefix="/profiles", tags=["cookie history"], dependencies=[Depends(_enabled)])


def can_restore(request: Request, name: str) -> bool:
    store = hub_store(request)
    group = store.profile_groups.get(name)
    user = request.state.user
    return user["role"] == "admin" or (group is not None and store.is_lead(user, group))


@router.get("/{name}/cookie-history", responses=_ERRORS)
def list_history(
    name: str, request: Request, pm: IProfileManager = Depends(get_profile_manager)
) -> dict:
    profile = require_profile(name, pm)
    return {
        "entries": cookie_history.entries(profile.uid),
        "can_restore": can_restore(request, name),
    }


@router.post("/{name}/cookie-history/{entry_id}/restore", responses=_ERRORS)
def restore_history(
    name: str,
    entry_id: str,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    bus: EventBus = Depends(get_event_bus),
) -> dict:
    profile = require_profile(name, pm)
    if not can_restore(request, name):
        raise HTTPException(status_code=403, detail=get_string("err_admins_or_leads"))
    by = request.state.user["name"]
    try:
        with bl.exclusive(name):
            entry = cookie_history.restore(
                profile.uid, entry_id, str(pathlib.Path(DATA_DIR, name)), by
            )
    except ProfileBusyError as e:
        raise HTTPException(status_code=409, detail=get_string("err_stop_cookie_history")) from e
    except cookie_history.NotFound as e:
        raise HTTPException(status_code=404, detail=get_string("err_cookie_history_none")) from e
    except (OSError, ValueError) as e:  # damaged on disk: the hub copy stays as it was
        logger.warning("Cookie history restore of %s failed: %s", name, e)
        raise HTTPException(status_code=422, detail=get_string("err_bundle_corrupt")) from e
    logger.info("[sync %s] hub.cookie_restore by=%s entry=%s", name, by, entry_id)
    bus.emit()
    return {"id": entry["id"], "cookies": entry["cookies"]}
