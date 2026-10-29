"""Launch a fingerprint-chromium profile as a Playwright BrowserContext.

    async with ChromeEngine("data/alice", proxy="socks5://u:p@host:1080") as context:
        page = context.pages[0]
        await page.goto("https://example.com")

What the engine adds on top of a bare `launch_persistent_context`:

- a persistent persona (seed, OS, brand, cores) per profile directory;
- proxies with credentials, through a loopback bridge (proxy_bridge.py);
- timezone / locale from the proxy's exit IP unless the persona pins them;
- Playwright's automation switches removed where a page can observe them;
- a refusal to present an OS the host cannot fake consistently;
- android personas: a phone (mobile emulation, touch, User-Agent, client hints).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import fetch
from . import persona as personas
from .geo import ExitGeo, lookup
from .proxy_bridge import ProxyBridge, Upstream
from .release import get_release, host_os, key

# Playwright defaults a page can observe, or that make the browser behave
# unlike a person's Chrome. Everything else Playwright passes is kept, except
# its --disable-features list (see playwright_disabled_features_arg).
IGNORED_DEFAULT_ARGS = (
    "--enable-automation",
    "--disable-popup-blocking",
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
    "--disable-default-apps",
    "--disable-back-forward-cache",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-ipc-flooding-protection",
    "--force-color-profile=srgb",
    "--hide-scrollbars",
    "--disable-sync",
    "--no-sandbox",
)
HEADLESS_SCREEN = (1920, 1080)

_FEATURES_BLOCK = re.compile(r"disabledFeatures = \[(.*?)\]\.filter\(Boolean\)", re.S)


@functools.lru_cache(maxsize=1)
def playwright_disabled_features_arg() -> str | None:
    """Playwright's own --disable-features switch, exactly as it passes it.

    It turns off third-party storage partitioning, HTTPS upgrades, paint
    holding and more, and ignore_default_args only drops exact matches, so the
    string is read from the installed Playwright driver. None when it cannot be
    reproduced; test_browser.py::test_automation_switches_are_gone notices.
    """
    try:
        from playwright._impl._driver import compute_driver_executable

        driver = compute_driver_executable()
        cli = Path(driver[-1] if isinstance(driver, tuple) else driver)
        lib = cli.parent / "lib"
        for path in (lib / "coreBundle.js", lib / "server" / "chromium" / "chromiumSwitches.js"):
            if not path.is_file():
                continue
            match = _FEATURES_BLOCK.search(path.read_text(encoding="utf-8", errors="replace"))
            if not match:
                continue
            body = re.sub(r"//[^\n]*", "", match.group(1))
            if "?" in body:  # an entry depends on the environment
                return None
            return "--disable-features=" + ",".join(re.findall(r'"([^"]+)"', body))
    except Exception:
        return None
    return None


# fingerprint-chromium ignores Chrome's --force-webrtc-ip-handling-policy and it
# does not hide LAN addresses behind mDNS names, so the policy is set through
# the profile's preferences on every launch:
# - behind a proxy, no UDP at all: no candidate can reveal the real IP;
# - without one, only the public address (the one sites already see) and never
#   a LAN address. Fully blocked WebRTC is itself unusual.
WEBRTC_POLICY_PROXY = "disable_non_proxied_udp"
WEBRTC_POLICY_DIRECT = "default_public_interface_only"


def set_webrtc_policy(profile_dir: Path, policy: str) -> None:
    """Write webrtc.ip_handling_policy into the profile's Default/Preferences."""
    path = profile_dir / "Default" / "Preferences"
    try:
        prefs = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(prefs, dict):
            prefs = {}
    except (OSError, ValueError):
        prefs = {}
    webrtc = prefs.get("webrtc")
    if not isinstance(webrtc, dict):
        webrtc = prefs["webrtc"] = {}
    if webrtc.get("ip_handling_policy") == policy:
        return
    webrtc["ip_handling_policy"] = policy
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("Preferences.foxprofile-tmp")
    tmp.write_text(json.dumps(prefs), encoding="utf-8")
    tmp.replace(path)


