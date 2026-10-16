from __future__ import annotations

import contextlib
import pathlib
import secrets
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ..core.config import API_PORT, API_TOKEN, HEADLESS
from ..core.logging import get_logger
from ..core.strings import current_language, request_language
from . import mcp_http
from .auth import require_token
from .guard import LocalOnlyGuard
from .routes import (
    automation_router,
    browser_router,
    mcp_setup_router,
    page_router,
    profiles_router,
    proxy_router,
)
from .schemas.common import SuccessResponse

if TYPE_CHECKING:
    from ..core.container import Container

logger = get_logger("api")

API_PREFIX = "/api/v1"
VERSION = "2.3.1"
_PANEL = pathlib.Path(__file__).resolve().parents[1] / "web" / "index.html"
_FONTS = pathlib.Path(__file__).resolve().parents[1] / "assets" / "fonts"
# Served to the panel without a token: it needs them before login.
_FONT_FILES = {f.name: f for f in _FONTS.glob("*.ttf")}


def _hub_check(container: Container):
    """MCP callers in hub mode: a user's key or session, or the server token."""

    def check(bearer: str) -> bool:
        if API_TOKEN and secrets.compare_digest(bearer, API_TOKEN):
            return True
        return bool(bearer) and container.hub_store.verify(bearer) is not None

    return check


class RequestLanguage:
    """Error messages follow the caller's Accept-Language (the panel sends its own)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        header = dict(scope["headers"]).get(b"accept-language", b"").decode("latin-1")
        # ponytail: first tag only, no q-weights; the panel sends exactly one.
        code = header.split(",")[0].split(";")[0].split("-")[0].strip().lower()
        with request_language(code):
            await self.app(scope, receive, send)


def create_app(container: Container, self_url: str | None = None) -> FastAPI:
    """Build the FastAPI application.

    `self_url` is where this process's own API is reachable over loopback; the
    MCP endpoint's tools call back into it.
    """
    mcp_route, mcp_lifespan = mcp_http.build(
        self_url or f"http://127.0.0.1:{API_PORT}",
        API_TOKEN,
        _hub_check(container) if container.hub else None,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with mcp_lifespan:
            yield

    app = FastAPI(
        lifespan=lifespan,
        title="FoxProfile API",
        description=(
            "REST API for FoxProfile (Camoufox profile manager). When "
            "FOXPROFILE_API_TOKEN is set, send it as `Authorization: Bearer <token>`."
        ),
        version=VERSION,
    )
    app.state.container = container
    app.add_middleware(RequestLanguage)
    if not API_TOKEN:
        # Token-less means local-only: block CSRF and DNS rebinding from websites.
        app.add_middleware(LocalOnlyGuard)

    protected = [Depends(require_token)]
    app.include_router(profiles_router, prefix=API_PREFIX, dependencies=protected)
    app.include_router(browser_router, prefix=API_PREFIX, dependencies=protected)
    app.include_router(proxy_router, prefix=API_PREFIX, dependencies=protected)
    app.include_router(page_router, prefix=API_PREFIX, dependencies=protected)
    app.include_router(automation_router, prefix=API_PREFIX, dependencies=protected)
    app.include_router(mcp_setup_router, prefix=API_PREFIX, dependencies=protected)
    app.router.routes.append(mcp_route)
    if container.hub:
        # Browsers live on the operators' PCs; page actions relay through their agents.
        from ..hub import routes as hub_routes

        container.hub_store  # noqa: B018 - fail fast on a missing secret or bad hub file
        app.include_router(hub_routes.public_router, prefix=API_PREFIX)
        app.include_router(hub_routes.router, prefix=API_PREFIX, dependencies=protected)

    @app.get(f"{API_PREFIX}/health", response_model=SuccessResponse, tags=["health"])
    def health_check() -> SuccessResponse:
        return SuccessResponse(message="FoxProfile API is running")

    @app.get(f"{API_PREFIX}/info", tags=["health"])
    def info() -> dict:
        """Public, secret-free facts the web panel needs before logging in."""
        return {
            "name": "FoxProfile",
            "version": VERSION,
            "auth_required": bool(API_TOKEN),
            "headless": HEADLESS,
            "mcp_path": mcp_http.MCP_PATH,
            "mode": "hub" if container.hub else "local",
            "language": current_language(),
        }

    @app.get("/", include_in_schema=False)
    def panel() -> FileResponse:
        return FileResponse(_PANEL, headers={"Cache-Control": "no-store"})

    @app.get("/assets/fonts/{name}", include_in_schema=False)
    def font(name: str) -> FileResponse:
        path = _FONT_FILES.get(name)
        if path is None:
            raise HTTPException(status_code=404)
        return FileResponse(
            path, media_type="font/ttf", headers={"Cache-Control": "public, max-age=604800"}
        )

    logger.info("FastAPI application created")
    return app
