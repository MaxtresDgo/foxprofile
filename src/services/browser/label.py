"""Tell open browsers apart, like AdsPower: the profile's number on a toolbar badge,
and a first tab titled "name-remark" with the profile's details (its start page):
exit IP and country, number, group, accounts, proxy, remark, and the browser's
own user agent, languages and timezone.

The launcher hands what the runner cannot know (number, remark, hub group, the
accounts' platform and user name, never a password) to the runner in
FOXPROFILE_LABEL. The badge is a tiny extension written at each launch, with no
content scripts and no web-accessible resources, so pages cannot see it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import json
import os
import pathlib
import tempfile
import time

from ...core.strings import current_language, get_string, set_language
from ...models.profile import Profile
from ...utils.proxy_parser import parse_proxy
from .extensions import chrome_id

LABEL_ENV = "FOXPROFILE_LABEL"

# The badge extension's public key (the private one was thrown away): it fixes the
# extension's ID, so Chrome can be told to pin it next to the address bar.
_KEY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAw0eMn8zNWcsLTD2WWDR60wygN/cBo/AQjgbmHdMc"
    "MemPIro1TIEIBwlw6l94wGF1OK+8kiheg18hafnT1dfT33y6Os1/QNQEMM1IHrUzDOjxDUDM1c8a1YTG2CSa"
    "VWgxH2vxogVhEoeABytwNLl2PsrAlAg7EkpTxnn6a3iQ+bP6YVKeM1YYXepNotg9D/+mrc2dHSpxHSm70vbB"
    "xPfbXtOgtZPCefOXeQcELJHJ/sAPMWwAYC6ry2Qo4p5UvUvoNwq1QgioRCeW/uGEJ0dmzdyAAcoQSod5qWec"
    "CVeZuvnSyhV8JnhC8rBanNbaA7bsK0o/vxgWB6K42EaDXj0UmQIDAQAB"
)
CHROME_ID = chrome_id(base64.b64decode(_KEY))
# The panel's "live" violet (src/web/index.html --live).
_BADGE_COLOR = "#5B3FA8"


def details(group: str | None, accounts: list[dict]) -> dict:
    """What a launch adds for the start page: the hub group and each account's
    platform and user name (AccountStore.list(), which has no secrets)."""
    return {
        "group": group or "",
        "accounts": [{"platform": a["platform"], "username": a["username"]} for a in accounts],
    }


def env(profile: Profile, extra: dict | None = None) -> str:
    """What the launcher puts in LABEL_ENV."""
    return json.dumps(
        {
            "number": profile.number or "",
            "remark": profile.remark or "",
            "lang": current_language(),
            **(extra or {}),
        }
    )


def _proxy_kind(proxy: str) -> str:
    """ "SOCKS5 · host:port": the scheme and server, never the credentials."""
    parsed = parse_proxy(proxy) if proxy else None
    if not parsed:
        return ""
    scheme, _, server = parsed["server"].partition("://")
    return f"{scheme.upper()} · {server}"


def current(name: str, proxy: str = "", os_type: str = "") -> dict:
    """In a runner: everything the start page shows that Python knows."""
    try:
        data = json.loads(os.environ.get(LABEL_ENV) or "{}")
    except ValueError:
        data = {}
    if data.get("lang"):
        set_language(data["lang"])  # the launcher's, not this process's default
    return {
        "name": name,
        "number": data.get("number") or "",
        "remark": data.get("remark") or "",
        "group": data.get("group") or "",
        "accounts": data.get("accounts") or [],
        "proxy": _proxy_kind(proxy),
        "os": os_type,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def tab_title(tag: dict[str, str]) -> str:
    """AdsPower's start tab: "Profile 12-No chip"."""
    return f"{tag['name']}-{tag['remark']}" if tag["remark"] else tag["name"]


def full_title(tag: dict[str, str]) -> str:
    """The badge's tooltip. (Chrome's --window-name would put it in the taskbar, but
    it does nothing when Playwright starts the browser.)"""
    return f"{tag['number']} · {tab_title(tag)}" if tag["number"] else tab_title(tag)


