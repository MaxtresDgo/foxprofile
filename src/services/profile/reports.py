"""Latest operator report per profile/account; never contains account secrets."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import threading
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    platform: Literal["facebook", "instagram", "tiktok", "x", "gmail", "hotmail"]
    account_id: str | None = Field(None, min_length=1, max_length=64)
    # Mismas claves que el estado de perfil del panel Army (PerfilEstado).
    status: Literal["inhabilitado", "suspendido", "videoselfie", "codigo"]
    note: str = Field("", max_length=1000)


class ReportConflict(ValueError):
    pass


class ReportStore:
    def __init__(self, path: str | pathlib.Path) -> None:
        self.path = pathlib.Path(path)
        self._lock = threading.Lock()
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._data = {"reports": {}, "receipts": {}}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
        self._data = data

    def list(self, uid: str) -> list[dict]:
        with self._lock:
            return copy.deepcopy(list(self._data["reports"].get(uid, {}).values()))

    def submit(self, uid: str, body: dict, actor: str, username: str) -> dict:
        fingerprint = hashlib.sha256(
            json.dumps([uid, actor, body], sort_keys=True).encode()
        ).hexdigest()
        request_id, now = body["request_id"], time.time()
        with self._lock:
            receipt = self._data["receipts"].get(request_id)
            if receipt:
                if receipt["fingerprint"] != fingerprint:
                    raise ReportConflict("Report request ID was already used with different data")
                return {"id": request_id}
            data = copy.deepcopy(self._data)
            # shortcut: retry receipts live seven days; extend if offline queuing is added.
            data["receipts"] = {
                key: value
                for key, value in data["receipts"].items()
                if value["at"] > now - 7 * 86400
            }
            data["receipts"][request_id] = {"fingerprint": fingerprint, "at": now, "uid": uid}
            slot = body["account_id"] or "platform:" + body["platform"]
            data["reports"].setdefault(uid, {})[slot] = {
                "id": request_id,
                "platform": body["platform"],
                "account_id": body["account_id"],
                "username": username,
                "status": body["status"],
                "note": body["note"],
                "reported_by": actor,
                "reported_at": now,
                "reviewed_by": "",
                "reviewed_at": None,
            }
            self._save(data)
        return {"id": request_id}

    def review(self, uid: str, report_id: str, actor: str) -> dict:
        with self._lock:
            data = copy.deepcopy(self._data)
            report = next(
                (r for r in data["reports"].get(uid, {}).values() if r["id"] == report_id), None
            )
            if report is None:
                raise ReportConflict("A newer report exists; refresh before reviewing")
            if report["reviewed_at"] is None:
                report.update(reviewed_by=actor, reviewed_at=time.time())
                self._save(data)
            return copy.deepcopy(report)

    def forget_profile(self, uid: str) -> None:
        with self._lock:
            data = copy.deepcopy(self._data)
            data["reports"].pop(uid, None)
            data["receipts"] = {k: v for k, v in data["receipts"].items() if v["uid"] != uid}
            self._save(data)


def submit_report(pm, name: str, body: ReportInput, actor: str) -> dict:
    """Validate account ownership against the current store, not launch metadata."""
    with pm._lock:
        profile = pm.profiles.get(name)
        if profile is None:
            raise ValueError("Profile no longer exists")
        accounts = [a for a in pm.accounts.list(name) if a["platform"] == body.platform]
        account = next((a for a in accounts if a["id"] == body.account_id), None)
        if (body.account_id and account is None) or (accounts and account is None):
            raise ValueError("Choose an account belonging to this profile and platform")
        return pm.reports.submit(
            profile.uid, body.model_dump(), actor, account["username"] if account else ""
        )
