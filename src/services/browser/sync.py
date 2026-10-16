"""Window sync: what the user does in a leader profile is repeated in followers.

The leader's control server records steps (see recorder.py); a thread per
session long-polls them and replays them on the followers through
`run_steps`. Everything goes through `launcher.control()`, so it works the
same with local browsers and with hub agents.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from ...core.logging import get_logger
from .launcher import BrowserControlError
from .steps import run_steps

logger = get_logger("sync")

POLL_S = 20


class SyncError(ValueError):
    pass


class SyncSession:
    def __init__(self, bl: Any, leader: str, followers: list[str], on_end: Callable) -> None:
        self.leader = leader
        self.followers = followers
        self.replayed = 0
        self.errors: dict[str, str] = {}
        self.ended_reason: str | None = None
        self._bl = bl
        self._on_end = on_end
        self._stop = threading.Event()

    def as_dict(self) -> dict[str, Any]:
        return {
            "leader": self.leader,
            "followers": list(self.followers),
            "replayed": self.replayed,
            "errors": dict(self.errors),
        }

    def start(self) -> None:
        self._bl.control(self.leader, "record_start")
        threading.Thread(target=self._loop, daemon=True, name=f"sync-{self.leader}").start()

    def stop(self) -> None:
        self._stop.set()
        with suppress(BrowserControlError):
            self._bl.control(self.leader, "record_stop")  # also wakes the pending poll

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    batch = self._bl.control(
                        self.leader, "record_poll", {"timeout": POLL_S}, timeout=POLL_S + 10
                    )
                except BrowserControlError as e:
                    self.ended_reason = f"leader: {e}"
                    return
                if self._stop.is_set() or not batch.get("recording"):
                    return
                if not batch["steps"]:
                    continue
                self.followers = [f for f in self.followers if self._bl.is_running(f)]
                if not self.followers:
                    self.ended_reason = "no follower is running"
                    return
                results = run_steps(self._bl, self.followers, batch["steps"], stop_on_error=False)
                for name, result in results.items():
                    if result["error"]:
                        self.errors[name] = result["error"]
                self.replayed += len(batch["steps"])
        finally:
            if not self._stop.is_set():
                # Ended on its own: stop recording. Not after stop(), which already
                # did, so a new session on the same leader is not cut short.
                with suppress(BrowserControlError):
                    self._bl.control(self.leader, "record_stop")
            if self.ended_reason:
                logger.info("Sync from %s ended: %s", self.leader, self.ended_reason)
            self._on_end(self)


class SyncManager:
    """The running sync sessions, at most one per profile (leader or follower)."""

    def __init__(self, bl: Any, on_change: Callable[[], None] = lambda: None) -> None:
        self._bl = bl
        self._on_change = on_change
        self._sessions: dict[str, SyncSession] = {}
        self._lock = threading.Lock()

    def sessions(self) -> list[SyncSession]:
        with self._lock:
            return list(self._sessions.values())

    def start(self, leader: str, followers: list[str]) -> SyncSession:
        followers = [f for f in dict.fromkeys(followers) if f != leader]
        if not followers:
            raise SyncError("Pick at least one follower besides the leader")
        with self._lock:
            # A profile in two sessions could replay its own steps back (A→B, B→A).
            busy = {n for s in self._sessions.values() for n in (s.leader, *s.followers)}
            if clash := busy & {leader, *followers}:
                raise SyncError(f"Already syncing: {', '.join(sorted(clash))}")
            session = SyncSession(self._bl, leader, followers, self._ended)
            self._sessions[leader] = session
        try:
            session.start()
        except Exception:
            with self._lock:
                self._sessions.pop(leader, None)
            raise
        self._on_change()
        return session

    def stop(self, leader: str) -> bool:
        with self._lock:
            session = self._sessions.pop(leader, None)
        if session is None:
            return False
        session.stop()
        self._on_change()
        return True

    def _ended(self, session: SyncSession) -> None:
        with self._lock:
            if self._sessions.get(session.leader) is not session:
                return  # already removed by stop()
            del self._sessions[session.leader]
        self._on_change()
