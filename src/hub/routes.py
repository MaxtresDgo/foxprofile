"""Hub API: operator login (through the identity platform), user and group admin, and the agent protocol.

Agent protocol (all as the operator the agent belongs to):
  GET  /hub/poll?running=a,b   long-poll for {"op": "launch", "profile": {...}} or
                               {"op": "stop", "name": ...}; 204 when idle
  GET  /hub/bundle/{name}      the profile as a ZIP (export_to_zip format)
  POST /hub/status/{name}      {"ok": bool, "error": str|null} once the launch settles
  PUT  /hub/bundle/{name}?release=1   upload the browser data back, ending the lease
  Both carry the ZIP's SHA-256 in X-Content-SHA256; a mismatch is a 422.
  POST /hub/release/{name}     end the lease without data
  GET  /hub/agent/code         this hub's code, for installed agents to update to

The poll also carries the agent's code id, its installer's requirements and PC
name; the answer can be {"op": "update", "code": id} (see hub/agent_code.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import pathlib
import shutil
import tempfile
import time
import urllib.parse
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from ..api.auth import hub_store
from ..api.helpers import require_profile
from ..core import config
from ..core.config import DATA_DIR
from ..core.logging import get_logger
from ..core.strings import get_string
from ..services.profile.transfer import (
    IntegrityError,
    export_to_zip,
    file_sha256,
    import_from_zip,
    read_cookies,
)
from . import agent_code, installers, platform_auth
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
    email: str
    password: str


@public_router.post("/login")
def login(body: LoginBody, request: Request) -> dict:
    """Checks the credentials with the identity platform, then opens a hub session;
    the platform's permissions decide the role on every sign-in."""
    try:
        user = platform_auth.authenticate(body.email, body.password)
    except platform_auth.LoginError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e
    admin = platform_auth.has_permission(user["permisos"], config.AUTH_ADMIN_PERMISSION)
    store = hub_store(request)
    token = store.sign_in(user["email"], "admin" if admin else "operator", user["name"])
    return {"token": token, **store.verify(token)}