class EngineError(RuntimeError):
    pass


class EngineNotInstalled(EngineError):
    pass


def default_sandbox() -> bool:
    # Chromium refuses to run sandboxed as root on Linux.
    if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0:
        return False
    return True


def build_args(
    persona: personas.Persona,
    proxy_url: str | None = None,
    headless: bool | str = False,
    extra_args: tuple[str, ...] | list[str] = (),
) -> list[str]:
    """Command-line switches for a launch (Playwright adds its own before these)."""
    args = persona.to_args()
    if proxy_url:
        args.append(f"--proxy-server={proxy_url}")
    args += ["--no-first-run", "--no-default-browser-check", "--hide-crash-restore-bubble"]
    # A phone's window is the phone: a desktop-sized one would show in outerWidth.
    phone = (int(persona.screen[0]), int(persona.screen[1])) if persona.screen else None
    if headless is True:
        # Headless otherwise reports an 800x600 screen.
        w, h = phone or HEADLESS_SCREEN
        args += [f"--window-size={w},{h}", f"--screen-info={{0,0 {w}x{h}}}"]
    elif headless == "offscreen":
        size = f"{phone[0]},{phone[1]}" if phone else "1600,900"
        args += ["--window-position=-32000,-32000", f"--window-size={size}"]
    elif phone:
        # Room for the tab strip and toolbar around the emulated viewport.
        args.append(f"--window-size={phone[0] + 16},{phone[1] + 120}")
    else:
        args.append("--start-maximized")
    args += list(extra_args)
    return args


def _device_options(persona: personas.Persona) -> dict[str, Any]:
    """Playwright options for the persona's device: a phone, or the real window."""
    if persona.platform != "android":
        return {"no_viewport": True}
    assert persona.screen is not None
    w, h, dpr = persona.screen
    size = {"width": int(w), "height": int(h)}
    return {
        "user_agent": persona.user_agent,
        "viewport": size,
        "screen": size,
        "device_scale_factor": dpr,
        "is_mobile": True,
        "has_touch": True,
    }


