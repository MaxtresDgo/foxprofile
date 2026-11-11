from __future__ import annotations

import json
import os
import pathlib
import secrets
import threading
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...core.config import PROFILES_FILE
from ...core.strings import get_string
from ...services.proxy.geo_check import check_geo
from ...utils.proxy_parser import parse_proxy
from ...utils.validation import validate_locale, validate_proxy_format, validate_timezone
from ..auth import hub_store
from ..dependencies import get_browser_launcher, get_profile_manager, get_proxy_service
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


# --- Saved proxies: a directory admins keep (Settings › Proxies) -------------------
# Saved entries live in proxies.json next to profiles.json; the proxies profiles already
# use join the list on their own, each with the profiles that use it.

_saved_lock = threading.Lock()


class SavedProxy(BaseModel):
    proxy: str = Field(min_length=1, max_length=500)
    note: str = Field("", max_length=100)


def _manage(request: Request) -> None:
    if hub_store(request) is not None and request.state.user["role"] != "admin":
        raise HTTPException(status_code=403, detail=get_string("err_admins_only"))


def _saved_file() -> pathlib.Path:
    return pathlib.Path(PROFILES_FILE).with_name("proxies.json")


def _load_saved() -> list[dict]:
    try:
        return json.loads(_saved_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def _store_saved(items: list[dict]) -> None:
    """Caller holds _saved_lock."""
    path = _saved_file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _key(proxy: str) -> str:
    """host:port:user:pass and http://user:pass@host:port are the same proxy."""
    return json.dumps(parse_proxy(proxy) or proxy.strip(), sort_keys=True)


def _checked(body: SavedProxy, items: list[dict], skip: str | None = None) -> dict:
    proxy = body.proxy.strip()
    valid, msg = validate_proxy_format(proxy)
    if not valid:
        raise HTTPException(status_code=400, detail=msg)
    if any(s["id"] != skip and _key(s["proxy"]) == _key(proxy) for s in items):
        raise HTTPException(status_code=409, detail=get_string("err_proxy_saved"))
    return {"proxy": proxy, "note": body.note.strip()}


def _index(items: list[dict], sid: str) -> int:
    i = next((i for i, s in enumerate(items) if s["id"] == sid), None)
    if i is None:
        raise HTTPException(status_code=404, detail=get_string("err_not_found", name=sid))
    return i


@router.get("/saved", dependencies=[Depends(_manage)])
def list_saved(pm=Depends(get_profile_manager)) -> list[dict]:
    """Saved proxies first, then the unsaved ones profiles use (id None)."""
    used: dict[str, tuple[str, list[str]]] = {}
    for p in pm.list_profiles():
        if p.proxy:
            used.setdefault(_key(p.proxy), (p.proxy, []))[1].append(p.name)
    rows = [{**s, "profiles": used.pop(_key(s["proxy"]), ("", []))[1]} for s in _load_saved()]
    rows += [
        {"id": None, "proxy": proxy, "note": "", "profiles": names}
        for proxy, names in used.values()
    ]
    return rows


@router.post("/saved", status_code=201, dependencies=[Depends(_manage)])
def add_saved(body: SavedProxy) -> dict:
    with _saved_lock:
        items = _load_saved()
        entry = {"id": secrets.token_hex(6), **_checked(body, items)}
        _store_saved([*items, entry])
    return entry


@router.put("/saved/{sid}", dependencies=[Depends(_manage)])
def update_saved(sid: str, body: SavedProxy) -> dict:
    with _saved_lock:
        items = _load_saved()
        i = _index(items, sid)
        items[i] = {"id": sid, **_checked(body, items, skip=sid)}
        _store_saved(items)
    return items[i]


@router.delete("/saved/{sid}", dependencies=[Depends(_manage)])
def delete_saved(sid: str) -> dict:
    with _saved_lock:
        items = _load_saved()
        items.pop(_index(items, sid))
        _store_saved(items)
    return {"deleted": sid}
