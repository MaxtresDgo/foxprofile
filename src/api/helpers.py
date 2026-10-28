"""Shared helpers for API routes (DRY)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request

from ..core.config import DATA_DIR
from ..core.strings import get_string
from ..services.browser.launcher import BrowserControlError
from .schemas.profiles import ProfileResponse

if TYPE_CHECKING:
    from ..interfaces import IBrowserLauncher, IProfileManager
    from ..models.profile import Profile


def check_on_agent(request: Request, bl, op: str, params: dict, timeout: float) -> Any:
    """Hub mode: run a proxy check on the caller's PC, where the browsers use the proxy."""
    from ..hub.remote import AgentOfflineError

    try:
        return bl.run_check(request.state.user["name"], op, params, timeout)
    except AgentOfflineError as e:
        raise HTTPException(status_code=409, detail=get_string("err_agent_offline")) from e
    except BrowserControlError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e


def require_profile(name: str, pm: IProfileManager) -> Profile:
    """The profile, or 404. Use the returned object rather than indexing
    pm.profiles again: a concurrent delete can remove it in between."""
    profile = pm.profiles.get(name)
    if profile is None:
        raise HTTPException(status_code=404, detail=get_string("err_not_found", name=name))
    return profile


def build_profile_response(
    name: str,
    pm: IProfileManager,
    bl: IBrowserLauncher,
) -> ProfileResponse:
    """Build a ProfileResponse DTO for the given profile name."""
    return profile_response(require_profile(name, pm), bl)


def profile_response(profile: Profile, bl: IBrowserLauncher) -> ProfileResponse:
    return ProfileResponse(
        name=profile.name,
        proxy=profile.proxy,
        os_type=profile.os_type,
        timezone=profile.timezone,
        locale=profile.locale,
        engine=profile.engine,
        number=profile.number,
        remark=profile.remark,
        data_dir=os.path.join(os.getcwd(), DATA_DIR, profile.name),
        is_running=bl.is_running(profile.name),
    )