def write_extension(tag: dict[str, str], engine: str) -> str | None:
    """The badge extension's folder for this launch, or None without a number."""
    if not tag["number"]:
        return None
    title = full_title(tag)
    if engine == "chrome":
        manifest = {
            "manifest_version": 3,
            "key": _KEY,
            "action": {"default_title": title},
            "background": {"service_worker": "badge.js"},
        }
    else:  # Camoufox (Firefox): its own button area, so it shows without pinning
        manifest = {
            "manifest_version": 2,
            "browser_specific_settings": {"gecko": {"id": "label@foxprofile"}},
            "browser_action": {"default_title": title, "default_area": "navbar"},
            "background": {"scripts": ["badge.js"]},
        }
    manifest.update(name=title[:75], version="1.0")
    # Badge state is not kept across restarts: set it whenever the script starts.
    script = (
        "const action = chrome.action || chrome.browserAction;\n"
        "const show = () => {\n"
        f"  action.setBadgeText({{ text: {json.dumps(tag['number'])} }});\n"
        f"  action.setBadgeBackgroundColor({{ color: {json.dumps(_BADGE_COLOR)} }});\n"
        "};\n"
        "chrome.runtime.onStartup.addListener(show);\n"
        "chrome.runtime.onInstalled.addListener(show);\n"
        "show();\n"
    )
    # ponytail: one folder per profile and engine in the temp dir, rewritten on each
    # launch and never removed; a few KB each.
    folder = pathlib.Path(tempfile.gettempdir(), "foxprofile-label", tag["name"], engine)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "badge.js").write_text(script, encoding="utf-8")
    return str(folder)


def pin_in_chrome(profile_dir: str) -> None:
    """Pin the badge next to the address bar: Chrome hides new extensions in its menu."""
    path = pathlib.Path(profile_dir, "Default", "Preferences")
    try:
        prefs = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(prefs, dict):
            prefs = {}
    except (OSError, ValueError):
        prefs = {}
    extensions = prefs.get("extensions")
    if not isinstance(extensions, dict):
        extensions = prefs["extensions"] = {}
    pinned = extensions.get("pinned_extensions")
    if not isinstance(pinned, list):
        pinned = []
    if CHROME_ID in pinned:
        return
    extensions["pinned_extensions"] = [CHROME_ID, *pinned]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("Preferences.foxprofile-tmp")
    tmp.write_text(json.dumps(prefs), encoding="utf-8")
    tmp.replace(path)


def place(ip: str) -> dict:
    """Where an exit IP is, from Camoufox's local GeoIP database (no request)."""
    from camoufox.geolocation import get_geolocation

    geo = get_geolocation(ip)
    return {"ip": ip, "country": geo.locale.region or "", "timezone": geo.timezone or ""}


_OS = {"windows": "Windows", "macos": "macOS", "linux": "Linux", "android": "Android"}
_PLATFORMS = {
    "facebook": "Facebook",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "x": "X",
    "gmail": "Gmail",
    "hotmail": "Outlook",
}
_CHECKS = (
    ("BrowserScan", "https://www.browserscan.net/"),
    ("BrowserLeaks", "https://browserleaks.com/"),
)

# FoxProfile's passport look (DESIGN.md): the cover for the IP, paper for the data.
_STYLE = """<style>
body { margin: 0; background: #F3F4EF; color: #1E2422;
  font: 14px/1.5 "Be Vietnam Pro", system-ui, -apple-system, "Segoe UI", sans-serif; }
.cover { background: #17332E; color: #E8EFEA; padding: 28px 20px 64px; text-align: center; }
.who { color: #A3BAB0; font-size: 13px; }
.ip { font: 700 34px/1.2 "JetBrains Mono", ui-monospace, Consolas, monospace;
  margin: 6px 0 2px; user-select: all; overflow-wrap: anywhere; }
.where { color: #A3BAB0; font-size: 15px; min-height: 1.5em; }
.sheet { background: #FBFBF8; border: 1px solid #D5DACF; border-radius: 14px;
  max-width: 640px; margin: -44px auto 32px; padding: 20px; }
@media (max-width: 680px) { .sheet { margin: -44px 12px 24px; } }
.checks { display: flex; flex-wrap: wrap; gap: 6px 14px; color: #66706A; font-size: 13px; }
.checks a { color: #C2510F; }
h2 { font-size: 14.5px; margin: 22px 0 8px; padding-left: 8px; border-left: 3px solid #5B3FA8; }
dl { display: grid; grid-template-columns: minmax(96px, 34%) 1fr; margin: 0; }
dt, dd { margin: 0; padding: 9px 12px; }
dt { color: #66706A; text-align: right; }
dd { overflow-wrap: anywhere; }
dl > :nth-child(4n+1), dl > :nth-child(4n+2) { background: #E9ECE4; }
dl > :nth-child(4n+1) { border-radius: 8px 0 0 8px; }
dl > :nth-child(4n+2) { border-radius: 0 8px 8px 0; }
.data { font: 12.5px/1.5 "JetBrains Mono", ui-monospace, Consolas, monospace; }
</style>"""