class ChromeEngine:
    """One running fingerprint-chromium profile. Use as an async context manager."""

    def __init__(
        self,
        profile_dir: str | Path,
        *,
        platform: str | None = None,
        persona: personas.Persona | None = None,
        proxy: str | None = None,
        headless: bool | str = False,
        geoip: bool = True,
        timezone: str | None = None,
        locale: str | None = None,
        executable: str | Path | None = None,
        version: str | None = None,
        extra_args: tuple[str, ...] | list[str] = (),
        allow_unsupported: bool = False,
        sandbox: bool | None = None,
    ) -> None:
        if headless not in (True, False, "offscreen"):
            raise ValueError("headless must be True, False or 'offscreen'")
        self.profile_dir = Path(profile_dir)
        self._platform = platform
        self._persona = persona
        self._proxy = proxy
        self.headless = headless
        self.geoip = geoip
        self._timezone = timezone
        self._locale = locale
        self._executable = executable
        self._version = version
        self._extra_args = tuple(extra_args)
        self._allow_unsupported = allow_unsupported
        self.sandbox = default_sandbox() if sandbox is None else sandbox

        self.persona: personas.Persona | None = None
        self.geo: ExitGeo | None = None
        self.bridge: ProxyBridge | None = None
        self.args: list[str] = []
        self.warnings: list[str] = []
        self.crashes: list[str] = []
        self.context: Any = None
        self._playwright: Any = None
        # android: one CDP session per page holding its override (detaching drops it).
        self._phone_sessions: dict[Any, Any] = {}
        self._tasks: set[asyncio.Task] = set()

    # --- lifecycle --------------------------------------------------------

    async def __aenter__(self) -> Any:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def start(self) -> Any:
        if self.context is not None or self.bridge is not None:
            raise EngineError("The engine is already running; stop() it first")
        try:
            return await self._start()
        except BaseException:
            await self.stop()
            raise

    async def _start(self) -> Any:
        executable = self._resolve_executable()
        if not self._executable:
            self._refuse_older_build()
        persona = self._resolve_persona()

        upstream = Upstream.parse(self._proxy) if self._proxy else None
        if upstream:
            self.bridge = await ProxyBridge(upstream).start()

        if upstream and self.geoip and not (persona.timezone and persona.locale):
            self.geo = await self._lookup_geo()
            persona = persona.with_geo(self.geo.timezone, self.geo.locale)
        self.persona = persona

        self.args = build_args(
            persona,
            proxy_url=self.bridge.url if self.bridge else None,
            headless=self.headless,
            extra_args=self._extra_args,
        )

        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        set_webrtc_policy(
            self.profile_dir, WEBRTC_POLICY_PROXY if self.bridge else WEBRTC_POLICY_DIRECT
        )
        self.context = await self._playwright.chromium.launch_persistent_context(
            str(self.profile_dir),
            executable_path=str(executable),
            headless=self.headless is True,
            args=self.args,
            ignore_default_args=self._ignored_default_args(),
            chromium_sandbox=self.sandbox,
            accept_downloads=True,
            **_device_options(persona),
        )
        if self.geo and self.geo.latitude is not None and self.geo.longitude is not None:
            # Only the position is emulated; sites still have to ask for permission.
            await self.context.set_geolocation(
                {"latitude": self.geo.latitude, "longitude": self.geo.longitude, "accuracy": 50}
            )
        for page in self.context.pages:
            if persona.platform == "android":
                # Before the first navigation; _watch then sees it done.
                await self._phone(page)
            self._watch(page)
        self.context.on("page", self._watch)
        if persona.platform == "android" and self.headless is False and self.context.pages:
            await self._fit_window(self.context.pages[0])
        return self.context

    async def stop(self) -> None:
        if self.context is not None:
            with contextlib.suppress(Exception):
                await self.context.close()
            self.context = None
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
            self._playwright = None
        if self.bridge is not None:
            await self.bridge.stop()
            self.bridge = None

    # --- helpers ----------------------------------------------------------

    def _resolve_executable(self) -> Path:
        if self._executable:
            path = Path(self._executable)
            if not path.is_file():
                raise EngineNotInstalled(f"Browser executable not found: {path}")
            return path
        installed = fetch.installed_executable(self._version)
        if not installed:
            version = get_release(self._version).version
            raise EngineNotInstalled(
                f"fingerprint-chromium {version} is not installed; "
                f"run: python -m chrome_engine fetch --version {version}"
            )
        return installed

    def _refuse_older_build(self) -> None:
        """Chromium cannot read data a newer build wrote (it records the last one
        in "Last Version"): opening it with an older build loses cookies and storage."""
        version = get_release(self._version).version
        try:
            last = (self.profile_dir / "Last Version").read_text(encoding="utf-8").strip()
            newer = key(last) > key(version)
        except (OSError, ValueError):
            return
        if newer:
            raise EngineError(
                f"This profile was last opened by Chromium {last}; build {version} is older "
                "and cannot read its data"
            )

    def _resolve_persona(self) -> personas.Persona:
        if self._persona is not None:
            pinned = {
                k: v or None
                for k, v in (("timezone", self._timezone), ("locale", self._locale))
                if v is not None
            }
            persona = replace(self._persona, **pinned)
        else:
            persona = personas.load_or_create(
                self.profile_dir,
                self._platform or host_os(),
                version=self._version,
                timezone=self._timezone,
                locale=self._locale,
            )
        level, reason = personas.compatibility(host_os(), persona.platform)
        if level == personas.UNSUPPORTED and not self._allow_unsupported:
            raise EngineError(
                f"A {persona.platform} persona cannot be presented consistently on a "
                f"{host_os()} host: {reason}"
            )
        if level != personas.OK:
            self.warnings.append(f"{persona.platform} persona on {host_os()}: {reason}")
        return persona

    async def _lookup_geo(self) -> ExitGeo:
        assert self.bridge is not None
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, lookup, self.bridge.url)
        except Exception as e:
            # Launching anyway would show the host's timezone behind the proxy.
            raise EngineError(
                f"Could not place the proxy's exit IP ({type(e).__name__}: {e}); "
                "pin a timezone and locale or pass geoip=False"
            ) from e

    def _ignored_default_args(self) -> list[str]:
        ignored = list(IGNORED_DEFAULT_ARGS)
        features = playwright_disabled_features_arg()
        if features:
            ignored.append(features)
        else:
            self.warnings.append(
                "Playwright's --disable-features list could not be removed: third-party "
                "storage partitioning and HTTPS upgrades stay off"
            )
        return ignored

    def _watch(self, page: Any) -> None:
        page.on("crash", lambda p: self.crashes.append(p.url))
        if (
            self.persona is not None
            and self.persona.platform == "android"
            and (page not in self._phone_sessions)
        ):
            task = asyncio.ensure_future(self._phone(page))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _phone(self, page: Any) -> None:
        """navigator.platform and the full client hints of the phone. Playwright's
        own override (from user_agent / is_mobile) sets neither; workers keep the
        host's navigator.platform, which CDP cannot reach from here.

        The mouse also acts as a finger, as in DevTools' device mode: the page is a
        touch device, so mobile sites wait for touch gestures (a long press opens
        Facebook's reactions) that a mouse never sends. Playwright's own mouse
        then hangs; automation taps instead (control.PageController)."""
        assert self.persona is not None and self.persona.user_agent is not None
        try:
            session = await self.context.new_cdp_session(page)
            await session.send(
                "Emulation.setUserAgentOverride",
                {
                    "userAgent": self.persona.user_agent,
                    "platform": "Linux armv81",
                    "userAgentMetadata": personas.client_hints(self.persona),
                },
            )
            await session.send(
                "Emulation.setEmitTouchEventsForMouse",
                {"enabled": True, "configuration": "mobile"},
            )
        except Exception:  # the page closed first
            return
        self._phone_sessions[page] = session
        page.on("close", lambda p: self._phone_sessions.pop(p, None))

    async def _fit_window(self, page: Any) -> None:
        """Size a phone's window to its viewport and center it on the screen.
        Chromium keeps a tabbed window at least 500px wide and inside the screen's
        work area, so a narrow phone leaves a margin and a tall one is cut off.
        Builds without Browser.setContentsSize keep the launch size."""
        assert self.persona is not None and self.persona.screen is not None
        width, height = int(self.persona.screen[0]), int(self.persona.screen[1])
        try:
            session = await self.context.new_cdp_session(page)
            window = (await session.send("Browser.getWindowForTarget"))["windowId"]

            async def bounds(**change: Any) -> dict[str, Any]:
                if change:
                    await session.send(
                        "Browser.setWindowBounds", {"windowId": window, "bounds": change}
                    )
                    await asyncio.sleep(0.2)  # the window manager applies it later
                result = await session.send("Browser.getWindowBounds", {"windowId": window})
                return result["bounds"]

            # CDP has no screen query: a maximized window covers the work area.
            area = await bounds(windowState="maximized")
            await bounds(windowState="normal")
            with contextlib.suppress(Exception):
                await session.send(
                    "Browser.setContentsSize",
                    {"windowId": window, "width": width, "height": height},
                )
                await asyncio.sleep(0.2)
            size = await bounds()
            await bounds(
                left=area["left"] + max(0, area["width"] - size["width"]) // 2,
                top=area["top"] + max(0, area["height"] - size["height"]) // 2,
            )
            await session.detach()
        except Exception:  # the window closed first: nothing to fit
            return
