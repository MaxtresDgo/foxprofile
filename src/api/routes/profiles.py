from __future__ import annotations

import os
import pathlib
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from ...core.config import DATA_DIR
from ...core.logging import get_logger
from ...core.strings import get_string
from ...models.profile import ENGINES
from ...services.browser import chrome_prefetch, cookies, fingerprint
from ...services.browser.launcher import BrowserControlError, ProfileBusyError
from ...utils.validation import (
    validate_locale,
    validate_profile_name,
    validate_proxy_format,
    validate_timezone,
)
from ..auth import can_see, hub_store
from ..dependencies import get_browser_launcher, get_event_bus, get_profile_manager
from ..helpers import build_profile_response, profile_response, require_profile
from ..schemas.common import ErrorResponse, SuccessResponse
from ..schemas.profiles import (
    CookieImportRequest,
    CookieImportResponse,
    DataDirResponse,
    ExportRequest,
    ExportResponse,
    FingerprintResponse,
    ImportRequest,
    ImportResponse,
    ProfileCreate,
    ProfileListResponse,
    ProfileResponse,
    ProfileUpdate,
)
from ..schemas.proxy import GeoCheckResponse
from .proxy import geo_check_here_or_on_agent

if TYPE_CHECKING:
    from ...core.events import EventBus
    from ...interfaces import IBrowserLauncher, IProfileManager

logger = get_logger("api.profiles")

router = APIRouter(prefix="/profiles", tags=["profiles"])

_OS_TYPES = ("windows", "macos", "linux", "android")


def _require_os(os_type: str, engine: str) -> None:
    if os_type not in _OS_TYPES:
        raise HTTPException(
            status_code=400,
            detail=get_string("err_os_type", values=", ".join(_OS_TYPES)),
        )
    if os_type == "android" and engine != "chrome":
        # Camoufox is Firefox: it has no Android device to present.
        raise HTTPException(status_code=400, detail=get_string("err_android_chrome"))


def _require_engine(engine: str, here: bool = True) -> None:
    """`here`: the browser runs on this machine (not on a hub agent's PC)."""
    if engine not in ENGINES:
        raise HTTPException(
            status_code=400,
            detail=get_string("err_engine", values=", ".join(ENGINES)),
        )
    if here and engine == "chrome" and (reason := chrome_prefetch.unsupported_reason()):
        raise HTTPException(
            status_code=400, detail=get_string("err_chrome_unavailable", reason=reason)
        )


def _not_hub(request: Request) -> None:
    """Routes that need a browser or a file path on the server itself."""
    if hub_store(request) is not None:
        raise HTTPException(status_code=501, detail=get_string("err_hub_only"))


def _require_geo(timezone: str | None, locale: str | None) -> None:
    for validate, value in ((validate_timezone, timezone), (validate_locale, locale)):
        valid, msg = validate(value or "")
        if not valid:
            raise HTTPException(status_code=400, detail=msg)


@router.get("", response_model=ProfileListResponse)
def list_profiles(
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
) -> ProfileListResponse:
    profiles = [profile_response(p, bl) for p in pm.list_profiles() if can_see(request, p.name)]
    return ProfileListResponse(profiles=profiles, total=len(profiles))


