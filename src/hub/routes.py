"""Hub API: operator login, user and group admin, and the agent protocol.

Agent protocol (all as the operator the agent belongs to):
  GET  /hub/poll?running=a,b   long-poll for {"op": "launch", "profile": {...}} or
                               {"op": "stop", "name": ...}; 204 when idle
  GET  /hub/bundle/{name}      the profile as a ZIP (export_to_zip format)
  POST /hub/status/{name}      {"ok": bool, "error": str|null} once the launch settles
  PUT  /hub/bundle/{name}?release=1   upload the browser data back, ending the lease
  POST /hub/release/{name}     end the lease without data
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import tempfile
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from ..api.auth import hub_store
from ..api.helpers import require_profile
from ..core.config import DATA_DIR
from ..core.logging import get_logger
from ..core.strings import get_string
from ..services.profile.transfer import export_to_zip, import_from_zip
from .remote import POLL_WAIT
from .store import HubError

logger = get_logger("hub.routes")

public_router = APIRouter(prefix="/hub", tags=["hub"])
router = APIRouter(prefix="/hub", tags=["hub"])


def _user(request: Request) -> dict:
    return request.state.user


def _launcher(request: Request):
    return request.app.state.container.browser_launcher


def _admin(request: Request) -> None:
    if request.state.user["role"] != "admin":
        raise HTTPException(status_code=403, detail=get_string("err_admins_only"))


# --- login -------------------------------------------------------------------


class LoginBody(BaseModel):
    username: str
    password: str


@public_router.post("/login")
def login(body: LoginBody, request: Request) -> dict:
    store = hub_store(request)
    token = store.login(body.username, body.password)
    if token is None:
        raise HTTPException(status_code=401, detail=get_string("err_bad_login"))
    return {"token": token, **store.verify(token)}


@router.get("/me")
def me(request: Request) -> dict:
    """Who is logged in, whether their PC's agent is connected, and who has what open."""
    user, store, bl = _user(request), hub_store(request), _launcher(request)
    return {
        **user,
        "agent_online": bl.online(user["name"]),
        "leases": {n: u for n, u in bl.leases().items() if store.can(user, n)},
        "owners": {n: u for n, u in store.owners.items() if store.can(user, n)},
        "groups": {n: g for n, g in store.profile_groups.items() if store.can(user, n)},
        "my_groups": {g["name"]: g["my_role"] for g in store.list_groups(user)},
    }


@router.post("/me/key")
def new_key(request: Request) -> dict:
    """Create (or replace) the caller's personal API key, shown only this once."""
    user, store = _user(request), hub_store(request)
    if user["name"] not in store.users:
        raise HTTPException(status_code=400, detail=get_string("err_no_personal_key"))
    return {"key": store.new_key(user["name"])}


@router.post("/me/agent-key")
def new_agent_key(request: Request) -> dict:
    """A key for the caller's PC agent, handed to it by the panel's "Authorize agent"
    page; replaces the agent's previous key but not the personal one."""
    user, store = _user(request), hub_store(request)
    if user["name"] not in store.users:
        raise HTTPException(status_code=400, detail=get_string("err_no_personal_key"))
    return {"key": store.new_key(user["name"], "agent_key"), **user}


# --- user admin ----------------------------------------------------------------


class UserCreate(BaseModel):
    name: str
    password: str
    role: str = "operator"


class UserUpdate(BaseModel):
    password: str | None = None
    role: str | None = None


class OwnerBody(BaseModel):
    user: str | None


def _admin_or_lead(request: Request) -> None:
    """Group leads need the user list to pick members."""
    user, store = _user(request), hub_store(request)
    if user["role"] != "admin" and not any(store.is_lead(user, g) for g in store.groups):
        raise HTTPException(status_code=403, detail=get_string("err_admins_or_leads"))


@router.get("/users", dependencies=[Depends(_admin_or_lead)])
def list_users(request: Request) -> list[dict]:
    return hub_store(request).list_users()


