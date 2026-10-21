"""Each profile's site accounts (username, password, 2FA secret), kept apart from
profiles.json so they never travel in ZIP exports, profile listings or the hub's
launch commands. The whole file is encrypted with Fernet: the key comes from
FOXPROFILE_ACCOUNTS_KEY, or from a key file next to ACCOUNTS_FILE made on the
first write.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import os
import pathlib
import secrets
import struct
import threading
import time

from cryptography.fernet import Fernet, InvalidToken

from ...core.config import ACCOUNTS_FILE, ACCOUNTS_KEY
from ...core.logging import get_logger

logger = get_logger("profile.accounts")

PLATFORMS = ("facebook", "instagram", "tiktok", "x", "gmail", "hotmail")
# Brand names, the same in every language (the web panel keeps its own copy).
PLATFORM_LABELS = {
    "facebook": "Facebook",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "x": "X",
    "gmail": "Gmail",
    "hotmail": "Hotmail / Outlook",
}
FIELDS = ("platform", "username", "password", "totp_secret")
PERIOD = 30


class AccountsLocked(Exception):
    """The file exists but this key cannot read it: nothing is read or written."""


def _secret_bytes(secret: str) -> bytes:
    s = "".join(secret.split()).upper().rstrip("=")
    return base64.b32decode(s + "=" * (-len(s) % 8))


def valid_totp_secret(secret: str) -> bool:
    try:
        return len(_secret_bytes(secret)) > 0
    except (binascii.Error, ValueError):
        return False


def totp(secret: str, now: float | None = None, digits: int = 6) -> tuple[str, int]:
    """RFC 6238 code for `secret` (base32) and the seconds it stays valid."""
    now = time.time() if now is None else now
    counter = int(now // PERIOD)
    mac = hmac.new(_secret_bytes(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**digits).zfill(digits), PERIOD - int(now % PERIOD)


def public(account: dict) -> dict:
    """An account without its secrets, for listings."""
    return {
        "id": account["id"],
        "platform": account["platform"],
        "username": account["username"],
        "has_password": bool(account.get("password")),
        "has_totp": bool(account.get("totp_secret")),
    }


class AccountStore:
    def __init__(self, path: str = ACCOUNTS_FILE, key: str = ACCOUNTS_KEY) -> None:
        self.path = pathlib.Path(path)
        self._key = key
        self._lock = threading.Lock()
        self._data: dict[str, list[dict]] | None = None

    # --- file ------------------------------------------------------------------

    def _key_file(self) -> pathlib.Path:
        return self.path.with_suffix(".key")

    def _fernet(self, create: bool) -> Fernet | None:
        if self._key:
            return Fernet(self._key.encode())
        key_file = self._key_file()
        if key_file.exists():
            return Fernet(key_file.read_bytes().strip())
        if not create:
            return None
        key = Fernet.generate_key()
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(key)
        if os.name != "nt":
            key_file.chmod(0o600)
        logger.info("Created the accounts key %s", key_file)
        return Fernet(key)

    def _load(self) -> dict[str, list[dict]]:
        if self._data is not None:
            return self._data
        if not self.path.exists():
            self._data = {}
            return self._data
        fernet = self._fernet(create=False)
        try:
            if fernet is None:
                raise InvalidToken
            self._data = json.loads(fernet.decrypt(self.path.read_bytes()))
        except (InvalidToken, ValueError) as e:
            # Never fall back to an empty store: the next write would wipe the file.
            logger.error("Cannot read %s with this key", self.path)
            raise AccountsLocked from e
        return self._data

    def _save(self) -> None:
        from .manager import _replace

        token = self._fernet(create=True).encrypt(json.dumps(self._data).encode())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(token)
        if os.name != "nt":
            tmp.chmod(0o600)
        try:
            _replace(tmp, self.path)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    # --- accounts --------------------------------------------------------------

    def list(self, profile: str) -> list[dict]:
        with self._lock:
            return [public(a) for a in self._load().get(profile, [])]

    def get(self, profile: str, account_id: str) -> dict | None:
        """The account with its secrets, or None."""
        with self._lock:
            found = next((a for a in self._load().get(profile, []) if a["id"] == account_id), None)
            return dict(found) if found else None

    def add(self, profile: str, fields: dict) -> dict:
        with self._lock:
            data = self._load()
            account = {"id": secrets.token_hex(4), **{f: fields.get(f) or "" for f in FIELDS}}
            data.setdefault(profile, []).append(account)
            self._save()
            return public(account)

    def update(self, profile: str, account_id: str, fields: dict) -> dict | None:
        """Change the given fields; "" clears one. None when there is no such account."""
        with self._lock:
            data = self._load()
            account = next((a for a in data.get(profile, []) if a["id"] == account_id), None)
            if account is None:
                return None
            account.update({f: fields[f] or "" for f in FIELDS if f in fields})
            self._save()
            return public(account)

    def delete(self, profile: str, account_id: str) -> bool:
        with self._lock:
            data = self._load()
            accounts = data.get(profile, [])
            kept = [a for a in accounts if a["id"] != account_id]
            if len(kept) == len(accounts):
                return False
            if kept:
                data[profile] = kept
            else:
                del data[profile]
            self._save()
            return True

    def rename_profile(self, old: str, new: str) -> None:
        with self._lock:
            if not self.path.exists() and self._data is None:
                return
            data = self._load()
            if old in data:
                data[new] = data.pop(old)
                self._save()

    def forget_profile(self, profile: str) -> None:
        with self._lock:
            if not self.path.exists() and self._data is None:
                return
            data = self._load()
            if data.pop(profile, None) is not None:
                self._save()
