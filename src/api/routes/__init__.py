from .adspower import router as adspower_router
from .automation import router as automation_router
from .browser import router as browser_router
from .mcp_setup import router as mcp_setup_router
from .page import router as page_router
from .profiles import router as profiles_router
from .proxy import router as proxy_router

__all__ = [
    "adspower_router",
    "automation_router",
    "browser_router",
    "mcp_setup_router",
    "page_router",
    "profiles_router",
    "proxy_router",
]