@router.post(
    "",
    response_model=ProfileResponse,
    status_code=201,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def create_profile(
    body: ProfileCreate,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    bus: EventBus = Depends(get_event_bus),
) -> ProfileResponse:
    valid, msg = validate_profile_name(body.name)
    if not valid:
        raise HTTPException(status_code=400, detail=msg)
    _require_os(body.os_type, body.engine)
    hub = hub_store(request)
    _require_engine(body.engine, here=hub is None)
    _require_geo(body.timezone, body.locale)

    if body.proxy:
        valid, msg = validate_proxy_format(body.proxy)
        if not valid:
            raise HTTPException(status_code=400, detail=msg)

    if not pm.add_profile(
        body.name,
        body.proxy or "",
        body.os_type,
        body.timezone or None,
        body.locale or None,
        body.engine,
    ):
        raise HTTPException(status_code=409, detail=get_string("err_profile_exists"))

    if hub is not None and request.state.user["name"] in hub.users:
        hub.set_owner(body.name, request.state.user["name"])
    logger.info("API created profile: %s", body.name)
    if body.engine == "chrome" and hub is None:
        chrome_prefetch.start()
    bus.emit()
    return build_profile_response(body.name, pm, bl)


@router.get(
    "/{name}",
    response_model=ProfileResponse,
    responses={404: {"model": ErrorResponse}},
)
def get_profile(
    name: str,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
) -> ProfileResponse:
    require_profile(name, pm)
    return build_profile_response(name, pm, bl)


@router.patch(
    "/{name}",
    response_model=ProfileResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def update_profile(
    name: str,
    body: ProfileUpdate,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    bus: EventBus = Depends(get_event_bus),
) -> ProfileResponse:
    profile = require_profile(name, pm)
    supplied = body.model_dump(exclude_unset=True)

    new_name = supplied.get("name", name)
    new_proxy = supplied.get("proxy", profile.proxy)
    new_os = supplied.get("os_type", profile.os_type)
    new_timezone = supplied.get("timezone", profile.timezone)
    new_locale = supplied.get("locale", profile.locale)

    if "name" in supplied:
        valid, msg = validate_profile_name(new_name)
        if not valid:
            raise HTTPException(status_code=400, detail=msg)
        if new_name != name and bl.is_running(name):
            raise HTTPException(
                status_code=409,
                detail=get_string("stop_before_rename"),
            )

    if supplied.get("engine") not in (None, profile.engine):
        raise HTTPException(
            status_code=400,
            detail=get_string("err_engine_locked"),
        )
    if "os_type" in supplied:
        _require_os(new_os, profile.engine)
    _require_geo(supplied.get("timezone"), supplied.get("locale"))

    if "proxy" in supplied and new_proxy:
        valid, msg = validate_proxy_format(new_proxy)
        if not valid:
            raise HTTPException(status_code=400, detail=msg)

    if not pm.update_profile(
        name, new_name, new_proxy or "", new_os, new_timezone or None, new_locale or None
    ):
        raise HTTPException(status_code=409, detail=get_string("err_update_failed"))
    if (store := hub_store(request)) is not None and new_name != name:
        store.rename_profile(name, new_name)

    logger.info("API updated profile: %s -> %s", name, new_name)
    bus.emit()
    return build_profile_response(new_name, pm, bl)


@router.delete(
    "/{name}",
    response_model=SuccessResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def delete_profile(
    name: str,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    bus: EventBus = Depends(get_event_bus),
) -> SuccessResponse:
    require_profile(name, pm)
    if bl.is_running(name):
        raise HTTPException(
            status_code=409,
            detail=get_string("err_stop_delete"),
        )
    pm.delete_profile(name)
    if (store := hub_store(request)) is not None:
        store.forget_profile(name)
    logger.info("API deleted profile: %s", name)
    bus.emit()
    return SuccessResponse(message=f"Profile '{name}' deleted")


@router.get(
    "/{name}/data-dir",
    response_model=DataDirResponse,
    responses={404: {"model": ErrorResponse}},
)
def get_data_dir(
    name: str,
    pm: IProfileManager = Depends(get_profile_manager),
) -> DataDirResponse:
    require_profile(name, pm)
    data_dir = os.path.join(os.getcwd(), DATA_DIR, name)
    return DataDirResponse(
        name=name,
        data_dir=data_dir,
        exists=pathlib.Path(data_dir).exists(),
    )


@router.post(
    "/{name}/export",
    dependencies=[Depends(_not_hub)],
    response_model=ExportResponse,
    responses={404: {"model": ErrorResponse}},
)
def export_profile(
    name: str,
    body: ExportRequest,
    pm: IProfileManager = Depends(get_profile_manager),
) -> ExportResponse:
    require_profile(name, pm)
    if not pathlib.Path(body.export_dir).is_dir():
        raise HTTPException(status_code=400, detail=get_string("err_export_dir"))

    success, result = pm.export_profile(name, body.export_dir, body.include_data)
    if success:
        logger.info("API exported profile: %s -> %s", name, result)
        return ExportResponse(success=True, zip_path=result)
    return ExportResponse(success=False, error=result)


@router.post(
    "/import",
    dependencies=[Depends(_not_hub)],
    response_model=ImportResponse,
    responses={400: {"model": ErrorResponse}},
)
def import_profile(
    body: ImportRequest,
    pm: IProfileManager = Depends(get_profile_manager),
    bus: EventBus = Depends(get_event_bus),
) -> ImportResponse:
    if not pathlib.Path(body.zip_path).is_file():
        raise HTTPException(status_code=400, detail=get_string("err_zip_path"))

    success, result = pm.import_profile(body.zip_path, body.overwrite)
    if success:
        logger.info("API imported profile: %s", result)
        bus.emit()
        return ImportResponse(success=True, profile_name=result)
    return ImportResponse(success=False, error=result)


def _require_stopped(name: str, bl: IBrowserLauncher, key: str) -> None:
    if bl.is_running(name):
        raise HTTPException(status_code=409, detail=get_string(key))


def _cookies_on_agent(request: Request, bl, profile, action: str, params: dict) -> dict:
    """Hub mode: run the cookie tool on the caller's PC, where browsers exist."""
    from ...hub.remote import AgentOfflineError

    try:
        return bl.cookies(request.state.user["name"], profile, action, params)
    except AgentOfflineError as e:
        raise HTTPException(status_code=409, detail=get_string("err_agent_offline")) from e


@router.get(
    "/{name}/cookies",
    response_class=PlainTextResponse,
    responses={
        200: {"content": {"text/plain": {}, "application/json": {}}},
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
)
def export_cookies(
    name: str,
    request: Request,
    format: str = Query("json", pattern="^(json|netscape)$"),
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
) -> PlainTextResponse:
    """Export cookies as Cookie-Editor JSON or Netscape cookies.txt."""
    profile = require_profile(name, pm)
    _require_stopped(name, bl, "err_stop_export_cookies")
    try:
        if hub_store(request) is not None:
            r = _cookies_on_agent(request, bl, profile, "export", {"format": format})
            text, count = r["text"], r["count"]
        else:
            with bl.exclusive(name):
                text, count = cookies.export_cookies(name, profile.os_type, format, profile.engine)
    except ProfileBusyError as e:
        raise HTTPException(status_code=409, detail=get_string("err_busy_other")) from e
    except (cookies.CookieError, BrowserControlError) as e:
        status = getattr(e, "status", 400)
        raise HTTPException(status_code=status, detail=str(e)) from e
    media = "application/json" if format == "json" else "text/plain"
    return PlainTextResponse(text, media_type=media, headers={"X-Cookie-Count": str(count)})


@router.post(
    "/{name}/cookies",
    response_model=CookieImportResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def import_cookies(
    name: str,
    body: CookieImportRequest,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
) -> CookieImportResponse:
    """Import cookies from JSON or Netscape text into a stopped profile."""
    profile = require_profile(name, pm)
    _require_stopped(name, bl, "err_stop_import_cookies")
    try:
        if hub_store(request) is not None:
            params = {"content": body.content}
            count = _cookies_on_agent(request, bl, profile, "import", params)["imported"]
        else:
            with bl.exclusive(name):
                count = cookies.import_cookies(name, profile.os_type, body.content, profile.engine)
    except ProfileBusyError as e:
        raise HTTPException(status_code=409, detail=get_string("err_busy_other")) from e
    except cookies.CookieError as e:
        return CookieImportResponse(success=False, error=str(e))
    except BrowserControlError as e:
        if e.status != 400:
            raise HTTPException(status_code=e.status, detail=str(e)) from e
        return CookieImportResponse(success=False, error=str(e))
    logger.info("API imported %d cookies into %s", count, name)
    return CookieImportResponse(success=True, imported=count)


@router.get(
    "/{name}/fingerprint",
    response_model=FingerprintResponse,
    responses={404: {"model": ErrorResponse}},
)
def get_fingerprint(
    name: str,
    pm: IProfileManager = Depends(get_profile_manager),
) -> FingerprintResponse:
    """Show the fingerprint the profile presents on every launch."""
    require_profile(name, pm)
    info = fingerprint.summary(os.path.join(DATA_DIR, name))
    if info is None:
        return FingerprintResponse(name=name, exists=False)
    return FingerprintResponse(name=name, exists=True, **info)


@router.delete(
    "/{name}/fingerprint",
    response_model=SuccessResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def reset_fingerprint(
    name: str,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
) -> SuccessResponse:
    """Discard the saved fingerprint; the next launch generates a new device."""
    require_profile(name, pm)
    _require_stopped(name, bl, "err_stop_reset_fp")
    removed = fingerprint.reset(os.path.join(DATA_DIR, name))
    logger.info("API reset fingerprint for %s (existed=%s)", name, removed)
    return SuccessResponse(message=f"Fingerprint reset for '{name}'")


@router.get(
    "/{name}/ip-check",
    response_model=GeoCheckResponse,
    responses={404: {"model": ErrorResponse}},
)
def check_profile_ip(
    name: str,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
) -> GeoCheckResponse:
    """Where GeoIP sources place the profile's exit IP vs. the timezone/locale it presents.

    Goes out through the profile's proxy to Cloudflare, ipinfo and ip-api (~10s).
    """
    profile = require_profile(name, pm)
    return geo_check_here_or_on_agent(request, bl, profile.proxy, profile.timezone, profile.locale)
