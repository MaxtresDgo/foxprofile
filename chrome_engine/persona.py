"""A profile's persistent device identity for the Chrome engine.

fingerprint-chromium derives most of the device from one 32-bit seed (GPU,
canvas/audio noise, fonts, client rects); the persona adds what the seed does
not choose: OS, OS version, browser brand, CPU cores, timezone and language.
It is generated once and stored in the profile directory, so a profile shows
the same device on every launch, like Camoufox's fingerprint.json.

An android persona is a phone: fingerprint-chromium has no Android platform, so
it keeps the seed (desktop GPU strings, noise) and the engine adds the rest
itself: the User-Agent from the persona (a --user-agent switch, which also
reaches service and shared workers), mobile emulation (screen, touch) and
navigator.platform / client hints over CDP. Used to carry Android profiles over
from other anti-detect browsers with the User-Agent sites already know.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from random import Random
from typing import Any

PERSONA_FILE = "chrome_persona.json"
PLATFORMS = ("windows", "macos", "linux", "android")
BRANDS = ("Chrome", "Edge")
SEED_MAX = 2**31 - 1

# Values Chrome reports in navigator.userAgentData platformVersion, weighted
# roughly by usage. Windows: 10.0.0 = Windows 10, 15.0.0 = 11 23H2, 19.0.0 = 11 24H2.
_PLATFORM_VERSIONS: dict[str, list[tuple[str, float]]] = {
    "windows": [("10.0.0", 0.35), ("15.0.0", 0.25), ("19.0.0", 0.40)],
    "macos": [("14.7.6", 0.25), ("15.6.1", 0.45), ("26.0.1", 0.30)],
    "linux": [("6.8.0", 0.5), ("6.11.0", 0.3), ("6.14.0", 0.2)],
}
_CORES: dict[str, list[tuple[int, float]]] = {
    "windows": [(4, 0.15), (8, 0.35), (12, 0.20), (16, 0.25), (20, 0.05)],
    "macos": [(8, 0.45), (10, 0.25), (12, 0.20), (14, 0.10)],
    "linux": [(4, 0.20), (8, 0.35), (12, 0.20), (16, 0.25)],
}

# Phone screens (CSS width, height, devicePixelRatio) for android personas
# without one of their own.
_PHONE_SCREENS: list[tuple[tuple[int, int, float], float]] = [
    ((412, 915, 2.625), 0.4),
    ((393, 873, 2.75), 0.3),
    ((360, 800, 3.0), 0.3),
]
_ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/{major}.0.0.0 Mobile Safari/537.36"
)
_UA_CHROME = re.compile(r"Chrome/(\d+)\.(\d+)\.(\d+)\.(\d+)")
_UA_ANDROID = re.compile(r"Android (\d+(?:\.\d+){0,2})(?:; ([^;)]*))?")

_LOCALE = re.compile(r"^[a-z]{2,3}(-[A-Z]{2})?$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")

# How well a persona OS can be presented on a host OS. fingerprint-chromium
# spoofs navigator, client hints and the WebGL vendor/renderer everywhere, but
# fonts come from the host. A Linux persona keeps the host's real WebGL renderer:
# Direct3D 11 on Windows, the Apple GPU (e.g. "ANGLE Metal Renderer: Apple M1 Pro")
# on macOS. macOS overlay scrollbars are 0px wide, which a Windows persona cannot
# have. Measured with 148.0.7778.215 on Windows 11 and macOS 15 (Apple Silicon).
OK, WARN, UNSUPPORTED = "ok", "warn", "unsupported"
_COMPATIBILITY: dict[tuple[str, str], tuple[str, str]] = {
    ("windows", "macos"): (WARN, "the host's Windows fonts are visible to font probes"),
    ("windows", "linux"): (UNSUPPORTED, "WebGL still reports a Direct3D 11 renderer"),
    ("linux", "windows"): (WARN, "the host's Linux fonts are visible; install Windows fonts"),
    ("linux", "macos"): (WARN, "the host's Linux fonts are visible to font probes"),
    ("macos", "windows"): (
        WARN,
        "the host's macOS fonts are visible to font probes and overlay scrollbars are 0px wide",
    ),
    ("macos", "linux"): (UNSUPPORTED, "WebGL still reports the host's Apple GPU renderer"),
}
# Every host presents a phone the same way, with the same gaps.
_ANDROID_GAPS = (
    "the PC's desktop fonts are visible to font probes and navigator.platform "
    "inside workers is the PC's"
)


class PersonaError(ValueError):
    pass


@dataclass(frozen=True)
class Persona:
    seed: int
    platform: str
    platform_version: str
    brand: str = "Chrome"
    hardware_concurrency: int = 8
    # None: follow the proxy's exit IP (or the host when there is no proxy).
    timezone: str | None = None
    locale: str | None = None
    # android only: the exact User-Agent and the screen as [width, height, dpr].
    user_agent: str | None = None
    screen: list[float] | None = None

    def __post_init__(self) -> None:
        validate(self)

    def with_geo(self, timezone: str | None, locale: str | None) -> Persona:
        """Fill in timezone / locale only where the persona leaves them open."""
        return replace(self, timezone=self.timezone or timezone, locale=self.locale or locale)

    def to_args(self) -> list[str]:
        """fingerprint-chromium switches for this persona."""
        if self.platform == "android":
            # No --fingerprint-platform: it would pin navigator.platform and the
            # client hints to a desktop OS, beyond the engine's CDP override.
            assert self.user_agent is not None
            args = [
                f"--fingerprint={self.seed}",
                f"--fingerprint-brand={self.brand}",
                f"--fingerprint-brand-version={chrome_version(self.user_agent)}",
                f"--fingerprint-hardware-concurrency={self.hardware_concurrency}",
                f"--user-agent={self.user_agent}",
            ]
        else:
            args = [
                f"--fingerprint={self.seed}",
                f"--fingerprint-platform={self.platform}",
                f"--fingerprint-platform-version={self.platform_version}",
                f"--fingerprint-brand={self.brand}",
                f"--fingerprint-hardware-concurrency={self.hardware_concurrency}",
            ]
        if self.timezone:
            args.append(f"--timezone={self.timezone}")
        if self.locale:
            args.append(f"--lang={self.locale}")
            args.append(f"--accept-lang={accept_languages(self.locale)}")
        return args

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Persona:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def validate(p: Persona) -> None:
    if not isinstance(p.seed, int) or not 1 <= p.seed <= SEED_MAX:
        raise PersonaError(f"seed must be an integer in 1..{SEED_MAX}")
    if p.platform not in PLATFORMS:
        raise PersonaError(f"platform must be one of {', '.join(PLATFORMS)}")
    if not _VERSION.match(p.platform_version or ""):
        raise PersonaError("platform_version must look like 15.0.0")
    if p.brand not in BRANDS:
        raise PersonaError(f"brand must be one of {', '.join(BRANDS)}")
    if p.brand == "Edge" and p.platform in ("linux", "android"):
        raise PersonaError("Edge personas are Windows or macOS only")
    if p.platform == "android":
        if not p.user_agent or "Android" not in p.user_agent or not _UA_CHROME.search(p.user_agent):
            raise PersonaError("an android persona needs an Android Chrome User-Agent")
        if not (
            isinstance(p.screen, list)
            and len(p.screen) == 3
            and all(isinstance(v, (int, float)) and v > 0 for v in p.screen)
        ):
            raise PersonaError("an android persona needs a screen [width, height, dpr]")
    elif p.user_agent is not None or p.screen is not None:
        raise PersonaError("user_agent and screen are for android personas only")
    if not isinstance(p.hardware_concurrency, int) or not 1 <= p.hardware_concurrency <= 64:
        raise PersonaError("hardware_concurrency must be 1..64")
    if p.timezone is not None:
        _check_timezone(p.timezone)
    if p.locale is not None and not _LOCALE.match(p.locale):
        raise PersonaError("locale must look like en-US or vi-VN")


def _check_timezone(name: str) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise PersonaError(f"Unknown timezone {name!r}") from e


def accept_languages(locale: str) -> str:
    """The language list a browser set to `locale` typically sends: vi-VN,vi,en-US,en."""
    langs = [locale]
    base = locale.split("-")[0]
    if base != locale:
        langs.append(base)
    if base != "en":
        langs += ["en-US", "en"]
    return ",".join(langs)


def _pick(rng: Random, weighted: list[tuple[Any, float]]) -> Any:
    values, weights = zip(*weighted, strict=True)
    return rng.choices(values, weights=weights, k=1)[0]


def chrome_version(user_agent: str) -> str:
    """141.0.7390.122 from a Chrome User-Agent."""
    match = _UA_CHROME.search(user_agent)
    if not match:
        raise PersonaError("the User-Agent has no Chrome version")
    return ".".join(match.groups())


def client_hints(p: Persona) -> dict[str, Any]:
    """CDP userAgentMetadata for an android persona: what Chrome on that phone reports."""
    assert p.user_agent is not None
    full = chrome_version(p.user_agent)
    major = full.split(".")[0]
    android = _UA_ANDROID.search(p.user_agent)
    model = (android.group(2) or "").strip() if android else ""
    brands = [
        ("Google Chrome" if p.brand == "Chrome" else p.brand, major, full),
        ("Not?A_Brand", "8", "8.0.0.0"),
        ("Chromium", major, full),
    ]
    return {
        "brands": [{"brand": b, "version": v} for b, v, _ in brands],
        "fullVersionList": [{"brand": b, "version": f} for b, _, f in brands],
        "fullVersion": full,
        "platform": "Android",
        "platformVersion": p.platform_version,
        "architecture": "",
        "model": model,
        "mobile": True,
        "bitness": "",
        "wow64": False,
    }


def _android_version(user_agent: str) -> str:
    match = _UA_ANDROID.search(user_agent)
    parts = (match.group(1) if match else "10").split(".")
    return ".".join((parts + ["0", "0"])[:3])


def generate(
    platform: str,
    seed: int | None = None,
    brand: str = "Chrome",
    timezone: str | None = None,
    locale: str | None = None,
    user_agent: str | None = None,
    screen: list[float] | None = None,
    version: str | None = None,
) -> Persona:
    """A new persona. The same seed and platform always give the same persona.

    android: `user_agent` keeps the phone a profile had elsewhere; without one
    it is a reduced Chrome-on-Android User-Agent of the build `version` runs
    (the default build if unset)."""
    if platform not in PLATFORMS:
        raise PersonaError(f"platform must be one of {', '.join(PLATFORMS)}")
    seed = seed if seed is not None else 1 + secrets.randbelow(SEED_MAX)
    rng = Random(f"{seed}:{platform}")
    if platform == "android":
        from .release import DEFAULT_VERSION

        major = (version or DEFAULT_VERSION).split(".")[0]
        user_agent = user_agent or _ANDROID_UA.format(major=major)
        return Persona(
            seed=seed,
            platform=platform,
            platform_version=_android_version(user_agent),
            brand=brand,
            hardware_concurrency=8,
            timezone=timezone,
            locale=locale,
            user_agent=user_agent,
            screen=list(screen) if screen else list(_pick(rng, _PHONE_SCREENS)),
        )
    return Persona(
        seed=seed,
        platform=platform,
        platform_version=_pick(rng, _PLATFORM_VERSIONS[platform]),
        brand=brand,
        hardware_concurrency=_pick(rng, _CORES[platform]),
        timezone=timezone,
        locale=locale,
    )


def persona_path(profile_dir: str | Path) -> Path:
    return Path(profile_dir) / PERSONA_FILE


def load(profile_dir: str | Path) -> Persona | None:
    try:
        return Persona.from_json(json.loads(persona_path(profile_dir).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def save(profile_dir: str | Path, persona: Persona) -> None:
    path = persona_path(profile_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(persona.to_json(), indent=2), encoding="utf-8")
    tmp.replace(path)


def load_or_create(
    profile_dir: str | Path, platform: str, version: str | None = None, **overrides: Any
) -> Persona:
    """The profile's saved persona, generated on first use or when the OS changes.

    `version`: the build it runs, for a new android persona's User-Agent.
    `overrides` (timezone, locale, brand, ...) are applied to the saved persona
    without regenerating it, so pinning a timezone keeps the same device. None
    leaves a field as saved; "" clears it (timezone / locale follow the IP again).
    """
    persona = load(profile_dir)
    if persona is None or persona.platform != platform:
        persona = generate(platform, version=version)
    overrides = {k: (v or None) for k, v in overrides.items() if v is not None}
    if overrides:
        persona = replace(persona, **overrides)
    save(profile_dir, persona)
    return persona


def retarget(profile_dir: str | Path, version: str) -> bool:
    """After a profile moves to the build `version`: an android persona's
    User-Agent says that Chrome (a reduced one keeps its zeros). Desktop personas
    have no User-Agent of their own (the browser reports its version). True if
    the saved persona changed."""
    persona = load(profile_dir)
    if persona is None or not persona.user_agent:
        return False
    match = _UA_CHROME.search(persona.user_agent)
    if not match:
        return False
    reduced = match.groups()[1:] == ("0", "0", "0")
    new = f"{version.split('.')[0]}.0.0.0" if reduced else version
    user_agent = _UA_CHROME.sub(f"Chrome/{new}", persona.user_agent, count=1)
    if user_agent == persona.user_agent:
        return False
    save(profile_dir, replace(persona, user_agent=user_agent))
    return True


def compatibility(host: str, platform: str) -> tuple[str, str]:
    """(OK | WARN | UNSUPPORTED, reason) for presenting `platform` on a `host` OS."""
    if platform == "android":
        return WARN, _ANDROID_GAPS
    if host == platform:
        return OK, ""
    return _COMPATIBILITY.get((host, platform), (WARN, "untested combination"))
