"""Snapshots in the bucket (services/profile/snapshots.py). Per profile: list them,
save one of a stopped profile, put one back. For the whole bucket (Settings,
admins in hub mode): every profile's snapshots by uid, including those of deleted
profiles (orphans), which can be deleted or put back as a profile. 404 while
snapshots are off."""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ...core.config import DATA_DIR
from ...core.logging import get_logger
from ...core.strings import get_string
from ...models.profile import ENGINES, UID_RE
from ...services.browser.launcher import ProfileBusyError
from ...services.profile import snapshots
from ...services.profile.transfer import IntegrityError
from ...utils.validation import (
    valid_or_none,
    validate_locale,
    validate_profile_name,
    validate_timezone,
)
from ..auth import hub_store
from ..dependencies import get_browser_launcher, get_event_bus, get_profile_manager
from ..helpers import require_profile
from ..schemas.common import ErrorResponse

if TYPE_CHECKING:
    from ...core.events import EventBus
    from ...interfaces import IBrowserLauncher, IProfileManager

logger = get_logger("api.snapshots")

_ERRORS = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
}


def _enabled() -> None:
    if not snapshots.enabled():
        raise HTTPException(status_code=404, detail=get_string("err_snapshots_off"))


router = APIRouter(prefix="/profiles", tags=["snapshots"], dependencies=[Depends(_enabled)])
bucket_router = APIRouter(prefix="/snapshots", tags=["snapshots"], dependencies=[Depends(_enabled)])


def _store_error(e: Exception) -> HTTPException:
    logger.warning("Snapshot store: %s", e)
    return HTTPException(status_code=502, detail=get_string("err_snapshot_store", error=str(e)))


def _restore(bl: IBrowserLauncher, profile, snap_id: str) -> None:
    try:
        with bl.exclusive(profile.name):
            snapshots.restore(profile, snap_id, pathlib.Path(DATA_DIR).resolve())
    except ProfileBusyError as e:
        raise HTTPException(status_code=409, detail=get_string("err_stop_snapshot")) from e
    except snapshots.SnapshotNotFound as e:
        raise HTTPException(status_code=404, detail=get_string("err_snapshot_not_found")) from e
    except (IntegrityError, ValueError) as e:  # damaged: the data stays as it was
        raise HTTPException(status_code=422, detail=get_string("err_bundle_corrupt")) from e
    except Exception as e:
        raise _store_error(e) from e


@router.get("/{name}/snapshots", responses=_ERRORS)
def list_snapshots(name: str, pm: IProfileManager = Depends(get_profile_manager)) -> dict:
    profile = require_profile(name, pm)
    try:
        return {"snapshots": snapshots.list_snapshots(profile.uid)}
    except Exception as e:
        raise _store_error(e) from e


@router.post("/{name}/snapshots", responses=_ERRORS)
def create_snapshot(
    name: str,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
) -> dict:
    profile = require_profile(name, pm)
    try:
        with bl.exclusive(name):
            snap_id = snapshots.create(profile, str(pathlib.Path(DATA_DIR, name)))
    except ProfileBusyError as e:
        raise HTTPException(status_code=409, detail=get_string("err_stop_snapshot")) from e
    except Exception as e:
        raise _store_error(e) from e
    return {"id": snap_id}


@router.post("/{name}/snapshots/{snap_id}/restore", responses=_ERRORS)
def restore_snapshot(
    name: str,
    snap_id: str,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    bus: EventBus = Depends(get_event_bus),
) -> dict:
    _restore(bl, require_profile(name, pm), snap_id)
    logger.info("API restored %s to snapshot %s", name, snap_id)
    bus.emit()
    return {"id": snap_id}


# --- The whole bucket ---------------------------------------------------------


def _admin(request: Request) -> None:
    """In hub mode the bucket holds every user's profiles: admins only."""
    if hub_store(request) is not None and request.state.user["role"] != "admin":
        raise HTTPException(status_code=403, detail=get_string("err_admins_only"))


