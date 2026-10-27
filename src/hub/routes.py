"""Hub API: operator login (through the identity platform), user and group admin, and the agent protocol.

Agent protocol (all as the operator the agent belongs to):
  GET  /hub/poll?running=a,b   long-poll for {"op": "launch", "profile": {...}} or
                               {"op": "stop", "name": ...}; 204 when idle
  GET  /hub/bundle/{name}      the profile as a ZIP (export_to_zip format)
  POST /hub/status/{name}      {"ok": bool, "error": str|null} once the launch settles
  PUT  /hub/bundle/{name}?release=1   upload the browser data back, ending the lease
  Both carry the ZIP's SHA-256 in X-Content-SHA256; a mismatch is a 422.
  PUT  /hub/bundle/{name}/part?offset=N   the ZIP in pieces (Cloudflare caps a request
       at 100 MB and 100 s), then PUT /hub/bundle/{name}?chunked=1 puts them together
  POST /hub/bundle/{name}/download     the same for a download: builds the ZIP in the
       background; GET …/download/{id} until ready, GET …/download/{id}/part?offset=N,
       then DELETE …/download/{id}
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
import secrets
import shutil
import tempfile
import threading
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
from ..services.profile import snapshots
from ..services.profile.transfer import (
    IntegrityError,
    WrongProfileError,
    export_to_zip,
    file_sha256,
    swap_in,
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
    if not admin and not platform_auth.has_permission(
        user["permisos"], config.AUTH_ACCESS_PERMISSION
    ):
        raise HTTPException(status_code=403, detail=get_string("err_no_access"))
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
        "agent_connecting": _connecting(bl, user["name"]),
        "leases": {n: u for n, u in bl.leases().items() if store.can(user, n)},
        "syncing": sorted(n for n in _syncing(bl) if store.can(user, n)),
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
    _launcher(request).authorized[user["name"]] = time.monotonic()
    return {"key": store.new_key(user["name"], "agent_key"), **user}


# How long the code of the panel's "Authorize agent" page stays good.
AGENT_CODE_TTL = 120


@router.post("/me/agent-code")
def new_agent_code(request: Request) -> dict:
    """A one-time code the panel sends to the caller's agent (a navigation to its
    loopback page that replaces the panel's, so that tab can close itself); the
    agent trades it for its key at /hub/agent-code. The key never sits in a URL."""
    user, store, bl = _user(request), hub_store(request), _launcher(request)
    if user["name"] not in store.users:
        raise HTTPException(status_code=400, detail=get_string("err_no_personal_key"))
    now = time.monotonic()
    code = secrets.token_urlsafe(32)
    bl.agent_codes = {c: v for c, v in bl.agent_codes.items() if v[1] > now}
    bl.agent_codes[code] = (user["name"], now + AGENT_CODE_TTL)
    bl.authorized[user["name"]] = now
    return {"code": code}


class AgentCode(BaseModel):
    code: str = Field(max_length=100)


@public_router.post("/agent-code")
def redeem_agent_code(body: AgentCode, request: Request) -> dict:
    """The agent's key for a code from /hub/me/agent-code: once, and only in time."""
    user, expires = _launcher(request).agent_codes.pop(body.code, ("", 0.0))
    if not user or expires < time.monotonic():
        raise HTTPException(status_code=400, detail=get_string("err_agent_code"))
    return {"key": hub_store(request).new_key(user, "agent_key")}


@router.post("/me/army-key")
def new_army_key(request: Request) -> dict:
    """A key for Army's panel, handed to it by the panel's "Authorize Army" page
    (posted to config.ARMY_CALLBACK only); replaces the previous one of its kind."""
    user, store = _user(request), hub_store(request)
    if not config.ARMY_CALLBACK:
        raise HTTPException(status_code=404, detail=get_string("err_no_army"))
    if user["name"] not in store.users:
        raise HTTPException(status_code=400, detail=get_string("err_no_personal_key"))
    return {"key": store.new_key(user["name"], "army_key"), **user}


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


class UserBody(BaseModel):
    role: str = "operator"
    display: str = ""


@router.put("/users/{user}", dependencies=[Depends(_admin)])
def ensure_user(user: str, body: UserBody, request: Request) -> dict:
    """Create the account of someone who has not signed in yet, so a profile can be
    assigned to them (Army's panel lists its own users); an existing one keeps its role."""
    store = hub_store(request)
    name = user.strip().lower()
    existing = store.users.get(name) or {}
    try:
        return store.ensure_user(
            name, existing.get("role", body.role), body.display or existing.get("display", "")
        )
    except HubError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


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
    syncing: list[str] = Field(default_factory=list, max_length=1000)