@router.post("/users", status_code=201, dependencies=[Depends(_admin)])
def create_user(body: UserCreate, request: Request) -> dict:
    try:
        hub_store(request).add_user(body.name, body.password, body.role)
    except HubError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"name": body.name, "role": body.role}


@router.patch("/users/{user}", dependencies=[Depends(_admin)])
def update_user(user: str, body: UserUpdate, request: Request) -> dict:
    try:
        hub_store(request).update_user(user, body.password, body.role)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=get_string("err_unknown_user")) from e
    except HubError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"name": user}


@router.delete("/users/{user}", dependencies=[Depends(_admin)])
def delete_user(user: str, request: Request) -> dict:
    if user == _user(request)["name"]:
        raise HTTPException(status_code=400, detail=get_string("err_delete_self"))
    try:
        hub_store(request).delete_user(user)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=get_string("err_unknown_user")) from e
    return {"name": user}


@router.put("/owners/{name}", dependencies=[Depends(_admin)])
def set_owner(name: str, body: OwnerBody, request: Request) -> dict:
    require_profile(name, request.app.state.container.profile_manager)
    try:
        hub_store(request).set_owner(name, body.user)
    except HubError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"name": name, "user": body.user}


# --- groups --------------------------------------------------------------------


class GroupCreate(BaseModel):
    name: str


class MemberBody(BaseModel):
    role: str = "member"


class ProfileGroupBody(BaseModel):
    group: str | None


def _lead(group: str, request: Request) -> None:
    store = hub_store(request)
    if group not in store.groups:
        raise HTTPException(status_code=404, detail=get_string("err_unknown_group"))
    if not store.is_lead(_user(request), group):
        raise HTTPException(status_code=403, detail=get_string("err_group_leads_only"))


@router.get("/groups")
def list_groups(request: Request) -> list[dict]:
    return hub_store(request).list_groups(_user(request))


@router.post("/groups", status_code=201, dependencies=[Depends(_admin)])
def create_group(body: GroupCreate, request: Request) -> dict:
    try:
        hub_store(request).add_group(body.name)
    except HubError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"name": body.name}


@router.delete("/groups/{group}", dependencies=[Depends(_admin)])
def delete_group(group: str, request: Request) -> dict:
    try:
        hub_store(request).delete_group(group)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=get_string("err_unknown_group")) from e
    return {"name": group}


def _change_member(group: str, user: str, role: str | None, request: Request) -> dict:
    _lead(group, request)
    store, caller = hub_store(request), _user(request)
    if caller["role"] != "admin" and (role == "lead" or store.groups[group].get(user) == "lead"):
        raise HTTPException(status_code=403, detail=get_string("err_only_admins_leads"))
    try:
        store.set_member(group, user, role)
    except HubError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"group": group, "user": user, "role": role}


@router.put("/groups/{group}/members/{user}")
def set_member(group: str, user: str, request: Request, body: MemberBody = MemberBody()) -> dict:
    return _change_member(group, user, body.role, request)


@router.delete("/groups/{group}/members/{user}")
def remove_member(group: str, user: str, request: Request) -> dict:
    return _change_member(group, user, None, request)


@router.put("/profile-group/{name}")
def set_profile_group(name: str, body: ProfileGroupBody, request: Request) -> dict:
    """Move a profile into a group (or out of one). `{name}` already passed `can()`."""
    require_profile(name, request.app.state.container.profile_manager)
    store, user = hub_store(request), _user(request)
    current = store.profile_groups.get(name)
    for group in {current, body.group} - {None}:
        if group in store.groups and not store.is_lead(user, group):
            raise HTTPException(status_code=403, detail=get_string("err_not_your_group"))
    try:
        store.set_profile_group(name, body.group)
    except HubError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"name": name, "group": body.group}


# --- agent protocol ------------------------------------------------------------


