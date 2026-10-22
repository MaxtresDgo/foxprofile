"""Browser extensions: the catalogue, where each one applies, and their files.

An extension applies to every profile of its engine (global), to the profiles of
some hub groups, or to some profiles; `excluded` leaves profiles out of any of
those. In hub mode only admins manage them, while any signed-in caller (an agent
launching a profile) can download a file.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ...core.logging import get_logger
from ...core.strings import get_string
from ...services.browser import extensions as ext
from ..auth import hub_store

logger = get_logger("api.extensions")


def _catalogue(request: Request) -> ext.ExtensionStore:
    return request.app.state.container.extensions


def _manage(request: Request) -> None:
    if hub_store(request) is not None and request.state.user["role"] != "admin":
        raise HTTPException(status_code=403, detail=get_string("err_admins_only"))


router = APIRouter(prefix="/extensions", tags=["extensions"])


def _entry(store: ext.ExtensionStore, ext_id: str) -> dict:
    try:
        return store.get(ext_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=get_string("err_ext_not_found")) from None


def _added(store: ext.ExtensionStore, data: bytes, **kwargs) -> dict:
    try:
        return store.add(data, **kwargs)
    except ext.ExtensionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("", dependencies=[Depends(_manage)])
def list_extensions(store: ext.ExtensionStore = Depends(_catalogue)) -> list[dict]:
    return store.list()


@router.put("/upload", dependencies=[Depends(_manage)])
async def upload_extension(
    request: Request,
    filename: str = Query("", max_length=255),
    engine: str | None = Query(None, pattern="^(chrome|camoufox)$"),
    store: ext.ExtensionStore = Depends(_catalogue),
) -> dict:
    """A .crx or .zip (Chrome) or .xpi (Firefox, for Camoufox) as the request body."""
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > ext.MAX_BYTES:
            raise HTTPException(status_code=413, detail=get_string("err_ext_too_big"))
    return await run_in_threadpool(_added, store, bytes(data), filename=filename, engine=engine)


class StoreBody(BaseModel):
    ref: str = Field(min_length=1, max_length=500, description="Chrome Web Store ID or URL")


@router.post("/store", dependencies=[Depends(_manage)])
def add_from_store(body: StoreBody, store: ext.ExtensionStore = Depends(_catalogue)) -> dict:
    """Download an extension from the Chrome Web Store (a call to Google, only when asked)."""
    from chrome_engine.release import DEFAULT_VERSION

    try:
        data, store_id = ext.from_store(body.ref, DEFAULT_VERSION)
    except ext.ExtensionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _added(store, data, engine="chrome", source="store", store_id=store_id)


class ScopeBody(BaseModel):
    global_: bool | None = Field(None, alias="global")
    groups: list[str] | None = Field(None, max_length=500)
    profiles: list[str] | None = Field(None, max_length=5000)
    excluded: list[str] | None = Field(None, max_length=5000)


@router.patch("/{ext_id}", dependencies=[Depends(_manage)])
def set_scope(
    ext_id: str, body: ScopeBody, store: ext.ExtensionStore = Depends(_catalogue)
) -> dict:
    _entry(store, ext_id)
    scope = body.model_dump(exclude_none=True)
    if "global_" in scope:
        scope["global"] = scope.pop("global_")
    return store.set_scope(ext_id, **scope)


@router.delete("/{ext_id}", dependencies=[Depends(_manage)])
def delete_extension(ext_id: str, store: ext.ExtensionStore = Depends(_catalogue)) -> dict:
    entry = _entry(store, ext_id)
    store.delete(ext_id)
    logger.info("Extension %s (%s) deleted", entry["name"], ext_id)
    return {"id": ext_id}


@router.get("/{ext_id}/file")
def extension_file(ext_id: str, store: ext.ExtensionStore = Depends(_catalogue)) -> FileResponse:
    """The normalized ZIP an agent unpacks; X-Content-SHA256 to check it against."""
    entry = _entry(store, ext_id)
    return FileResponse(
        store.archive(ext_id),
        media_type="application/zip",
        headers={"X-Content-SHA256": entry["sha256"]},
    )