def _orphan(uid: str, pm: IProfileManager) -> None:
    if not UID_RE.fullmatch(uid):
        raise HTTPException(status_code=404, detail=get_string("err_snapshot_not_found"))
    if any(p.uid == uid for p in pm.list_profiles()):
        raise HTTPException(status_code=409, detail=get_string("err_snapshot_linked"))


@bucket_router.get("", responses=_ERRORS, dependencies=[Depends(_admin)])
def bucket_overview(pm: IProfileManager = Depends(get_profile_manager)) -> dict:
    """Every profile with snapshots; orphans (deleted profiles) carry the name they had."""
    names = {p.uid: p.name for p in pm.list_profiles()}
    try:
        groups = snapshots.overview()
        rows = [
            {
                "uid": uid,
                "name": names.get(uid) or snapshots.saved_profile(uid).get("name") or uid,
                "orphan": uid not in names,
                "count": g["count"],
                "size": g["size"],
                "newest": snapshots.created(g["newest"]),
            }
            for uid, g in groups.items()
        ]
    except Exception as e:
        raise _store_error(e) from e
    rows.sort(key=lambda r: (not r["orphan"], r["name"].casefold()))
    return {"profiles": rows}


@bucket_router.get("/{uid}", responses=_ERRORS, dependencies=[Depends(_admin)])
def uid_snapshots(uid: str) -> dict:
    """One profile's snapshots by uid: also a deleted one's."""
    if not UID_RE.fullmatch(uid):
        raise HTTPException(status_code=404, detail=get_string("err_snapshot_not_found"))
    try:
        return {"snapshots": snapshots.list_snapshots(uid)}
    except Exception as e:
        raise _store_error(e) from e


@bucket_router.delete("/{uid}", responses=_ERRORS, dependencies=[Depends(_admin)])
def delete_orphan(uid: str, pm: IProfileManager = Depends(get_profile_manager)) -> dict:
    _orphan(uid, pm)
    try:
        return {"deleted": snapshots.delete_all(uid)}
    except Exception as e:
        raise _store_error(e) from e


class OrphanRestore(BaseModel):
    # Empty: the name the profile had.
    name: str = Field("", max_length=64)
    # Empty: the newest snapshot.
    snap_id: str = Field("", max_length=32)


@bucket_router.post("/{uid}/restore", responses=_ERRORS, dependencies=[Depends(_admin)])
def restore_orphan(
    uid: str,
    body: OrphanRestore,
    request: Request,
    pm: IProfileManager = Depends(get_profile_manager),
    bl: IBrowserLauncher = Depends(get_browser_launcher),
    bus: EventBus = Depends(get_event_bus),
) -> dict:
    """Put a deleted profile back from one of its snapshots (the newest by default),
    with its settings and uid (so its snapshots are its own again)."""
    _orphan(uid, pm)
    try:
        saved = snapshots.saved_profile(uid)
        snap_id = body.snap_id or snapshots.list_snapshots(uid)[0]["id"]
    except snapshots.SnapshotNotFound as e:
        raise HTTPException(status_code=404, detail=get_string("err_snapshot_not_found")) from e
    except Exception as e:
        raise _store_error(e) from e
    name = body.name.strip() or saved.get("name") or ""
    valid, msg = validate_profile_name(name)
    if not valid:
        raise HTTPException(status_code=400, detail=msg)
    engine = saved.get("engine") if saved.get("engine") in ENGINES else "camoufox"
    if not pm.add_profile(
        name,
        saved.get("proxy") or "",
        saved.get("os_type") or "windows",
        valid_or_none(saved.get("timezone"), validate_timezone),
        valid_or_none(saved.get("locale"), validate_locale),
        engine,
        uid=uid,
    ):
        raise HTTPException(status_code=409, detail=get_string("err_profile_exists"))
    pm.set_label(name, saved.get("number"), saved.get("remark"))
    try:
        _restore(bl, pm.profiles[name], snap_id)
    except HTTPException:
        pm.delete_profile(name)  # no half-restored profile
        raise
    hub = hub_store(request)
    if hub is not None and request.state.user["name"] in hub.users:
        hub.set_owner(name, request.state.user["name"])
    logger.info("API put back deleted profile %s from snapshot %s", name, snap_id)
    bus.emit()
    return {"name": name}
