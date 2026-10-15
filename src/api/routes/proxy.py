from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request

from ...services.proxy.geo_check import check_geo
from ...utils.validation import validate_locale, validate_proxy_format, validate_timezone
from ..auth import hub_store
from ..dependencies import get_browser_launcher, get_proxy_service
from ..helpers import check_on_agent
from ..schemas.proxy import GeoCheckRequest, GeoCheckResponse, ProxyCheckRequest, ProxyCheckResponse

if TYPE_CHECKING:
    from ...interfaces import IProxyService

router = APIRouter(prefix="/proxy", tags=["proxy"])


@router.post("/check", response_model=ProxyCheckResponse)
def check_proxy(
    body: ProxyCheckRequest,
    request: Request,
    ps: IProxyService = Depends(get_proxy_service),
    bl=Depends(get_browser_launcher),
) -> ProxyCheckResponse:
    timeout = body.timeout or 10
    if hub_store(request) is not None:
        params = {"proxy": body.proxy, "timeout": timeout}
        r = check_on_agent(request, bl, "proxy_check", params, timeout + 15)
        return ProxyCheckResponse(success=r["ok"], message=r["message"])
    ok, message = ps.check_proxy_sync(body.proxy, timeout)
    return ProxyCheckResponse(success=ok, message=message)


@router.post("/geo-check", response_model=GeoCheckResponse)
def geo_check(
    body: GeoCheckRequest, request: Request, bl=Depends(get_browser_launcher)
) -> GeoCheckResponse:
    """Check an unsaved proxy / timezone / locale combination (see GET /profiles/{name}/ip-check)."""
    for validate, value in (
        (validate_proxy_format, body.proxy),
        (validate_timezone, body.timezone),
        (validate_locale, body.locale),
    ):
        valid, msg = validate(value or "")
        if not valid:
            raise HTTPException(status_code=400, detail=msg)
    return geo_check_here_or_on_agent(
        request, bl, body.proxy or None, body.timezone or None, body.locale or None
    )


def geo_check_here_or_on_agent(
    request: Request, bl, proxy: str | None, timezone: str | None, locale: str | None
) -> GeoCheckResponse:
    if hub_store(request) is not None:
        params = {"proxy": proxy, "timezone": timezone, "locale": locale}
        return GeoCheckResponse(**check_on_agent(request, bl, "geo_check", params, 60))
    return GeoCheckResponse.from_result(check_geo(proxy, timezone, locale))
