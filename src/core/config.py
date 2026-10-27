import os

try:
    from dotenv import find_dotenv, load_dotenv

    # From the working directory, not from this file: the Windows installer keeps
    # the code in Program Files and each user's .env in %LOCALAPPDATA%\FoxProfile.
    load_dotenv(find_dotenv(usecwd=True))
except ImportError:
    pass


def _env(key: str, default: str) -> str:
    """Read the FOXPROFILE_<key> environment variable."""
    return os.getenv(f"FOXPROFILE_{key}", default)


PROFILES_FILE = _env("PROFILES_FILE", "profiles.json")
# Each profile's site accounts, encrypted (services/profile/accounts.py). The key is
# ACCOUNTS_KEY (a Fernet key) or, when unset, a .key file made next to ACCOUNTS_FILE.
ACCOUNTS_FILE = _env("ACCOUNTS_FILE", "accounts.json")
ACCOUNTS_KEY = _env("ACCOUNTS_KEY", "")
DATA_DIR = _env("DATA_DIR", "camoufox_data")
# Browser extensions (services/browser/extensions.py): the catalogue and where each
# applies, and a folder with their ZIPs (plus, where browsers run, the unpacked copies).
EXTENSIONS_FILE = _env("EXTENSIONS_FILE", "extensions.json")
EXTENSIONS_DIR = _env("EXTENSIONS_DIR", "extensions")
LOG_DIR = _env("LOG_DIR", "logs")
LOG_LEVEL = _env("LOG_LEVEL", "INFO")
PROXY_CHECK_TIMEOUT = int(_env("PROXY_TIMEOUT", "10"))
LANGUAGE = _env("LANG", "en")

API_HOST = _env("API_HOST", "127.0.0.1")
API_PORT = int(_env("API_PORT", "8000"))

# Seconds the API waits for a launched browser to report ready or failed.
LAUNCH_WAIT_TIMEOUT = int(_env("LAUNCH_TIMEOUT", "90"))

FINGERPRINT_FILE = "fingerprint.json"


def _headless(value: str) -> bool | str:
    """'true'/'false', or 'virtual' (Linux: hidden Xvfb display, more stealthy)."""
    value = value.strip().lower()
    if value == "virtual":
        return "virtual"
    return value in ("1", "true", "yes")


# Browsers open hidden when true; server mode turns this on by default.
HEADLESS = _headless(_env("HEADLESS", "false"))

# Reopen the tabs a profile had open when it was last stopped.
RESTORE_TABS = _env("RESTORE_TABS", "true").strip().lower() in ("1", "true", "yes")

# Shared secret for the REST API, the web panel and the MCP server. Required
# whenever the API listens on anything other than the loopback interface.
API_TOKEN = _env("API_TOKEN", "")

# Hub mode: this server coordinates; browsers run on each operator's PC through
# `python -m src.agent`. Users and profile owners live in HUB_FILE.
HUB = _env("HUB", "false").strip().lower() in ("1", "true", "yes")
HUB_FILE = _env("HUB_FILE", "hub.json")
# Hub logins are checked against an external identity platform (hub/platform_auth.py):
# POST {AUTH_URL}/auth/login with `Origin: AUTH_ORIGIN`, the origin that platform has
# registered for this app. Users holding AUTH_ADMIN_PERMISSION there are hub admins.
AUTH_URL = _env("AUTH_URL", "").rstrip("/")
AUTH_ORIGIN = _env("AUTH_ORIGIN", "").rstrip("/")
AUTH_ADMIN_PERMISSION = _env("AUTH_ADMIN_PERMISSION", "foxprofile:admin")
# Army's panel links each user's account (hub/routes.new_army_key): after they
# authorize it, the hub panel POSTs their key here, and only here. It is fixed in
# this config, never taken from the link, so a crafted link cannot send a key elsewhere.
ARMY_CALLBACK = _env("ARMY_CALLBACK", "").rstrip("/")
# Agent side: where the hub is and which operator this PC belongs to.
HUB_URL = _env("HUB_URL", "").rstrip("/")
# Agent installers in a private S3 bucket (RustFS, MinIO, AWS) instead of next to
# HUB_FILE (hub/installers.py, boto3 from requirements-hub.txt). S3_ENDPOINT is how
# the hub reaches the store; S3_PUBLIC_URL how browsers do, when that differs (a
# presigned URL only works for the host it was signed for).
S3_ENDPOINT = _env("S3_ENDPOINT", "").rstrip("/")
S3_PUBLIC_URL = _env("S3_PUBLIC_URL", "").rstrip("/") or S3_ENDPOINT
S3_BUCKET = _env("S3_BUCKET", "")
S3_ACCESS_KEY = _env("S3_ACCESS_KEY", "")
S3_SECRET_KEY = _env("S3_SECRET_KEY", "")
S3_REGION = _env("S3_REGION", "us-east-1")
# The bucket is shared with other apps: FoxProfile keeps to fox-profiles/<dev|prod>/.
S3_ENV = _env("S3_ENV", "prod").strip().lower()
if S3_ENV not in ("dev", "prod"):
    raise ValueError(f"FOXPROFILE_S3_ENV must be dev or prod, not {S3_ENV!r}")
S3_PREFIX = f"fox-profiles/{S3_ENV}/installers/"
# Profile snapshots (services/profile/snapshots.py): each synced or saved version of
# a profile as a ZIP in a bucket, such as a NAS running RustFS. Opt-in: the bucket
# must be named here (cookies go into it); endpoint, keys and region fall back to
# S3_*. SNAPSHOTS=0 turns them off too.
SNAPSHOTS_KEEP = int(_env("SNAPSHOTS", "5"))
SNAPSHOT_S3_ENDPOINT = _env("SNAPSHOT_S3_ENDPOINT", "").rstrip("/") or S3_ENDPOINT
SNAPSHOT_S3_BUCKET = _env("SNAPSHOT_S3_BUCKET", "")
SNAPSHOT_S3_ACCESS_KEY = _env("SNAPSHOT_S3_ACCESS_KEY", "") or S3_ACCESS_KEY
SNAPSHOT_S3_SECRET_KEY = _env("SNAPSHOT_S3_SECRET_KEY", "") or S3_SECRET_KEY
SNAPSHOT_S3_REGION = _env("SNAPSHOT_S3_REGION", "") or S3_REGION
SNAPSHOT_PREFIX = f"fox-profiles/{S3_ENV}/snapshots/"
