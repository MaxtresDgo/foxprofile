"""Hub users, sessions, profile owners and groups, persisted in one JSON file.

Users are keyed by the email they sign in with on the identity platform
(hub/platform_auth.py), which owns their password and role; this file keeps the
rest. Session tokens are stateless and signed with the server secret
(FOXPROFILE_API_TOKEN).
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
import unicodedata

from ..core.strings import get_string
from ..services.profile.manager import _replace

ROLES = ("admin", "operator")
GROUP_ROLES = ("member", "lead")
SESSION_TTL = 12 * 3600
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
KEY_PREFIX = "fpk_"
KEY_FIELDS = ("key", "agent_key", "army_key")


def group_name_for(text: str) -> str:
    """A valid group name for free text, such as an AdsPower group: accents
    dropped, anything else outside A-Z, 0-9, - and _ turned into "-"."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", plain).strip("-")[:32].strip("-")
    return name or "adspower"


class HubError(ValueError):
    """Invalid user input; the message is safe to show."""


class HubStore:
    def __init__(self, path: str, secret: str) -> None:
        if len(secret) < 24:
            raise SystemExit(
                "Hub mode signs sessions with FOXPROFILE_API_TOKEN: set it to 24+ random characters."
            )
        self._path = pathlib.Path(path)
        self._secret = secret.encode()
        self._lock = threading.RLock()
        self.users: dict[str, dict] = {}
        self.owners: dict[str, str] = {}
        # group -> {user: "member" | "lead"}; profile -> group (at most one).
        self.groups: dict[str, dict[str, str]] = {}
        self.profile_groups: dict[str, str] = {}
        if self._path.exists():
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self.users = data.get("users", {})
            self.owners = data.get("owners", {})
            self.groups = data.get("groups", {})
            self.profile_groups = data.get("profile_groups", {})

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "users": self.users,
                    "owners": self.owners,
                    "groups": self.groups,
                    "profile_groups": self.profile_groups,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        _replace(tmp, self._path)

    # --- users -------------------------------------------------------------

    def delete_user(self, name: str) -> None:
        with self._lock:
            if self.users.pop(name, None) is None:
                raise KeyError(name)
            # Their profiles stay, visible to admins until reassigned.
            self.owners = {p: u for p, u in self.owners.items() if u != name}
            for members in self.groups.values():
                members.pop(name, None)
            self._save()

    def list_users(self) -> list[dict]:
        with self._lock:
            return [
                {"name": n, "role": u["role"], "display": u.get("display", "")}
                for n, u in sorted(self.users.items())
            ]

    # --- sessions ------------------------------------------------------------

    def sign_in(self, name: str, role: str, display: str = "") -> str:
        """A session token for a user the identity platform just let in, creating
        their local account on first sign-in; role and display name follow the platform's."""
        self.ensure_user(name, role, display)
        return self._sign(name, int(time.time()) + SESSION_TTL)

    def ensure_user(self, name: str, role: str, display: str = "") -> dict:
        """Create the local account if missing; role and display name follow the caller's."""
        if role not in ROLES:
            raise HubError(get_string("err_role", values=", ".join(ROLES)))
        with self._lock:
            user = self.users.setdefault(name, {})
            if (user.get("role"), user.get("display", "")) != (role, display):
                user.update(role=role, display=display)
                self._save()
            return self._who(name)

    def _sig(self, name: str, exp: int) -> str:
        return hmac.new(self._secret, f"{name}.{exp}".encode(), hashlib.sha256).hexdigest()

    def _sign(self, name: str, exp: int) -> str:
        return f"{name}.{exp}.{self._sig(name, exp)}"

    def new_key(self, name: str, field: str = "key") -> str:
        """A long-lived API key that replaces the previous one of its kind: `key` for
        MCP clients and scripts, `agent_key` for the user's PC agent, `army_key` for
        Army's panel, which opens this user's profiles in their name."""
        assert field in KEY_FIELDS
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            self.users[name][field] = hashlib.sha256(key.encode()).hexdigest()
            self._save()
        return key

    def _match_key(self, key: str) -> tuple[str, str] | None:
        """(user, kind) of a personal key, kind being one of KEY_FIELDS. Lock held."""
        digest = hashlib.sha256(key.encode()).hexdigest()
        for name, user in self.users.items():
            for field in KEY_FIELDS:
                if user.get(field) and hmac.compare_digest(user[field], digest):
                    return name, field
        return None

    def _verify_key(self, key: str) -> dict | None:
        with self._lock:
            match = self._match_key(key)
            return self._who(match[0]) if match else None

    def key_kind(self, key: str) -> str | None:
        """Which kind of personal key this is ("key", "agent_key", "army_key"), or None."""
        with self._lock:
            match = self._match_key(key)
        return match[1] if match else None

    def verify(self, token: str) -> dict | None:
        """{"name", "role", "display"} for a valid personal key or unexpired session token."""
        if token.startswith(KEY_PREFIX):
            return self._verify_key(token)
        try:
            name, exp_s, sig = token.rsplit(".", 2)  # emails contain dots
            exp = int(exp_s)
        except ValueError:
            return None
        with self._lock:
            if name not in self.users or exp < time.time():
                return None
            if not hmac.compare_digest(sig, self._sig(name, exp)):
                return None
            return self._who(name)

    def _who(self, name: str) -> dict:
        user = self.users[name]
        return {"name": name, "role": user["role"], "display": user.get("display", "")}

    # --- profile owners ------------------------------------------------------

    def can(self, user: dict, profile: str) -> bool:
        if user["role"] == "admin" or self.owners.get(profile) == user["name"]:
            return True
        group = self.profile_groups.get(profile)
        return group is not None and user["name"] in self.groups.get(group, {})

    def set_owner(self, profile: str, user: str | None) -> None:
        with self._lock:
            if user is not None and user not in self.users:
                raise HubError(get_string("err_unknown_user"))
            if user is None:
                self.owners.pop(profile, None)
            else:
                self.owners[profile] = user
            self._save()

    def rename_profile(self, old: str, new: str) -> None:
        with self._lock:
            for mapping in (self.owners, self.profile_groups):
                if old in mapping:
                    mapping[new] = mapping.pop(old)
            self._save()

    def forget_profile(self, profile: str) -> None:
        """Drop a deleted profile's owner and group."""
        with self._lock:
            self.owners.pop(profile, None)
            self.profile_groups.pop(profile, None)
            self._save()

    # --- groups --------------------------------------------------------------

    def is_lead(self, user: dict, group: str) -> bool:
        return user["role"] == "admin" or self.groups.get(group, {}).get(user["name"]) == "lead"

    def add_group(self, name: str) -> None:
        if not _NAME.match(name):
            raise HubError(get_string("err_group_name"))
        with self._lock:
            if name in self.groups:
                raise HubError(get_string("err_group_exists"))
            self.groups[name] = {}
            self._save()

    def delete_group(self, name: str) -> None:
        with self._lock:
            if self.groups.pop(name, None) is None:
                raise KeyError(name)
            # Its profiles stay with their owners (and admins).
            self.profile_groups = {p: g for p, g in self.profile_groups.items() if g != name}
            self._save()

    def set_member(self, group: str, user: str, role: str | None) -> None:
        """Add `user` to `group` with `role`, or remove them when role is None."""
        with self._lock:
            if group not in self.groups:
                raise KeyError(group)
            if role is None:
                self.groups[group].pop(user, None)
            else:
                if role not in GROUP_ROLES:
                    raise HubError(get_string("err_role", values=", ".join(GROUP_ROLES)))
                if user not in self.users:
                    raise HubError(get_string("err_unknown_user"))
                self.groups[group][user] = role
            self._save()

    def set_profile_group(self, profile: str, group: str | None) -> None:
        with self._lock:
            if group is None:
                self.profile_groups.pop(profile, None)
            elif group not in self.groups:
                raise HubError(get_string("err_unknown_group"))
            else:
                self.profile_groups[profile] = group
            self._save()

    def list_groups(self, user: dict) -> list[dict]:
        """All groups for admins, otherwise the ones `user` belongs to."""
        with self._lock:
            counts: dict[str, int] = {}
            for g in self.profile_groups.values():
                counts[g] = counts.get(g, 0) + 1
            return [
                {
                    "name": g,
                    "members": dict(sorted(members.items())),
                    "my_role": "lead" if user["role"] == "admin" else members.get(user["name"]),
                    "profiles": counts.get(g, 0),
                }
                for g, members in sorted(self.groups.items())
                if user["role"] == "admin" or user["name"] in members
            ]