@router.get("/poll")
async def poll(request: Request, running: str = "") -> Response:
    user, bl = _user(request)["name"], _launcher(request)
    store = hub_store(request)
    names = {n for n in running.split(",") if n and store.can(_user(request), n)}
    bl.touch(user, names)
    # ponytail: 0.1 s polling of an in-memory queue; an asyncio.Event per user if it ever costs.
    for _ in range(POLL_WAIT * 10):
        # A restarted or dropped agent leaves this poll running; a command taken here
        # would go to a closed socket and be lost. Ceiling: a proxy in front of the hub
        # may hide the disconnect; acking each command would close that gap.
        if await request.is_disconnected():
            return Response(status_code=204)
        command = bl.next_command(user)
        if command is not None:
            return JSONResponse(command)
        await asyncio.sleep(0.1)
        bl.touch(user, set())
    return Response(status_code=204)


def _require_lease(request: Request, name: str, allow_free: bool = False) -> None:
    holder = _launcher(request).holder(name)
    if holder == _user(request)["name"] or (allow_free and holder is None):
        return
    raise HTTPException(status_code=409, detail=get_string("err_not_leased"))


@router.get("/bundle/{name}")
def download_bundle(name: str, request: Request) -> FileResponse:
    profile = require_profile(name, request.app.state.container.profile_manager)
    _require_lease(request, name)
    tmp = tempfile.mkdtemp(prefix="foxprofile-bundle-")
    ok, result = export_to_zip(profile, os.path.join(DATA_DIR, name), tmp, include_data=True)
    if not ok:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(status_code=500, detail=result)
    return FileResponse(
        result,
        media_type="application/zip",
        background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True),
    )


@router.put("/bundle/{name}")
async def upload_bundle(name: str, request: Request, release: bool = Query(False)) -> dict:
    """Replace the profile's browser data with the agent's copy."""
    require_profile(name, request.app.state.container.profile_manager)
    # A late upload is fine while nobody else holds the profile (lease dropped
    # because the agent went offline, or the hub restarted).
    _require_lease(request, name, allow_free=True)
    data_root = pathlib.Path(DATA_DIR).resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    # Same filesystem as the data dir, so the final swap is a rename.
    tmp = tempfile.mkdtemp(prefix=".upload-", dir=data_root.parent)
    try:
        zip_path = os.path.join(tmp, "bundle.zip")
        # ponytail: no size cap; operators are trusted LAN users. Cap here if that changes.
        with open(zip_path, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        await run_in_threadpool(_swap_in, zip_path, tmp, name, data_root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if release:
        _launcher(request).release(name, _user(request)["name"])
    logger.info("Synced %s from %s", name, _user(request)["name"])
    return {"name": name, "released": release}


def _swap_in(zip_path: str, tmp: str, name: str, data_root: pathlib.Path) -> None:
    extract = os.path.join(tmp, "data")
    ok, result = import_from_zip(zip_path, extract)
    if not ok:
        raise HTTPException(status_code=400, detail=result)
    if result.name != name:
        raise HTTPException(status_code=400, detail=get_string("err_archive_other"))
    new = pathlib.Path(extract, name)
    new.mkdir(parents=True, exist_ok=True)
    target = data_root / name
    old = pathlib.Path(tmp, "old")
    if target.exists():
        target.rename(old)
    new.rename(target)


class AnswerBody(BaseModel):
    ok: bool
    result: Any = None
    error: str | None = None
    status: int = 400


@router.post("/result/{request_id}")
def answer(request_id: str, body: AnswerBody, request: Request) -> dict:
    """The agent's reply to a control or cookies command."""
    if not _launcher(request).answer(_user(request)["name"], request_id, body.model_dump()):
        raise HTTPException(status_code=404, detail=get_string("err_no_request"))
    return {"id": request_id}


class StatusBody(BaseModel):
    ok: bool
    error: str | None = None


@router.post("/status/{name}")
def report_status(name: str, body: StatusBody, request: Request) -> dict:
    _launcher(request).report(_user(request)["name"], name, body.ok, body.error)
    return {"name": name}


@router.post("/release/{name}")
def release(name: str, request: Request) -> dict:
    if not _launcher(request).release(name, _user(request)["name"]):
        raise HTTPException(status_code=409, detail=get_string("err_not_leased"))
    return {"name": name}