def _activity(bl, user: str) -> dict | None:
    """The user's agent's current activity for the panel, or None when idle or silent."""
    seen = bl.agents.get(user, {}).get("activity")
    if not seen or seen["state"] == "ready" or time.monotonic() - seen["at"] > ACTIVITY_TTL:
        return None
    return {k: seen[k] for k in ("state", "pct", "profile")}


# How long the panel says "connecting" after an agent is authorized; past that
# without a poll, it did not connect.
AGENT_CONNECT_WAIT = 60


def _connecting(bl, user: str) -> bool:
    authorized = bl.authorized.get(user, float("-inf"))
    return not bl.online(user) and time.monotonic() - authorized < AGENT_CONNECT_WAIT


def _syncing(bl) -> set[str]:
    """Profiles an agent is still uploading: closed, but still leased to it."""
    names: set[str] = set()
    for user, info in list(bl.agents.items()):
        seen = info.get("activity")
        if not seen or time.monotonic() - seen["at"] > ACTIVITY_TTL:
            continue
        # Agents from before the list name only the profile they report.
        mine = set(seen.get("syncing") or ())
        if seen["state"] == "syncing" and seen.get("profile"):
            mine.add(seen["profile"])
        names |= {n for n in mine if bl.holder(n) == user}
    return names


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
    request: Request,
    running: str = "",
    code: str = "",
    req: str = "",
    pc: str = "",
    ver: str = "",
) -> Response:
    user, bl = _user(request)["name"], _launcher(request)
    store = hub_store(request)
    names = {n for n in running.split(",") if n and store.can(_user(request), n)}
    bl.touch(user, names)
    bl.agents[user] = {
        **bl.agents.get(user, {}),
        "code": code,
        "req": req,
        "pc": pc[:64],
        "version": ver[:32],
    }
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
        # An admin can ask again: a newer installer may have been uploaded since.
        if not forced and (info.get("told") == hub_code or busy):
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
                "version": info.get("version", ""),
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
    bl, user = _launcher(request), _user(request)["name"]
    holder = bl.holder(name)
    if holder == user:
        return
    # A free profile only takes data from whoever held it last.
    if allow_free and holder is None and bl.last_holder(name) in (None, user):
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


def _part_file(name: str) -> pathlib.Path:
    """Where a chunked upload of the profile grows. One per profile: only the
    lease holder uploads it, and a new upload starts it over."""
    return pathlib.Path(DATA_DIR).resolve().parent / f".part-{name}.zip"


# Downloads in pieces, for the same proxies: the ZIP of a big profile can take
# longer than 100 s to build, so it is built in the background while the agent
# polls. Jobs live in memory: a hub restart drops them and that launch fails.
PART_BYTES = 8 * 1024 * 1024
_DOWNLOAD_TTL = 3600
_downloads: dict[str, dict] = {}
_downloads_lock = threading.Lock()


def _build_download(job: dict, profile: Any, data_dir: str) -> None:
    try:
        ok, result = export_to_zip(profile, data_dir, job["dir"], include_data=True)
        if not ok:
            raise OSError(result)
        job.update(path=result, size=os.path.getsize(result), sha256=file_sha256(result))
    except Exception as e:
        job["error"] = str(e)
    finally:
        job["done"] = True  # last: a poll reads the fields above once it sees it


def _drop_download(job_id: str) -> None:
    with _downloads_lock:
        job = _downloads.pop(job_id, None)
    if job:
        shutil.rmtree(job["dir"], ignore_errors=True)


def _download_job(request: Request, name: str, job_id: str) -> dict:
    _require_lease(request, name)
    job = _downloads.get(job_id)
    if job is None or job["name"] != name:
        raise HTTPException(status_code=404, detail=get_string("err_no_request"))
    return job


@router.post("/bundle/{name}/download")
def start_download(name: str, request: Request) -> dict:
    profile = require_profile(name, request.app.state.container.profile_manager)
    _require_lease(request, name)
    now = time.time()
    with _downloads_lock:  # jobs an agent never ended (crash, lost connection)
        stale = [i for i, job in _downloads.items() if now - job["created"] > _DOWNLOAD_TTL]
    for job_id in stale:
        _drop_download(job_id)
    job_id = secrets.token_hex(16)
    job = {"name": name, "created": now, "done": False}
    job["dir"] = tempfile.mkdtemp(prefix="foxprofile-bundle-")
    with _downloads_lock:
        _downloads[job_id] = job
    data_dir = os.path.join(DATA_DIR, name)
    threading.Thread(target=_build_download, args=(job, profile, data_dir), daemon=True).start()
    return {"id": job_id}


