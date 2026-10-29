"""Carry profiles over from AdsPower through its local API.

AdsPower must be running on this PC with its local API on (127.0.0.1:<port>,
50325 by default, plus an API key when its security setting asks for one). The
API allows one request per second, so every call here is spaced out.

What comes over: name, proxy, User-Agent (so the OS and, for phones, an android
persona with that exact User-Agent), cookies, and the profile's site account
(platform, username, password, 2FA key) when it is one of the sites FoxProfile
keeps accounts for. Profiles are created on the Chrome
engine: AdsPower's SunBrowser is Chromium, so sites keep seeing the same kind of
browser. Fingerprint noise (canvas, audio, client rects), local storage,
extensions and notes stay behind.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from urllib.parse import quote

from ...core.logging import get_logger
from ...core.strings import get_string
from ...utils.proxy_parser import mask_proxy
from ...utils.validation import _INVALID_CHARS, validate_profile_name, validate_proxy_format
from .accounts import AccountsLocked, valid_totp_secret

logger = get_logger("profile.adspower")

DEFAULT_PORT = 50325
# AdsPower answers at most one request per second, across all its endpoints.
MIN_INTERVAL = 1.1
ENGINE = "chrome"
_PAGE = 100

_LIST = "/api/v2/browser-profile/list"
_UA = "/api/v2/browser-profile/ua"
_COOKIES = "/api/v2/browser-profile/cookies"
_PROXIES = "/api/v2/proxy-list/list"

# AdsPower's "platform" is a site's domain; these are the ones FoxProfile keeps
# accounts for (a subdomain counts too: m.facebook.com, business.facebook.com).
_PLATFORM_DOMAINS = {
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "tiktok.com": "tiktok",
    "x.com": "x",
    "twitter.com": "x",
    "google.com": "gmail",
    "gmail.com": "gmail",
    "outlook.com": "hotmail",
    "hotmail.com": "hotmail",
    "live.com": "hotmail",
}

_lock = threading.Lock()
_last_call = 0.0
# Loopback only: never through a proxy from the environment.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class AdsPowerError(Exception):
    """A message that is safe to show to the user."""


class AdsPower:
    def __init__(self, port: int = DEFAULT_PORT, api_key: str = "") -> None:
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise AdsPowerError(get_string("adspower_bad_port"))
        # Only the port is configurable: the server never fetches another host.
        self.port = port
        self.api_key = (api_key or "").strip()

    def _call(self, method: str, path: str, body: Any = None, query: dict | None = None) -> Any:
        global _last_call
        url = f"http://127.0.0.1:{self.port}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with _lock:
            wait = _last_call + MIN_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                with _opener.open(request, timeout=30) as response:
                    payload = json.loads(response.read() or b"{}")
            except urllib.error.HTTPError as e:
                raise AdsPowerError(get_string("adspower_failed", error=f"HTTP {e.code}")) from e
            except (OSError, ValueError) as e:
                raise AdsPowerError(get_string("adspower_unreachable", port=self.port)) from e
            finally:
                _last_call = time.monotonic()
        if not isinstance(payload, dict) or payload.get("code") != 0:
            msg = payload.get("msg") if isinstance(payload, dict) else payload
            raise AdsPowerError(get_string("adspower_failed", error=msg or "?"))
        return payload.get("data") or {}

    def profiles(self) -> list[dict[str, Any]]:
        """Every AdsPower profile: {id, no, name, group, proxy, target}. proxy has no
        credentials; target is the FoxProfile name it gets, unique in the batch."""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._call("POST", _LIST, {"page": page, "limit": _PAGE}).get("list") or []
            items += batch
            if len(batch) < _PAGE:
                break
            page += 1
        taken: set[str] = set()
        out = []
        for item in items:
            summary = _summary(item)
            summary["target"] = foxprofile_name(summary["name"], summary["no"], taken)
            taken.add(summary["target"])
            out.append(summary)
        return out

    def fetch(self, profile_id: str) -> dict[str, Any]:
        """Everything a FoxProfile profile needs from one AdsPower profile."""
        found = self._call("POST", _LIST, {"profile_id": [profile_id], "page": 1, "limit": 1})
        items = found.get("list") or []
        if not items:
            raise AdsPowerError(get_string("adspower_not_found", id=profile_id))
        item = items[0]
        warnings: list[str] = []

        config = item.get("user_proxy_config") or {}
        saved = str(item.get("fbcc_proxy_acc_id") or "")
        if saved not in ("", "0") and not config.get("proxy_host"):
            # A proxy from AdsPower's own proxy list, referenced by id.
            listed = self._call("POST", _PROXIES, {"proxy_id": [saved], "page": 1, "limit": 1})
            entries = listed.get("list") or []
            config = _saved_proxy_config(entries[0]) if entries else {}
        proxy = proxy_from_config(config, warnings)

        user_agent = find_user_agent(self._call("POST", _UA, {"profile_id": [profile_id]}))
        user_agent = user_agent or find_user_agent(item.get("fingerprint_config") or {})
        if not user_agent:
            raise AdsPowerError(get_string("adspower_no_ua", name=item.get("name") or profile_id))
        os_type = os_from_ua(user_agent, warnings)

        raw = self._call("GET", _COOKIES, query={"profile_id": profile_id}).get("cookies")
        cookies = raw if isinstance(raw, str) else json.dumps(raw or [])

        return {
            "id": profile_id,
            "no": item.get("profile_no"),
            "name": item.get("name") or "",
            "remark": item.get("remark") or "",
            "proxy": proxy,
            "user_agent": user_agent,
            "os_type": os_type,
            "cookies": cookies,
            "account": account_from(item, warnings),
            "warnings": warnings,
        }


def _summary(item: dict[str, Any]) -> dict[str, Any]:
    config = item.get("user_proxy_config") or {}
    proxy = mask_proxy(proxy_from_config(config, []))
    saved = str(item.get("fbcc_proxy_acc_id") or "")
    if not proxy and saved not in ("", "0"):
        proxy = f"AdsPower #{saved}"
    return {
        "id": str(item.get("profile_id") or ""),
        "no": item.get("profile_no"),
        "name": item.get("name") or "",
        "group": item.get("group_name") or "",
        "proxy": proxy,
    }


def _saved_proxy_config(entry: dict[str, Any]) -> dict[str, Any]:
    """An entry of AdsPower's proxy list, in user_proxy_config terms."""
    return {
        "proxy_soft": "other",
        "proxy_type": entry.get("type"),
        "proxy_host": entry.get("host"),
        "proxy_port": entry.get("port"),
        "proxy_user": entry.get("user"),
        "proxy_password": entry.get("password"),
    }


