"""Hub users, sessions and profile owners, persisted in one JSON file.

Passwords are scrypt hashes. Session tokens are stateless and signed with the
server secret (FOXPROFILE_API_TOKEN); they embed a fragment of the user's
password hash, so changing a password logs that user out everywhere.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import pathlib
import re
import secrets
import threading
import time

from ..core.logging import get_logger
from ..services.profile.manager import _replace

logger = get_logger("hub.store")

ROLES = ("admin", "operator")
SESSION_TTL = 12 * 3600
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MIN_PASSWORD = 8
KEY_PREFIX = "fpk_"


class HubError(ValueError):
    """Invalid user input; the message is safe to show."""


def _hash(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex()


class HubStore:
    def __init__(self, path: str, secret: str, admin_password: str = "") -> None:
        if len(secret) < 24:
            raise SystemExit(
                "Hub mode signs sessions with FOXPROFILE_API_TOKEN: set it to 24+ random characters."
            )
        self._path = pathlib.Path(path)
        self._secret = secret.encode()
        self._lock = threading.RLock()
        self.users: dict[str, dict] = {}
        self.owners: dict[str, str] = {}
        if self._path.exists():
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self.users = data.get("users", {})
            self.owners = data.get("owners", {})
        if not self.users and admin_password:
            self.add_user("admin", admin_password, "admin")
            logger.info("Created the admin user")

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"users": self.users, "owners": self.owners}, indent=2),
            encoding="utf-8",
        )
        _replace(tmp, self._path)

    # --- users -------------------------------------------------------------

    def add_user(self, name: str, password: str, role: str = "operator") -> None:
        if not _NAME.match(name):
            raise HubError("User name: 1-32 letters, digits, '_' or '-'")
        if role not in ROLES:
            raise HubError(f"role must be one of: {', '.join(ROLES)}")
        with self._lock:
            if name in self.users:
                raise HubError("User already exists")
            self.users[name] = {"role": role}
            self._set_password(name, password)

    def _set_password(self, name: str, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise HubError(f"Password must be at least {MIN_PASSWORD} characters")
        salt = secrets.token_hex(16)
        self.users[name].update(salt=salt, hash=_hash(password, salt))
        self._save()

    def update_user(self, name: str, password: str | None, role: str | None) -> None:
        with self._lock:
            if name not in self.users:
                raise KeyError(name)
            if role is not None:
                if role not in ROLES:
                    raise HubError(f"role must be one of: {', '.join(ROLES)}")
                self.users[name]["role"] = role
            if password is not None:
                self._set_password(name, password)
            else:
                self._save()

    def delete_user(self, name: str) -> None:
        with self._lock:
            if self.users.pop(name, None) is None:
                raise KeyError(name)
            # Their profiles stay, visible to admins until reassigned.
            self.owners = {p: u for p, u in self.owners.items() if u != name}
            self._save()

    def list_users(self) -> list[dict]:
        with self._lock:
            return [{"name": n, "role": u["role"]} for n, u in sorted(self.users.items())]

    # --- sessions ------------------------------------------------------------

    def login(self, name: str, password: str) -> str | None:
        """A session token, or None for a wrong name or password."""
        user = self.users.get(name)
        # Hash even for unknown names so timing does not reveal which exist.
        # ponytail: no login throttling; scrypt cost is the only brake (LAN deployment).
        digest = _hash(password, user["salt"] if user else "00" * 16)
        if user is None or not hmac.compare_digest(digest, user["hash"]):
            return None
        return self._sign(name, int(time.time()) + SESSION_TTL)

    def _sig(self, name: str, exp: int) -> str:
        user_hash = self.users[name]["hash"][:16]
        msg = f"{name}.{exp}.{user_hash}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def _sign(self, name: str, exp: int) -> str:
        return f"{name}.{exp}.{self._sig(name, exp)}"

    def new_key(self, name: str) -> str:
        """A personal API key for MCP clients and scripts; replaces the previous one."""
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            self.users[name]["key"] = hashlib.sha256(key.encode()).hexdigest()
            self._save()
        return key

    def _verify_key(self, key: str) -> dict | None:
        digest = hashlib.sha256(key.encode()).hexdigest()
        with self._lock:
            for name, user in self.users.items():
                if user.get("key") and hmac.compare_digest(user["key"], digest):
                    return {"name": name, "role": user["role"]}
        return None

    def verify(self, token: str) -> dict | None:
        """{"name", "role"} for a valid personal key or unexpired session token."""
        if token.startswith(KEY_PREFIX):
            return self._verify_key(token)
        try:
            name, exp_s, sig = token.split(".")
            exp = int(exp_s)
        except ValueError:
            return None
        with self._lock:
            if name not in self.users or exp < time.time():
                return None
            if not hmac.compare_digest(sig, self._sig(name, exp)):
                return None
            return {"name": name, "role": self.users[name]["role"]}

    # --- profile owners ------------------------------------------------------

    def can(self, user: dict, profile: str) -> bool:
        return user["role"] == "admin" or self.owners.get(profile) == user["name"]

    def set_owner(self, profile: str, user: str | None) -> None:
        with self._lock:
            if user is not None and user not in self.users:
                raise HubError("Unknown user")
            if user is None:
                self.owners.pop(profile, None)
            else:
                self.owners[profile] = user
            self._save()

    def rename_profile(self, old: str, new: str) -> None:
        with self._lock:
            if old in self.owners:
                self.owners[new] = self.owners.pop(old)
                self._save()
