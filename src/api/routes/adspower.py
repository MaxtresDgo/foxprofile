"""Import profiles from AdsPower (services/profile/adspower.py).

POST /adspower/profiles {port, api_key}: AdsPower's profiles, each with the
FoxProfile name it gets (`target`) and whether that name is taken.
POST /adspower/import {port, api_key, profile_id, name}: one profile. The panel
walks the selection one by one, so it can show progress without a job queue.
With `reimport`, a profile that already exists gets its cookies refreshed from
AdsPower (merged) and nothing else.

AdsPower runs on the operator's PC. In hub mode the hub cannot reach it: both
calls run on the caller's agent, and the hub creates the profile from what the
agent read.
"""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ...core.config import DATA_DIR
from ...core.logging import get_logger
from ...core.strings import get_string
from ...hub.store import HubError, group_name_for
from ...services.browser import chrome_prefetch
from ...services.browser.launcher import BrowserControlError
from ...services.profile import adspower
from ...utils.validation import validate_profile_name
from ..auth import hub_store
from ..dependencies import get_browser_launcher, get_event_bus, get_profile_manager
from ..helpers import check_on_agent
from .profiles import _cookies_on_agent, _require_engine

logger = get_logger("api.adspower")

router = APIRouter(prefix="/adspower", tags=["adspower"])

# AdsPower answers one request a second: a full list is a page per 100 profiles,
# one profile is four calls.
_LIST_TIMEOUT = 300
_FETCH_TIMEOUT = 60


class AdsPowerAccess(BaseModel):
    port: int = adspower.DEFAULT_PORT
    # In the body, not the URL: it would end up in logs.
    api_key: str = ""


class AdsPowerImport(AdsPowerAccess):
    profile_id: str
    name: str
    # The AdsPower group, from the list: in hub mode the profile joins the hub
    # group of that name (see _join_group).
    group: str = ""
    # When the profile exists already: refresh its cookies instead of skipping it.
    reimport: bool = False


def _here(call):
    try:
        return call()
    except adspower.AdsPowerError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/profiles")
def list_profiles(
    body: AdsPowerAccess,
    request: Request,
    pm=Depends(get_profile_manager),
    bl=Depends(get_browser_launcher),
) -> dict[str, Any]:
    if hub_store(request) is not None:
        items = check_on_agent(request, bl, "adspower_list", body.model_dump(), _LIST_TIMEOUT)
    else:
        items = _here(lambda: adspower.AdsPower(body.port, body.api_key).profiles())
    hub = hub_store(request)
    for item in items:
        item["exists"] = item["target"] in pm.profiles
        if hub is not None and item.get("group"):
            item["hub_group"] = group_name_for(item["group"])
    return {"profiles": items, "engine": adspower.ENGINE}


def _join_group(request: Request, hub, name: str, group: str, warnings: list[str]) -> str | None:
    """Put `name` in the hub group named after its AdsPower group, creating the
    group when an admin imports. The usual rights apply: a lead of that group or
    an admin. Returns the hub group, or None (with a warning) when it cannot."""
    if not group:
        return None
    user, target = request.state.user, group_name_for(group)
    if target not in hub.groups:
        if user["role"] != "admin":
            warnings.append(get_string("adspower_group_missing", group=target))
            return None
        with contextlib.suppress(HubError):  # created meanwhile by another import
            hub.add_group(target)
    if not hub.is_lead(user, target):
        warnings.append(get_string("adspower_group_denied", group=target))
        return None
    if hub.profile_groups.get(name) != target:
        hub.set_profile_group(name, target)
    return target


def _reimport(body: AdsPowerImport, request: Request, pm, bl, hub) -> dict[str, Any]:
    """Refresh an existing profile's cookies from AdsPower; the caller must see it
    and it must be closed (cookies are written to a stopped profile)."""
    if hub is not None and not hub.can(request.state.user, body.name):
        raise HTTPException(status_code=404, detail=get_string("err_not_found", name=body.name))
    if bl.is_running(body.name):
        raise HTTPException(status_code=409, detail=get_string("err_stop_reimport", name=body.name))
    if hub is None:
        client = adspower.AdsPower(body.port, body.api_key)
        return _here(
            lambda: adspower.reimport_here(pm, bl, client.fetch(body.profile_id), body.name)
        )
    params = {"port": body.port, "api_key": body.api_key, "profile_id": body.profile_id}
    data = check_on_agent(request, bl, "adspower_fetch", params, _FETCH_TIMEOUT)
    result: dict[str, Any] = {
        "status": "reimported",
        "name": body.name,
        "cookies": 0,
        "warnings": [],
    }
    if not adspower.has_cookies(data):
        result["warnings"].append(get_string("adspower_no_cookies"))
    else:
        profile = pm.profiles[body.name]
        try:
            imported = _cookies_on_agent(
                request, bl, profile, "import", {"content": data["cookies"]}
            )
        except BrowserControlError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e
        result["cookies"] = imported["imported"]
    result["group"] = _join_group(request, hub, body.name, body.group, result["warnings"])
    logger.info("Reimported the cookies of %s on %s's PC", body.name, request.state.user["name"])
    return result


@router.post("/import")
def import_profile(
    body: AdsPowerImport,
    request: Request,
    pm=Depends(get_profile_manager),
    bl=Depends(get_browser_launcher),
    bus=Depends(get_event_bus),
) -> dict[str, Any]:
    valid, msg = validate_profile_name(body.name)
    if not valid:
        raise HTTPException(status_code=400, detail=msg)
    hub = hub_store(request)
    if body.name in pm.profiles and body.reimport:
        return _reimport(body, request, pm, bl, hub)
    if body.name in pm.profiles:
        result: dict[str, Any] = {"status": "exists", "name": body.name, "warnings": []}
        # Imported before groups came over (or moved since): sort it now.
        if hub is not None and hub.can(request.state.user, body.name):
            result["group"] = _join_group(request, hub, body.name, body.group, result["warnings"])
        return result
    _require_engine(adspower.ENGINE, here=hub is None)

    params = {"port": body.port, "api_key": body.api_key, "profile_id": body.profile_id}
    if hub is None:
        client = adspower.AdsPower(body.port, body.api_key)
        result = _here(
            lambda: adspower.import_here(pm, bl, client.fetch(body.profile_id), body.name, DATA_DIR)
        )
        if result["status"] == "created":
            chrome_prefetch.start()
            bus.emit()
        logger.info("Imported %s from AdsPower (%d cookies)", body.name, result["cookies"])
        return result

    data = check_on_agent(request, bl, "adspower_fetch", params, _FETCH_TIMEOUT)
    result = adspower.create(pm, data, body.name, DATA_DIR)
    result["cookies"] = 0
    if result["status"] != "created":
        return result
    if request.state.user["name"] in hub.users:
        hub.set_owner(body.name, request.state.user["name"])
    result["group"] = _join_group(request, hub, body.name, body.group, result["warnings"])
    bus.emit()
    if adspower.has_cookies(data):
        profile = pm.profiles[body.name]
        try:
            params = {"content": data["cookies"]}
            result["cookies"] = _cookies_on_agent(request, bl, profile, "import", params)[
                "imported"
            ]
        except (BrowserControlError, HTTPException) as e:
            result["warnings"].append(str(getattr(e, "detail", e)))
    logger.info("Imported %s from AdsPower on %s's PC", body.name, request.state.user["name"])
    return result