@router.get("/bundle/{name}/download/{job_id}")
def download_status(name: str, job_id: str, request: Request) -> dict:
    job = _download_job(request, name, job_id)
    if not job["done"]:
        return {"ready": False}
    if "error" in job:
        raise HTTPException(status_code=500, detail=job["error"])
    return {"ready": True, "size": job["size"], "sha256": job["sha256"]}


@router.get("/bundle/{name}/download/{job_id}/part")
def download_part(name: str, job_id: str, request: Request, offset: int = Query(ge=0)) -> Response:
    job = _download_job(request, name, job_id)
    if not job["done"] or "error" in job:
        raise HTTPException(status_code=409, detail=get_string("err_bundle_corrupt"))
    with open(job["path"], "rb") as f:
        f.seek(offset)
        return Response(f.read(PART_BYTES), media_type="application/octet-stream")


@router.delete("/bundle/{name}/download/{job_id}")
def end_download(name: str, job_id: str, request: Request) -> dict:
    _download_job(request, name, job_id)
    _drop_download(job_id)
    return {"id": job_id}


MAX_BUNDLE = config.HUB_MAX_BUNDLE_MB * 1024 * 1024


def _too_big() -> str:
    return get_string("err_bundle_too_big", mb=config.HUB_MAX_BUNDLE_MB)


@router.put("/bundle/{name}/part")
async def upload_bundle_part(name: str, request: Request, offset: int = Query(ge=0)) -> dict:
    require_profile(name, request.app.state.container.profile_manager)
    _require_lease(request, name, allow_free=True)
    path = _part_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    size = path.stat().st_size if path.exists() else 0
    if offset > size:  # a piece went missing: the agent starts over
        raise HTTPException(status_code=416, detail=get_string("err_bundle_corrupt"))
    with open(path, "r+b" if path.exists() else "wb") as f:
        f.truncate(offset)  # a retried piece replaces what its failed try left
        f.seek(offset)
        async for chunk in request.stream():
            if f.tell() + len(chunk) > MAX_BUNDLE:
                f.truncate(0)
                raise HTTPException(status_code=413, detail=_too_big())
            f.write(chunk)
        return {"size": f.tell()}


@router.put("/bundle/{name}")
async def upload_bundle(
    name: str, request: Request, release: bool = Query(False), chunked: bool = Query(False)
) -> dict:
    """Replace the profile's browser data with the agent's copy: the request body,
    or with chunked=1 the pieces sent to /part."""
    profile = require_profile(name, request.app.state.container.profile_manager)
    # A late upload is fine while nobody else holds the profile (lease dropped
    # because the agent went offline, or the hub restarted).
    _require_lease(request, name, allow_free=True)
    data_root = pathlib.Path(DATA_DIR).resolve()
    data_root.mkdir(parents=True, exist_ok=True)
    # Same filesystem as the data dir, so the final swap is a rename.
    tmp = tempfile.mkdtemp(prefix=".upload-", dir=data_root.parent)
    try:
        zip_path = os.path.join(tmp, "bundle.zip")
        if chunked:
            part = _part_file(name)
            if not part.exists():
                raise HTTPException(status_code=416, detail=get_string("err_bundle_corrupt"))
            part.rename(zip_path)
            actual = await run_in_threadpool(file_sha256, zip_path)
        else:
            digest = hashlib.sha256()
            with open(zip_path, "wb") as f:
                async for chunk in request.stream():
                    if f.tell() + len(chunk) > MAX_BUNDLE:
                        raise HTTPException(status_code=413, detail=_too_big())
                    f.write(chunk)
                    digest.update(chunk)
            actual = digest.hexdigest()
        expected = request.headers.get(SHA_HEADER)  # agents older than the header send none
        if expected and expected.lower() != actual:
            raise HTTPException(status_code=422, detail=get_string("err_bundle_corrupt"))
        await run_in_threadpool(_swap_in, zip_path, tmp, name, data_root)
        if snapshots.enabled():  # the checked upload is this version's snapshot
            snapshots.upload_later(profile, zip_path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if release:
        _launcher(request).release(name, _user(request)["name"])
    logger.info("Synced %s from %s", name, _user(request)["name"])
    return {"name": name, "released": release}


def _swap_in(zip_path: str, tmp: str, name: str, data_root: pathlib.Path) -> None:
    try:
        swap_in(zip_path, tmp, name, data_root)
    except IntegrityError as e:  # never replace good data with damaged cookies
        logger.warning("Upload of %s refused: %s", name, e)
        raise HTTPException(status_code=422, detail=get_string("err_bundle_corrupt")) from e
    except WrongProfileError as e:
        raise HTTPException(status_code=400, detail=get_string("err_archive_other")) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


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
