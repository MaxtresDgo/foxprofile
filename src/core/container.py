import logging
import threading
from collections.abc import Callable

from ..interfaces.protocols import IBrowserLauncher, IProfileManager, IProxyService
from .config import HUB, LOG_DIR, LOG_LEVEL
from .events import EventBus
from .logging import setup_logging


class Container:
    def __init__(self, hub: bool = HUB) -> None:
        # Hub mode: browsers run on the operators' PCs (see src/hub).
        self.hub = hub
        setup_logging(LOG_DIR, getattr(logging, LOG_LEVEL, logging.INFO))
        self._instances: dict = {}
        # The first API requests arrive on several worker threads at once; each
        # service must be built exactly once or they would hold separate state.
        self._lock = threading.RLock()

    def _get(self, key: str, build: Callable[[], object]):
        instance = self._instances.get(key)
        if instance is None:
            with self._lock:
                instance = self._instances.get(key)
                if instance is None:
                    instance = self._instances[key] = build()
        return instance

    @property
    def event_bus(self) -> EventBus:
        return self._get("eb", EventBus)

    @property
    def profile_manager(self) -> IProfileManager:
        from ..services.profile.manager import ProfileManager

        return self._get("pm", ProfileManager)

    @property
    def browser_launcher(self) -> IBrowserLauncher:
        return self._get("bl", self._build_launcher)

    def _build_launcher(self):
        store = self.extensions
        if self.hub:
            from ..hub.remote import RemoteLauncher

            remote = RemoteLauncher()
            # What the agent downloads and loads; groups only exist on the hub.
            remote.extensions = lambda p: store.for_profile(
                p.name, p.engine, self.hub_store.profile_groups.get(p.name)
            )
            remote.details = lambda p: self._details(p, self.hub_store.profile_groups.get(p.name))
            return remote
        from ..services.browser.launcher import BrowserLauncher

        launcher = BrowserLauncher()
        launcher.extensions = lambda p: store.dirs(store.for_profile(p.name, p.engine))
        launcher.details = lambda p: self._details(p, None)
        return launcher

    def _details(self, profile, group: str | None) -> dict:
        """The start page's group and accounts (no secrets); a locked store shows none."""
        from ..services.browser import label

        try:
            accounts = self.profile_manager.accounts.list(profile.name)
        except Exception:
            accounts = []
        return label.details(group, accounts)

    @property
    def extensions(self):
        """The extension catalogue (services/browser/extensions.py)."""
        return self.profile_manager.extensions

    @property
    def sync(self):
        from ..services.browser.sync import SyncManager

        return self._get("sync", lambda: SyncManager(self.browser_launcher, self.event_bus.emit))

    @property
    def hub_store(self):
        from ..hub.store import HubStore
        from . import config

        def build():
            if not (config.AUTH_URL and config.AUTH_ORIGIN):
                raise SystemExit(
                    "Hub mode signs users in through FOXPROFILE_AUTH_URL with "
                    "FOXPROFILE_AUTH_ORIGIN: set both."
                )
            return HubStore(config.HUB_FILE, config.API_TOKEN)

        return self._get("hub", build)

    @property
    def proxy_service(self) -> IProxyService:
        from ..services.proxy.service import ProxyService

        return self._get("ps", ProxyService)
