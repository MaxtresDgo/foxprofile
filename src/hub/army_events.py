"""Tell Army's backends that account reports changed, so its panel refreshes over its
socket instead of waiting for its poll. Best-effort and off the request thread: a down
Army must never fail or slow a report. Carries no report data, only the event name;
Army refetches through each user's own key, which keeps the hub's visibility rules."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.request

from ..core import config
from ..core.logging import get_logger

logger = get_logger("hub.army_events")


def signature(event: str, at: int) -> str:
    """HMAC-SHA256 of "event.at" with ARMY_SECRET; Army checks it with FOX_HUB_SECRET."""
    return hmac.new(
        config.ARMY_SECRET.encode(), f"{event}.{at}".encode(), hashlib.sha256
    ).hexdigest()


def _post(url: str, body: bytes) -> None:
    try:
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(request, timeout=5).close()
    except Exception as e:  # noqa: BLE001 - any failure only costs Army its instant refresh
        logger.info("Army webhook %s failed: %s", url, e)


def notify(event: str) -> None:
    if not config.ARMY_SECRET or not config.ARMY_WEBHOOK_URLS:
        return
    at = int(time.time())
    body = json.dumps({"evento": event, "at": at, "firma": signature(event, at)}).encode()
    for url in config.ARMY_WEBHOOK_URLS:
        threading.Thread(target=_post, args=(url, body), daemon=True).start()
