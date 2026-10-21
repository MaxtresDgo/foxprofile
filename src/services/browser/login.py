"""Fill a site's login form in a running profile from one of its saved accounts.

Each recipe is the login page, the URLs that are still part of signing in, its
fields in order (selector, account field, press Enter after it) and the 2FA code
field. Sites change their pages: the selectors live here and nowhere else.

A profile already signed in is left as it is: the site sends it away from its
login page, so nothing is typed. A captcha or any other check is left to the
operator; a missing field ends the run with an error naming it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ...api.schemas.page import NavigateRequest, TypeRequest, WaitRequest
from ...core.logging import get_logger
from ...core.strings import get_string
from ..profile.accounts import totp

logger = get_logger("browser.login")

FIELD_TIMEOUT = 20_000
# A code typed this close to its end may expire before the site checks it.
MIN_CODE_SECONDS = 5

# Every 2FA field also matches the standard one-time-code hint.
_OTC = 'input[autocomplete="one-time-code"]'
# Sites keep invisible, inert password inputs for password managers (X does):
# Playwright would fill those, so they are left out.
_PASSWORD = 'input[type="password"]:not([inert])'

# Checked against the live pages (Oct 2026). The first step of each was seen;
# later steps (X, Google, Microsoft passwords; every 2FA prompt) need a real
# account, so they keep the known names plus generic fallbacks.
RECIPES: dict[str, dict[str, Any]] = {
    "facebook": {
        "url": "https://www.facebook.com/login",
        "login_pages": ("/login",),
        # The ids are generated per load; the names are stable.
        "fields": [
            ('input[name="email"]', "username", False),
            ('input[name="pass"]', "password", True),
        ],
        "otp": f'input[name="approvals_code"], {_OTC}',
    },
    "instagram": {
        # Same login form as Facebook now; the old names stay as a fallback.
        "url": "https://www.instagram.com/accounts/login/",
        "login_pages": ("/accounts/login",),
        "fields": [
            ('input[name="email"], input[name="username"]', "username", False),
            ('input[name="pass"], input[name="password"]', "password", True),
        ],
        "otp": f'input[name="verificationCode"], {_OTC}',
    },
    "tiktok": {
        "url": "https://www.tiktok.com/login/phone-or-email/email",
        "login_pages": ("/login",),
        "fields": [('input[name="username"]', "username", False), (_PASSWORD, "password", True)],
        "otp": _OTC,
    },
    "x": {
        # Redirects to /i/jf/onboarding/web?mode=login: the username first, then
        # "Continue" shows the password.
        "url": "https://x.com/i/flow/login",
        "login_pages": ("/i/flow/login", "/i/jf/onboarding", "/login"),
        "fields": [
            (
                'input[name="username_or_email"], input[autocomplete~="username"], input[name="text"]',
                "username",
                True,
            ),
            (f'input[name="password"]:not([inert]), {_PASSWORD}', "password", True),
        ],
        "otp": f'input[data-testid="ocfEnterTextTextInput"], {_OTC}',
    },
    "gmail": {
        # The email field is type="text"; a hidden "hiddenPassword" input sits on
        # the first step and must not be filled.
        # Signed in, Google goes on to the account page; any page left on
        # accounts.google.com (sign-in, account chooser, a challenge) is not done.
        "url": "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fmyaccount.google.com%2F",
        "login_pages": ("accounts.google.com",),
        "fields": [
            ('#identifierId, input[name="identifier"], input[type="email"]', "username", True),
            ('input[name="Passwd"]', "password", True),
        ],
        "otp": f'input[name="totpPin"], #totpPin, {_OTC}',
    },
    "hotmail": {
        # New Microsoft sign-in: #usernameEntry (no name); loginfmt/passwd are the
        # previous page's names.
        "url": "https://login.live.com/",
        "login_pages": ("login.live.com", "login.microsoftonline.com"),
        "fields": [
            ('#usernameEntry, input[name="loginfmt"]', "username", True),
            (f'#passwordEntry, input[name="passwd"], {_PASSWORD}', "password", True),
        ],
        "otp": f'input[name="otc"], {_OTC}',
    },
}


def visible(selector: str) -> str:
    """Each alternative of `selector`, visible only: the page control takes the
    first match in document order, which may be a hidden copy of the field."""
    return ", ".join(part.strip() + ":visible" for part in selector.split(","))


def _type(bl: Any, profile: str, selector: str, text: str, submit: bool) -> None:
    params = TypeRequest(
        selector=visible(selector), text=text, submit=submit, timeout=FIELD_TIMEOUT
    )
    bl.control(profile, "type", params.model_dump())


def on_login_page(recipe: dict[str, Any], url: str) -> bool:
    return any(marker in url for marker in recipe["login_pages"])


def _current_url(bl: Any, profile: str) -> str:
    result = bl.control(profile, "evaluate", {"script": "location.href"})
    return str((result or {}).get("result") or "")


def open_login(bl: Any, profile: str, platform: str) -> bool:
    """Open the site's login page: True when the profile is already signed in
    (the site sent it elsewhere), False when the login form is there. Raises when
    neither: the page did not load, or it stays on a login page without the form."""
    recipe = RECIPES[platform]
    page = bl.control(profile, "navigate", NavigateRequest(url=recipe["url"]).model_dump())
    url = str((page or {}).get("url") or "")
    if url and not on_login_page(recipe, url):
        return True
    try:
        wait = WaitRequest(selector=visible(recipe["fields"][0][0]), timeout=FIELD_TIMEOUT)
        bl.control(profile, "wait_for", wait.model_dump())
        return False
    except Exception as e:
        # Some sites load their login page and only then send a signed-in
        # profile on (X goes to /home).
        url = _current_url(bl, profile)
        if url and not on_login_page(recipe, url):
            return True
        raise LookupError("username") from e


def login(
    bl: Any,
    profile: str,
    account: dict,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> dict:
    """{"ok", "error", "signed_in"}: whether the form was filled (not whether the
    site let it in); signed_in when the profile already was, and nothing was typed."""
    platform = account["platform"]
    recipe = RECIPES[platform]
    try:
        if open_login(bl, profile, platform):
            logger.info("Login on %s (%s): already signed in", profile, platform)
            return {"ok": True, "error": None, "signed_in": True}
    except LookupError:
        logger.info("Login on %s (%s): no username field", profile, platform)
        label = get_string("account_username")
        return {
            "ok": False,
            "error": get_string("err_login_field", field=label),
            "signed_in": False,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "signed_in": False}
    for selector, field, submit in recipe["fields"]:
        try:
            _type(bl, profile, selector, account.get(field) or "", submit)
        except Exception:
            logger.info("Login on %s (%s): no %s field", profile, account["platform"], field)
            label = get_string("account_username" if field == "username" else "account_password")
            return {
                "ok": False,
                "error": get_string("err_login_field", field=label),
                "signed_in": False,
            }
    secret = account.get("totp_secret")
    if secret:
        try:
            wait = WaitRequest(selector=visible(recipe["otp"]), timeout=FIELD_TIMEOUT)
            bl.control(profile, "wait_for", wait.model_dump())
        except Exception:
            # No code asked (a trusted device, or the site wants something else).
            logger.info("Login on %s (%s): no 2FA prompt", profile, account["platform"])
            return {"ok": True, "error": None, "signed_in": False}
        code, remaining = totp(secret, clock())
        if remaining < MIN_CODE_SECONDS:
            sleep(remaining)
            code, _ = totp(secret, clock())
        try:
            _type(bl, profile, recipe["otp"], code, True)
        except Exception:
            return {
                "ok": False,
                "error": get_string("err_login_field", field=get_string("account_otp")),
                "signed_in": False,
            }
    logger.info("Login form filled on %s (%s)", profile, platform)
    return {"ok": True, "error": None, "signed_in": False}