def proxy_from_config(config: dict[str, Any], warnings: list[str]) -> str | None:
    """AdsPower's user_proxy_config as a FoxProfile proxy string, or None."""
    if not config or config.get("proxy_soft") in (None, "", "no_proxy"):
        return None
    host, port = str(config.get("proxy_host") or "").strip(), str(config.get("proxy_port") or "")
    if not host or not port.isdigit():
        return None
    kind = str(config.get("proxy_type") or "http").lower()
    scheme = {"http": "http", "https": "http", "socks5": "socks5", "socks4": "socks4"}.get(kind)
    if scheme is None:
        warnings.append(get_string("adspower_proxy_skipped", type=kind))
        return None
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    user, password = config.get("proxy_user") or "", config.get("proxy_password") or ""
    auth = f"{quote(str(user), safe='')}:{quote(str(password), safe='')}@" if user else ""
    return f"{scheme}://{auth}{host}:{port}"


def platform_from(value: Any) -> str | None:
    """FoxProfile's platform for AdsPower's platform domain, or None."""
    text = str(value or "").strip().lower()
    host = urllib.parse.urlsplit(text if "//" in text else "//" + text).hostname or ""
    for domain, platform in _PLATFORM_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return platform
    return None


def account_from(item: dict[str, Any], warnings: list[str]) -> dict[str, str] | None:
    """The profile's site account as FoxProfile keeps it, or None when there is
    none or its site is not one FoxProfile supports (with a warning)."""
    username = str(item.get("username") or "").strip()
    password = str(item.get("password") or "")
    if not username and not password:
        return None
    platform = platform_from(item.get("platform"))
    if platform is None:
        warnings.append(
            get_string("adspower_account_skipped", platform=item.get("platform") or "?")
        )
        return None
    if not username:
        warnings.append(get_string("adspower_account_no_user", platform=platform))
        return None
    secret = "".join(str(item.get("fakey") or "").split()).upper()
    if secret and not valid_totp_secret(secret):
        warnings.append(get_string("adspower_bad_fakey"))
        secret = ""
    return {"platform": platform, "username": username, "password": password, "totp_secret": secret}


def find_user_agent(data: Any) -> str | None:
    """The User-Agent in an AdsPower answer, wherever it sits (ua / user_agent)."""
    if isinstance(data, str):
        return data if data.startswith("Mozilla/") else None
    if isinstance(data, dict):
        for key in ("ua", "user_agent", "userAgent"):
            value = data.get(key)
            if isinstance(value, str) and value.startswith("Mozilla/"):
                return value
        data = list(data.values())
    if isinstance(data, list):
        for value in data:
            found = find_user_agent(value)
            if found:
                return found
    return None


def os_from_ua(user_agent: str, warnings: list[str]) -> str:
    if "Android" in user_agent:
        return "android"
    if "iPhone" in user_agent or "iPad" in user_agent:
        # No iOS persona: a phone with the iOS User-Agent would contradict itself.
        warnings.append(get_string("adspower_ios"))
        return "android"
    if "Windows" in user_agent:
        return "windows"
    if "Macintosh" in user_agent:
        return "macos"
    if "Linux" in user_agent or "X11" in user_agent:
        return "linux"
    return "windows"


