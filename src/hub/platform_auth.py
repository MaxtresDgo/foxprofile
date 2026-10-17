"""Hub sign-in against the external identity platform.

POST {AUTH_URL}/auth/login {email, password} with `Origin: AUTH_ORIGIN`, the origin
the platform has registered for this app (it picks the permission the platform
requires). The answer is {access_token, user: {email, role: {permisos: [...]}}}.
Its token is not kept: a 2xx only proves the login, and the hub issues its own session.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request

from ..core import config
from ..core.logging import get_logger
from ..core.strings import get_string

logger = get_logger("hub.platform_auth")


class LoginError(Exception):
    """A failed sign-in; `status` is the HTTP status to answer, the message is safe to show."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def authenticate(email: str, password: str) -> dict:
    """{"email", "name", "permisos"} of the platform user, or LoginError."""
    req = urllib.request.Request(
        config.AUTH_URL + "/auth/login",
        data=json.dumps({"email": email.strip(), "password": password}).encode(),
        # A named User-Agent so a proxy in front of the platform (Cloudflare) can be told
        # to let the hub through; Python's default one is often blocked outright.
        headers={
            "Content-Type": "application/json",
            "Origin": config.AUTH_ORIGIN,
            "User-Agent": "FoxProfile-Hub",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            user = json.load(r)["user"]
        return {
            "email": str(user["email"]).strip().lower(),
            "name": str(user.get("name") or "").strip(),
            "permisos": list((user.get("role") or {}).get("permisos") or []),
        }
    except urllib.error.HTTPError as e:
        raise _refused(e) from e
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        logger.warning("Identity platform unreachable or answered badly: %s", e)
        raise LoginError(502, get_string("err_auth_unavailable")) from e


def _refused(e: urllib.error.HTTPError) -> LoginError:
    raw = e.read()
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        # Not the platform's JSON: a proxy in front of it (Cloudflare challenge or
        # "error code: 1010") or a wrong FOXPROFILE_AUTH_URL. Not the user's fault.
        logger.warning(
            "Identity platform login blocked before reaching it (%s): %s",
            e.code,
            raw[:200].decode(errors="replace"),
        )
        return LoginError(502, get_string("err_auth_unavailable"))
    # The platform's own message says why (unknown origin, lockout...); it never
    # includes the password, so it is safe in the server log.
    logger.info("Identity platform refused a login (%s): %s", e.code, body.get("message"))
    if e.code >= 500:
        return LoginError(502, get_string("err_auth_unavailable"))
    if e.code == 403:
        return LoginError(403, get_string("err_no_access"))
    wait = body.get("segundosRestantes")
    if isinstance(wait, (int, float)) and wait > 0:
        return LoginError(429, get_string("err_login_wait", minutes=math.ceil(wait / 60)))
    return LoginError(401, get_string("err_bad_login"))


def has_permission(permisos: list, required: str) -> bool:
    """The platform's rule: same number of `:` segments, each equal or `*`."""
    want = required.strip().split(":")
    for permiso in permisos:
        have = str(permiso).strip().split(":")
        if len(have) == len(want) and all(h in ("*", w) for h, w in zip(have, want, strict=True)):
            return True
    return False
