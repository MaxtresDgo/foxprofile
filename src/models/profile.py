import re
import uuid
from dataclasses import asdict, dataclass, field

# Browser engines a profile can run on. A profile keeps its engine for life:
# the two keep incompatible browser data in the same directory.
ENGINES = ("camoufox", "chrome")

UID_RE = re.compile(r"[0-9a-f]{32}")


def new_uid() -> str:
    return uuid.uuid4().hex


@dataclass
class Profile:
    name: str
    proxy: str | None = None
    os_type: str = "windows"
    # None = follow the proxy's exit IP (Camoufox geoip); set to pin a value.
    timezone: str | None = None
    locale: str | None = None
    engine: str = "camoufox"
    # Fixed for life, unlike the name: what the profile's snapshots are filed under.
    uid: str = field(default_factory=new_uid)

    def to_dict(self) -> dict:
        return asdict(self)
