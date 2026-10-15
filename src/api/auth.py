"""Bearer-token authentication for the REST API.

When FOXPROFILE_API_TOKEN is set every /api/v1 route except /health requires
`Authorization: Bearer <token>` (or `X-API-Key: <token>`). Without a token the
API only accepts connections from the local machine; see `check_bind_safety`.
"""

import ipaddress
import secrets
import socket

from fastapi import HTTPException, Request

from ..core.config import API_TOKEN


def _presented_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("X-API-Key", "").strip()


def hub_store(request: Request):
    """The HubStore when the app runs in hub mode, else None."""
    container = getattr(request.app.state, "container", None)
    return container.hub_store if getattr(container, "hub", False) else None


def can_see(request: Request, profile: str) -> bool:
    """Whether the hub user behind this request may see `profile` (always outside hub mode)."""
    store = hub_store(request)
    return store is None or store.can(request.state.user, profile)


def _require_hub_user(request: Request, store) -> None:
    presented = _presented_token(request)
    if API_TOKEN and secrets.compare_digest(presented, API_TOKEN):
        # The raw server token stays an admin key for scripts.
        user = {"name": "api", "role": "admin"}
    else:
        user = store.verify(presented)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid session",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.user = user
    name = request.path_params.get("name")
    if name is not None and not store.can(user, name):
        # Same answer as a missing profile: others' profile names stay private.
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")


def require_token(request: Request) -> None:
    """FastAPI dependency: reject the request unless it carries the API token.

    In hub mode it takes a user's session token instead and only lets the user
    reach the profiles they own (admins reach all)."""
    store = hub_store(request)
    if store is not None:
        _require_hub_user(request, store)
        return
    if not API_TOKEN:
        return
    if not secrets.compare_digest(_presented_token(request), API_TOKEN):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def is_loopback(host: str) -> bool:
    """True if every address `host` resolves to is a loopback address."""
    if host == "":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_loopback for i in infos)


def check_bind_safety(host: str, token: str = API_TOKEN) -> None:
    """Refuse to expose the API beyond this machine without a token."""
    if not is_loopback(host) and not token:
        raise SystemExit(
            f"Refusing to listen on {host} without FOXPROFILE_API_TOKEN: anyone who can "
            "reach this port could control every profile and read every cookie. "
            "Set FOXPROFILE_API_TOKEN (at least 24 random characters) or use 127.0.0.1.",
        )
    if token and len(token) < 24:
        raise SystemExit("FOXPROFILE_API_TOKEN must be at least 24 characters long.")