@router.get("/me")
def me(request: Request) -> dict:
    """Who is logged in, whether their PC's agent is connected, and who has what open."""
    user, store, bl = _user(request), hub_store(request), _launcher(request)
    return {
        **user,
        "agent_online": bl.online(user["name"]),
        "agent_activity": _activity(bl, user["name"]),
        "agent_running": bl.agents.get(user["name"], {}).get("running", 0),
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


@router.delete("/users/{user}", dependencies=[Depends(_admin)])
def delete_user(user: str, request: Request) -> dict:
    """Drops the local account: owners, groups and keys. Access itself is revoked on
    the identity platform; a user it still lets in comes back as a fresh account."""
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


# What the agent is busy with, as it last said (POST /hub/agent/status). It says
# so again every few seconds while busy, so an older report means it stopped.
ACTIVITY_TTL = 30


class AgentStatus(BaseModel):
    state: Literal["ready", "downloading", "updating", "opening", "syncing"]
    pct: int | None = Field(None, ge=0, le=100)
    profile: str | None = Field(None, max_length=200)


def _activity(bl, user: str) -> dict | None:
    """The user's agent's current activity for the panel, or None when idle or silent."""
    seen = bl.agents.get(user, {}).get("activity")
    if not seen or seen["state"] == "ready" or time.monotonic() - seen["at"] > ACTIVITY_TTL:
        return None
    return {k: seen[k] for k in ("state", "pct", "profile")}


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


@router.post("/agent/status")
def agent_status(body: AgentStatus, request: Request) -> dict:
    """The caller's agent says what it is doing: downloading the browser (even
    before it polls), updating, opening or saving a profile, or ready."""
    user, bl = _user(request)["name"], _launcher(request)
    bl.agents[user] = {
        **bl.agents.get(user, {}),
        "activity": {**body.model_dump(), "at": time.monotonic()},
    }
    return {"ok": True}


@router.get("/poll")
async def poll(
    request: Request, running: str = "", code: str = "", req: str = "", pc: str = ""
) -> Response:
    user, bl = _user(request)["name"], _launcher(request)
    store = hub_store(request)
    names = {n for n in running.split(",") if n and store.can(_user(request), n)}
    bl.touch(user, names)
    bl.agents[user] = {**bl.agents.get(user, {}), "code": code, "req": req, "pc": pc[:64]}
    bl.agents[user]["running"] = len(names)
    if code and req:
        hub = await run_in_threadpool(agent_code.hub_code)
        offer = _agent_update(bl, user, hub[0], bool(names))
        if offer is not None:
            return JSONResponse(offer)
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


# An agent that got an update but still reports its old code (the download failed)
# is offered it again after this long.
UPDATE_RETRY = 600


def _agent_update(bl, user: str, hub_code: str, busy: bool) -> dict | None:
    """What an installed agent should do about this hub's code, if anything: update
    once its browsers are closed (or now, when an admin asked), or install the new
    installer when the libraries changed."""
    info = bl.agents[user]
    if info["code"] == hub_code:
        bl.update_now.discard(user)
        return None
    forced = user in bl.update_now
    if info["req"] != agent_code.hub_requirements():
        # The agent runs the new installer itself: once, and with its browsers closed.
        if info.get("told") == hub_code or (busy and not forced):
            return None
        bl.update_now.discard(user)
        info["told"] = hub_code
        return {"op": "installer_needed"}
    if not forced and (
        busy or time.monotonic() - info.get("offered", -UPDATE_RETRY) < UPDATE_RETRY
    ):
        return None
    bl.update_now.discard(user)
    info["offered"] = time.monotonic()
    logger.info("Updating %s's agent to %s", user, hub_code)
    return {"op": "update", "code": hub_code}


@router.get("/agent/code")
async def agent_code_zip() -> Response:
    code, data = await run_in_threadpool(agent_code.hub_code)
    return Response(data, media_type="application/zip", headers={"X-Code-Id": code})


def _agent_state(info: dict, hub_code: str, hub_req: str | None) -> str:
    if not info.get("code"):
        return "old"  # an agent from before updates: needs the new installer once
    if not info.get("req"):
        return "script"
    if info["code"] == hub_code:
        return "current"
    return "pending" if info["req"] == hub_req else "installer"


@router.get("/agents", dependencies=[Depends(_admin)])
async def list_agents(request: Request) -> dict:
    bl = _launcher(request)
    hub_code, _ = await run_in_threadpool(agent_code.hub_code)
    hub_req = agent_code.hub_requirements()
    return {
        "code": hub_code,
        "agents": [
            {
                "user": user,
                "pc": info.get("pc", ""),
                "code": info.get("code", ""),
                "online": bl.online(user),
                "running": info.get("running", 0),
                "state": _agent_state(info, hub_code, hub_req),
                "update_now": user in bl.update_now,
            }
            for user, info in sorted(bl.agents.items())
        ],
    }


@router.post("/agents/{user}/update", dependencies=[Depends(_admin)])
def update_agent_now(user: str, request: Request) -> dict:
    """Update that agent on its next poll even with profiles open (they close)."""
    bl = _launcher(request)
    if user not in bl.agents:
        raise HTTPException(status_code=404, detail=get_string("err_no_agent"))
    bl.update_now.add(user)
    bl.agents[user].pop("offered", None)
    return {"user": user}


# --- installers: served by the panel to set up new PCs (hub/installers.py) -------


@contextlib.contextmanager
def _installer_errors():
    try:
        yield
    except installers.InstallerError as e:
        code = 404 if e.key == "err_no_installer" else 400
        raise HTTPException(status_code=code, detail=get_string(e.key, **e.fields)) from e
    except Exception as e:
        if type(e).__module__.startswith(("botocore", "boto3")):  # the bucket answered badly
            logger.warning("Installer storage: %s", e)
            raise HTTPException(status_code=502, detail=get_string("err_storage", error=e)) from e
        raise


@router.get("/installers")
def list_installers() -> list[dict]:
    with _installer_errors():
        return installers.listing()


@public_router.get("/installers/{platform}")
def download_installer(platform: str) -> Response:
    """Public: the installers hold no secrets, and a new PC has no session yet.
    From a bucket, a redirect to a short-lived presigned URL."""
    with _installer_errors():
        installers.check(platform)
        if installers.in_bucket():
            url = installers.download_url(platform)
            if url is None:
                raise installers.InstallerError("err_no_installer")
            return RedirectResponse(url, status_code=307)
        path = installers.local_file(platform)
        if path is None:
            raise installers.InstallerError("err_no_installer")
    return FileResponse(path, filename=path.name, media_type=installers.CONTENT_TYPE)


@router.post("/installers/{platform}/upload-url", dependencies=[Depends(_admin)])
def installer_upload_url(platform: str, name: str = Query(...), size: int = Query(...)) -> dict:
    """Where the panel PUTs the file: the bucket, a presigned URL per part (no hub
    session), or this hub's own upload route."""
    with _installer_errors():
        installers.check(platform, name)
        if installers.in_bucket():
            return {"direct": True, **installers.start_upload(platform, name, size)}
    query = urllib.parse.urlencode({"name": name})
    return {"url": f"/api/v1/hub/installers/{platform}?{query}", "direct": False}


class UploadBody(BaseModel):
    name: str
    upload_id: str
    etags: list[str] = []


@router.post("/installers/{platform}/uploaded", dependencies=[Depends(_admin)])
def installer_uploaded(platform: str, body: UploadBody) -> dict:
    with _installer_errors():
        installers.check(platform, body.name)
        result = installers.finish_upload(platform, body.name, body.upload_id, body.etags)
    logger.info("Installer for %s uploaded to the bucket: %s", platform, body.name)
    return result


@router.post("/installers/{platform}/abort", dependencies=[Depends(_admin)])
def installer_abort(platform: str, body: UploadBody) -> dict:
    with _installer_errors():
        installers.check(platform, body.name)
        installers.abort_upload(platform, body.name, body.upload_id)
    return {"aborted": body.upload_id}


@router.put("/installers/{platform}", dependencies=[Depends(_admin)])
async def upload_installer(platform: str, request: Request, name: str = Query(...)) -> dict:
    """Upload into the hub's folder (no bucket set)."""
    with _installer_errors():
        installers.check(platform, name)
        if installers.in_bucket():
            raise installers.InstallerError("err_installer_bucket")
    folder = installers.folder(platform)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".upload-{os.getpid()}-{time.monotonic_ns()}"
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        for old in folder.iterdir():  # one installer per platform
            if old != tmp:
                old.unlink()
        tmp.rename(folder / name)
    finally:
        tmp.unlink(missing_ok=True)
    logger.info("Installer for %s uploaded: %s", platform, name)
    return {"platform": platform, "name": name, "size": (folder / name).stat().st_size}


SHA_HEADER = "X-Content-SHA256"


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
        headers={SHA_HEADER: file_sha256(result)},
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
        digest = hashlib.sha256()
        # ponytail: no size cap; operators are trusted LAN users. Cap here if that changes.
        with open(zip_path, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
                digest.update(chunk)
        expected = request.headers.get(SHA_HEADER)  # agents older than the header send none
        if expected and expected.lower() != digest.hexdigest():
            raise HTTPException(status_code=422, detail=get_string("err_bundle_corrupt"))
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
    try:
        read_cookies(str(new))  # never replace good data with damaged cookies
    except IntegrityError as e:
        logger.warning("Upload of %s refused: %s", name, e)
        raise HTTPException(status_code=422, detail=get_string("err_bundle_corrupt")) from e
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
