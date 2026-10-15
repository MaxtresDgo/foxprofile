"""The hub's stand-in for BrowserLauncher: browsers run on the operators' PCs.

A launch takes a lease (profile -> user) and queues a command for that user's
agent. The agent long-polls for commands, downloads the profile, runs it, and
uploads the data when the browser closes, which ends the lease.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from typing import Any

from ..models.profile import Profile
from ..services.browser.launcher import BrowserControlError, ProfileBusyError, _LaunchResult

# An agent polls every POLL_WAIT seconds at most; silent for longer = offline.
POLL_WAIT = 25
AGENT_TIMEOUT = POLL_WAIT + 15


class AgentOfflineError(Exception):
    """The user's agent is not connected."""


class RemoteLauncher:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases: dict[str, str] = {}
        self._queues: dict[str, queue.Queue] = defaultdict(queue.Queue)
        self._seen: dict[str, float] = {}
        self._results: dict[str, _LaunchResult] = {}
        self._busy: set[str] = set()

    # --- hub side --------------------------------------------------------------

    def online(self, user: str) -> bool:
        return time.monotonic() - self._seen.get(user, float("-inf")) < AGENT_TIMEOUT

    def dispatch(self, user: str, profile: Profile) -> None:
        """Lease the profile to `user` and ask their agent to launch it."""
        with self._lock:
            if profile.name in self._leases or profile.name in self._busy:
                raise ProfileBusyError(profile.name)
            if not self.online(user):
                raise AgentOfflineError(user)
            self._leases[profile.name] = user
            self._results[profile.name] = _LaunchResult()
            self._queues[user].put({"op": "launch", "profile": profile.to_dict()})

    def leases(self) -> dict[str, str]:
        with self._lock:
            return dict(self._leases)

    def holder(self, name: str) -> str | None:
        with self._lock:
            return self._leases.get(name)

    def release(self, name: str, user: str) -> bool:
        """End `user`'s lease. False if someone else holds the profile."""
        with self._lock:
            if self._leases.get(name, user) != user:
                return False
            self._leases.pop(name, None)
            result = self._results.get(name)
        if result:
            result.settle(False, "Browser closed")
        return True

    # --- agent side ------------------------------------------------------------

    def touch(self, user: str, running: set[str]) -> None:
        """Mark the agent alive; re-lease what it runs (the hub may have restarted)."""
        with self._lock:
            self._seen[user] = time.monotonic()
            for name in running:
                self._leases.setdefault(name, user)

    def next_command(self, user: str) -> dict | None:
        with contextlib.suppress(queue.Empty):
            return self._queues[user].get_nowait()
        return None

    def report(self, user: str, name: str, ok: bool, error: str | None) -> None:
        with self._lock:
            if self._leases.get(name) != user:
                return
            result = self._results.get(name)
            if not ok:
                self._leases.pop(name, None)
        if result:
            result.settle(ok, error)

    # --- IBrowserLauncher, as the existing API routes use it -------------------

    def is_running(self, profile_name: str) -> bool:
        with self._lock:
            return profile_name in self._leases

    def running_profile_names(self) -> set[str]:
        with self._lock:
            return set(self._leases)

    def running_count(self) -> int:
        return len(self.running_profile_names())

    def stop_profile(self, profile_name: str, timeout: int = 2) -> bool:
        """Ask the holder's agent to stop; the lease ends once it uploads.

        With the agent offline the lease is dropped at once: its PC is gone and
        nothing newer than the last upload can arrive (a late upload is still
        accepted while nobody else holds the profile).
        """
        user = self.holder(profile_name)
        if user is None:
            return False
        if self.online(user):
            self._queues[user].put({"op": "stop", "name": profile_name})
        else:
            self.release(profile_name, user)
        return True

    @contextlib.contextmanager
    def exclusive(self, profile_name: str) -> Iterator[None]:
        with self._lock:
            if profile_name in self._leases or profile_name in self._busy:
                raise ProfileBusyError(profile_name)
            self._busy.add(profile_name)
        try:
            yield
        finally:
            with self._lock:
                self._busy.discard(profile_name)

    def wait_for_launch(self, profile_name: str, timeout: float) -> tuple[bool, str | None]:
        with self._lock:
            result = self._results.get(profile_name)
        if result is None:
            return False, "No launch in progress"
        if not result.settled.wait(timeout):
            return False, "timeout"
        return result.ok, result.error

    def control(
        self,
        profile_name: str,
        action: str,
        params: dict[str, Any] | None = None,
        timeout: float = 60,
    ) -> Any:
        raise BrowserControlError("Browsers run on the operators' PCs in hub mode", status=501)

    def shutdown_all(self) -> None:
        pass
