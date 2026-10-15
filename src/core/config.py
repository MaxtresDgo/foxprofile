import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _env(key: str, default: str) -> str:
    """Read the FOXPROFILE_<key> environment variable."""
    return os.getenv(f"FOXPROFILE_{key}", default)


PROFILES_FILE = _env("PROFILES_FILE", "profiles.json")
DATA_DIR = _env("DATA_DIR", "camoufox_data")
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
# Creates the `admin` user on first start when HUB_FILE has no users yet.
ADMIN_PASSWORD = _env("ADMIN_PASSWORD", "")
# Agent side: where the hub is and which operator this PC belongs to.
HUB_URL = _env("HUB_URL", "").rstrip("/")
AGENT_USER = _env("AGENT_USER", "")