def foxprofile_name(name: str, number: Any, taken: set[str]) -> str:
    """A valid FoxProfile name for an AdsPower profile, not in `taken`. A repeated
    AdsPower name gets its profile number, so the result is the same every time."""
    clean = "".join("-" if c in _INVALID_CHARS or ord(c) < 32 else c for c in name)
    clean = clean.strip().rstrip(".")[:50].strip()
    if not clean or not validate_profile_name(clean)[0]:
        clean = f"ads-{number}" if number not in (None, "") else "ads-profile"
    candidate, n = clean, 2
    if candidate in taken and number not in (None, ""):
        candidate = f"{clean}-{number}"
    while candidate in taken:
        candidate, n = f"{clean}-{n}", n + 1
    return candidate


def create(pm: Any, data: dict[str, Any], name: str, data_dir: str) -> dict[str, Any]:
    """Create profile `name` (a target from profiles()) for `data` (from fetch), with
    its persona. {"status": "exists"} when it is already there, so importing again
    only adds what is missing."""
    from chrome_engine import persona as personas

    warnings = list(data.get("warnings") or [])
    proxy = data.get("proxy") or ""
    if proxy and not validate_proxy_format(proxy)[0]:
        warnings.append(get_string("adspower_proxy_skipped", type="?"))
        proxy = ""
    if name in pm.profiles or not pm.add_profile(name, proxy, data["os_type"], None, None, ENGINE):
        return {"status": "exists", "name": name, "warnings": []}
    fill_label(pm, name, data)
    # Before the first launch (cookie import is one), so it is this phone from the start.
    user_agent = data["user_agent"] if data["os_type"] == "android" else None
    personas.save(
        os.path.join(data_dir, name), personas.generate(data["os_type"], user_agent=user_agent)
    )
    # Only on a new profile: importing again never touches one that exists (on a
    # hub it may belong to another operator).
    if data.get("account"):
        try:
            pm.accounts.add(name, data["account"])
        except AccountsLocked:
            warnings.append(get_string("err_accounts_locked"))
    logger.info("Created %s from AdsPower profile %s", name, data.get("id"))
    return {"status": "created", "name": name, "warnings": warnings}


def fill_label(pm: Any, name: str, data: dict[str, Any]) -> None:
    """AdsPower's number and remark (services/browser/label.py), only where the
    profile has none: one written in FoxProfile is never replaced."""
    profile = pm.profiles.get(name)
    if profile is None:
        return
    number = profile.number or data.get("no")
    remark = profile.remark or data.get("remark")
    if (number, remark) != (profile.number, profile.remark):
        pm.set_label(name, number, remark)


def has_cookies(data: dict[str, Any]) -> bool:
    return (data.get("cookies") or "").strip() not in ("", "[]")


def _cookies_here(pm: Any, bl: Any, name: str, data: dict[str, Any]) -> int:
    """Import `data`'s cookies into stopped profile `name` on this machine.
    Merged: AdsPower's replace those with the same name and domain."""
    from ..browser import cookies

    profile = pm.profiles[name]
    with bl.exclusive(name):
        return cookies.import_cookies(
            name, profile.os_type, data["cookies"], profile.engine, profile.chrome_version
        )


def import_here(pm: Any, bl: Any, data: dict[str, Any], name: str, data_dir: str) -> dict[str, Any]:
    """create() and import the cookies on this machine (desktop app, local API)."""
    from ..browser import cookies
    from ..browser.launcher import ProfileBusyError

    result = create(pm, data, name, data_dir)
    result["cookies"] = 0
    if result["status"] == "created" and has_cookies(data):
        try:
            result["cookies"] = _cookies_here(pm, bl, name, data)
        except (cookies.CookieError, ProfileBusyError) as e:
            # The profile stays; its cookies can be imported again by hand.
            result["warnings"].append(str(e))
    return result


def reimport_here(pm: Any, bl: Any, data: dict[str, Any], name: str) -> dict[str, Any]:
    """Refresh existing profile `name`'s cookies from AdsPower, on this machine.
    Only the cookies (and a missing number or remark): its proxy, accounts,
    persona and browser data stay."""
    from ..browser import cookies
    from ..browser.launcher import ProfileBusyError

    result: dict[str, Any] = {"status": "reimported", "name": name, "cookies": 0, "warnings": []}
    fill_label(pm, name, data)
    if not has_cookies(data):
        result["warnings"].append(get_string("adspower_no_cookies"))
        return result
    try:
        result["cookies"] = _cookies_here(pm, bl, name, data)
    except ProfileBusyError as e:
        raise AdsPowerError(get_string("err_stop_reimport", name=name)) from e
    except cookies.CookieError as e:
        raise AdsPowerError(str(e)) from e
    logger.info("Reimported the cookies of %s from AdsPower profile %s", name, data.get("id"))
    return result
