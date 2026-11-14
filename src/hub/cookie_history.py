"""The last cookie packs of each hub profile, to go back to one that worked.

Every accepted upload leaves its cookies.json.gz here, next to HUB_FILE:
cookie-history/<uid>/<id>.json.gz plus an index.json (newest last). By uid, so
the history follows a renamed profile. A few KB each; COOKIE_HISTORY per profile.
"""

import gzip
import hashlib
import json
import os
import pathlib
import secrets
import shutil
import time
from collections import Counter

from ..core.config import COOKIE_HISTORY, HUB_FILE
from ..services.profile.transfer import pack_cookies, read_cookies, read_meta, update_meta

ID_CHARS = set("0123456789abcdef-")


class NotFound(LookupError):
    pass


def _folder(uid: str) -> pathlib.Path:
    return pathlib.Path(HUB_FILE).resolve().parent / "cookie-history" / uid


def _index(folder: pathlib.Path) -> list[dict]:
    try:
        index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return index if isinstance(index, list) else []


def _write_index(folder: pathlib.Path, index: list[dict]) -> None:
    part = folder / "index.json.part"
    part.write_text(json.dumps(index), encoding="utf-8")
    os.replace(part, folder / "index.json")


def add(uid: str, profile_dir: str, by: str) -> dict | None:
    """Keep the profile's packed cookies, as just uploaded. None if it has none."""
    if not COOKIE_HISTORY:
        return None
    text = read_cookies(profile_dir)  # the upload was checked: it reads back whole
    if text is None:
        return None
    items = json.loads(text)
    packed = read_meta(profile_dir).get("packed") or {}
    data = gzip.compress(text.encode("utf-8"))
    entry = {
        "id": f"{int(time.time() * 1000)}-{secrets.token_hex(3)}",
        "ts": time.time(),
        "by": by,
        "pc": packed.get("pc", ""),
        "source": packed.get("source") or "close",
        "cookies": len(items),
        "sites": Counter(str(c.get("domain", "")).lstrip(".") for c in items).most_common(5),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    folder = _folder(uid)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{entry['id']}.json.gz").write_bytes(data)
    index = [*_index(folder), entry]
    for old in index[:-COOKIE_HISTORY]:
        (folder / f"{old['id']}.json.gz").unlink(missing_ok=True)
    _write_index(folder, index[-COOKIE_HISTORY:])
    return entry


def entries(uid: str) -> list[dict]:
    """Newest first; `suspicious` when a pack has under half the cookies of the one before."""
    index = _index(_folder(uid))
    out = []
    for i, entry in enumerate(index):
        before = index[i - 1]["cookies"] if i else 0
        out.append({**entry, "suspicious": entry["cookies"] * 2 < before})
    return out[::-1]


def restore(uid: str, entry_id: str, profile_dir: str, by: str) -> dict:
    """Make a kept pack the hub copy's cookies: the next open loads them."""
    folder = _folder(uid)
    entry = next((e for e in _index(folder) if e.get("id") == entry_id), None)
    if entry is None or not set(entry_id) <= ID_CHARS:
        raise NotFound(entry_id)
    data = (folder / f"{entry_id}.json.gz").read_bytes()
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise ValueError("cookie history: SHA-256 mismatch")
    pathlib.Path(profile_dir).mkdir(parents=True, exist_ok=True)
    pack_cookies(profile_dir, gzip.decompress(data).decode("utf-8"))
    packed = {"ts": time.time(), "pc": "hub", "cookies": entry["cookies"], "error": ""}
    packed |= {"source": "history", "history_ts": entry["ts"], "by": by}
    update_meta(profile_dir, packed=packed)
    return entry


def drop(uid: str) -> None:
    shutil.rmtree(_folder(uid), ignore_errors=True)