# Fills the page and what only the browser knows: the user agent, languages and
# timezone websites see, with a running clock.
_FILL = r"""({ title, body, lang }) => {
  document.title = title;
  document.documentElement.lang = lang;
  // Phone personas emulate a mobile screen: without this it lays out 980px wide, shrunk.
  const viewport = document.createElement("meta");
  viewport.name = "viewport";
  viewport.content = "width=device-width, initial-scale=1";
  document.head.append(viewport);
  document.body.innerHTML = body;
  const $ = (id) => document.getElementById(id);
  const ua = navigator.userAgent;
  const kernel = /(Chrome|Firefox)\/(\d+)/.exec(ua);
  $("kernel").textContent = kernel ? kernel[1] + " " + kernel[2] : "?";
  $("ua").textContent = ua;
  $("langs").textContent = navigator.languages.join(", ");
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const tick = () => { $("tz").textContent = tz + " · " + new Date().toLocaleTimeString(lang); };
  tick();
  setInterval(tick, 1000);
}"""

_PLACE = """({ ip, country, timezone, lang, unknown }) => {
  let name = country;
  try { name = new Intl.DisplayNames([lang], { type: "region" }).of(country); } catch {}
  document.getElementById("ip").textContent = ip || unknown;
  document.getElementById("where").textContent = [name, timezone].filter(Boolean).join(" · ");
  if (ip) {
    const link = document.getElementById("ip2location");
    link.href = "https://www.ip2location.com/demo/" + encodeURIComponent(ip);
    link.hidden = false;
  }
}"""


def _rows(rows: list[tuple[str, str, bool]]) -> str:
    """<dt>/<dd> pairs, empty values left out; True shows the value in the data font."""
    return "".join(
        f"<dt>{html.escape(k)}</dt><dd{' class=data' if data else ''}>{html.escape(v)}</dd>"
        for k, v, data in rows
        if v
    )


def page_body(tag: dict) -> str:
    """The start page's HTML (no script: _FILL runs what it needs)."""
    s = get_string
    who = " · ".join(filter(None, [tag["number"] and "#" + tag["number"], tag["name"]]))
    checks = "".join(
        f'<a href="{url}" target="_blank" rel="noopener">{name}</a>' for name, url in _CHECKS
    )
    account = [
        (s("start_name"), tag["name"], False),
        (s("start_number"), tag["number"], True),
        (s("start_started"), tag["started"], True),
        (s("start_group"), tag["group"], False),
        *(
            (_PLATFORMS.get(a["platform"], a["platform"]), a["username"], True)
            for a in tag["accounts"]
        ),
        (s("start_proxy"), tag["proxy"] or s("start_no_proxy"), bool(tag["proxy"])),
        (s("start_remark"), tag["remark"], False),
    ]
    fingerprint = [(s("start_os"), _OS.get(tag["os"], tag["os"]), False)]
    live = (("start_kernel", "kernel", ""), ("start_ua", "ua", " class=data"))
    live += (("start_languages", "langs", " class=data"), ("start_timezone", "tz", " class=data"))
    return (
        _STYLE
        + f'<header class="cover"><div class="who">{html.escape(who)}</div>'
        + f'<div class="ip" id="ip">{html.escape(s("start_locating"))}</div>'
        + '<div class="where" id="where"></div></header><main class="sheet">'
        + f'<div class="checks"><span>{html.escape(s("start_check"))}</span>{checks}'
        + '<a id="ip2location" target="_blank" rel="noopener" hidden>IP2Location</a></div>'
        + f"<h2>{html.escape(s('start_profile'))}</h2><dl>{_rows(account)}</dl>"
        + f"<h2>{html.escape(s('start_fingerprint'))}</h2><dl>{_rows(fingerprint)}"
        + "".join(f'<dt>{html.escape(s(k))}</dt><dd id="{i}"{c}></dd>' for k, i, c in live)
        + "</dl></main>"
    )


# Each start page's IP lookup, kept until done (a bare task may be collected).
_pending: set[asyncio.Task] = set()


async def open_tab(context: object, tag: dict, place_job: asyncio.Future | None = None) -> None:
    """Turn the browser's first (blank) tab into the start page. `place_job`
    resolves to place() of the exit IP, shown when it comes."""
    page = context.pages[0] if context.pages else await context.new_page()
    arg = {"title": tab_title(tag), "body": page_body(tag), "lang": current_language()}
    # Written into its about:blank: set_content waits for a load event that Chrome's
    # first tab never reports to Playwright.
    await asyncio.wait_for(page.evaluate(_FILL, arg), 5)
    if place_job is not None:
        task = asyncio.ensure_future(_show_place(page, place_job))
        _pending.add(task)
        task.add_done_callback(_pending.discard)


async def _show_place(page: object, place_job: asyncio.Future) -> None:
    try:
        found = await place_job
    except Exception:
        found = {}
    arg = {"ip": "", "country": "", "timezone": "", **found, "lang": current_language()}
    arg["unknown"] = get_string("start_ip_unknown")
    with contextlib.suppress(Exception):  # the tab may be closed by now
        await page.evaluate(_PLACE, arg)
