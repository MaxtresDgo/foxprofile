import re
import uuid
from dataclasses import asdict, dataclass, field

from chrome_engine.release import LEGACY_VERSION

# Browser engines a profile can run on. A profile keeps its engine for life:
# the two keep incompatible browser data in the same directory.
ENGINES = ("camoufox", "chrome")

UID_RE = re.compile(r"[0-9a-f]{32}")
CHROME_VERSION_RE = re.compile(r"\d+(\.\d+){3}")


def new_uid() -> str:
    return uuid.uuid4().hex


# Longest number and remark a profile keeps (the badge itself shows ~4 characters).
NUMBER_MAX = 20
REMARK_MAX = 60


def label_text(value: object, limit: int) -> str | None:
    """A number or remark as stored: one trimmed line, None when empty."""
    if value is None or isinstance(value, bool):
        return None
    text = " ".join(str(value).split())[:limit].strip()
    return text or None


@dataclass
class Profile:
    name: str
    proxy: str | None = None
    os_type: str = "windows"
    # None = follow the proxy's exit IP (Camoufox geoip); set to pin a value.
    timezone: str | None = None
    locale: str | None = None
    engine: str = "camoufox"
    # Tell open browsers apart, like AdsPower: the number on the toolbar badge, the
    # remark after the name in the first tab's title (services/browser/label.py).
    number: str | None = None
    remark: str | None = None
    # Fixed for life, unlike the name: what the profile's snapshots are filed under.
    uid: str = field(default_factory=new_uid)
    # chrome only: the fingerprint-chromium build it runs (chrome_engine/release.py).
    # It only ever moves to a newer one: Chromium cannot open data a newer build wrote.
    chrome_version: str | None = None

    def __post_init__(self) -> None:
        if self.engine != "chrome":
            self.chrome_version = None
        elif not (
            isinstance(self.chrome_version, str)
            and CHROME_VERSION_RE.fullmatch(self.chrome_version)
        ):
            self.chrome_version = LEGACY_VERSION

    def to_dict(self) -> dict:
        return asdict(self)
