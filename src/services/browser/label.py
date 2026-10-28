"""Tell open browsers apart, like AdsPower: the profile's number on a toolbar badge
and "name-remark" as the first tab's title.

The launcher hands the number and remark to the runner in FOXPROFILE_LABEL. The
badge is a tiny extension written at each launch, with no content scripts and no
web-accessible resources, so pages cannot see it.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import pathlib
import tempfile

from ...models.profile import Profile
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


def env(profile: Profile) -> str:
    """What the launcher puts in LABEL_ENV."""
    return json.dumps({"number": profile.number or "", "remark": profile.remark or ""})


def current(name: str) -> dict[str, str]:
    """In a runner: the profile's name, number and remark."""
    try:
        data = json.loads(os.environ.get(LABEL_ENV) or "{}")
    except ValueError:
        data = {}
    return {"name": name, "number": data.get("number") or "", "remark": data.get("remark") or ""}


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


_BODY = """<div style="font:16px system-ui,sans-serif;margin:18vh auto;max-width:34em;padding:0 16px">
<p style="font-size:48px;font-weight:700;margin:0">{number}</p>
<h1 style="font-size:28px;margin:.2em 0">{name}</h1>
<p style="font-size:20px;margin:0">{remark}</p></div>"""
_FILL = "([title, body]) => { document.title = title; document.body.innerHTML = body; }"


async def open_tab(context: object, tag: dict[str, str]) -> None:
    """Turn the browser's first (blank) tab into the label tab."""
    page = context.pages[0] if context.pages else await context.new_page()
    body = _BODY.format(**{k: html.escape(v) for k, v in tag.items()})
    # Written into its about:blank: set_content waits for a load event that Chrome's
    # first tab never reports to Playwright.
    await asyncio.wait_for(page.evaluate(_FILL, [tab_title(tag), body]), 5)
