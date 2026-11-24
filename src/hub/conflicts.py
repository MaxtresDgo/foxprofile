"""Uploads that started from an older copy than the hub's (two PCs on the same
version, one offline): kept whole instead of replacing the hub's copy, until an
admin or a group lead picks which version stays.

conflicts/<uid>/<id>.zip plus an index.json, next to HUB_FILE. By uid, so a
renamed profile keeps them. A profile with entries here is "in conflict".
"""

import gzip
import json
import os
import pathlib
import secrets
import shutil
import tempfile
import time
import zipfile
from collections import Counter

from ..core.config import HUB_FILE
from ..services.browser.session import TABS_FILE
from ..services.profile.transfer import (
    COOKIES_GZ,
    SYNC_META,
    read_cookies,
    swap_in,
)


class NotFound(LookupError):
    pass


def _root() -> pathlib.Path:
    return pathlib.Path(HUB_FILE).resolve().parent / "conflicts"


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


def _packed(zip_path: str) -> dict:
    try:
        with zipfile.ZipFile(zip_path) as z:
            meta = json.loads(z.read(f"data/{SYNC_META}"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return {}
    return meta.get("packed") or {} if isinstance(meta, dict) else {}


def add(uid: str, zip_path: str, by: str, base: str) -> dict:
    """Keep an upload that would have replaced newer data. Moves `zip_path`."""
    packed = _packed(zip_path)
    entry = {
        "id": f"{int(time.time() * 1000)}-{secrets.token_hex(3)}",
        "ts": time.time(),
        "by": by,
        "pc": packed.get("pc", ""),
        "cookies": packed.get("cookies"),
        "packed_ts": packed.get("ts"),
        "bytes": os.path.getsize(zip_path),
        "base": base,
    }
    folder = _root() / uid
    folder.mkdir(parents=True, exist_ok=True)
    shutil.move(zip_path, folder / f"{entry['id']}.zip")
    _write_index(folder, [*_index(folder), entry])
    return entry


def entries(uid: str) -> list[dict]:
    """Each kept version, with what tells it apart (details)."""
    folder = _root() / uid
    return [{**e, **details(str(folder / f"{e['id']}.zip"))} for e in _index(folder)]


def details(source: str) -> dict:
    """What tells versions apart without opening them, from the hub's profile folder
    or a kept version's ZIP: the Facebook account whose session it holds (c_user,
    only with xs; never a cookie's value otherwise), its cookies by site, its tabs."""
    cookies = tabs = None
    try:
        if source.endswith(".zip"):
            with zipfile.ZipFile(source) as z:
                names = set(z.namelist())
                if f"data/{COOKIES_GZ}" in names:
                    cookies = gzip.decompress(z.read(f"data/{COOKIES_GZ}")).decode("utf-8")
                if f"data/{TABS_FILE}" in names:
                    tabs = z.read(f"data/{TABS_FILE}").decode("utf-8")
        else:
            cookies = read_cookies(source)
            tabs_file = pathlib.Path(source, TABS_FILE)
            tabs = tabs_file.read_text(encoding="utf-8") if tabs_file.is_file() else None
        items = json.loads(cookies or "[]")
        urls = json.loads(tabs or "{}").get("tabs") or []
    except (OSError, ValueError, EOFError, zipfile.BadZipFile):  # IntegrityError too
        return {"facebook": "", "sites": [], "tabs": []}
    fb = {c.get("name"): c.get("value") for c in items if "facebook.com" in str(c.get("domain"))}
    return {
        "facebook": (fb.get("c_user") or "") if fb.get("xs") else "",
        "cookies": len(items),
        "sites": Counter(str(c.get("domain", "")).lstrip(".") for c in items).most_common(4),
        "tabs": [str(u) for u in urls][:5],
    }


def counts() -> dict[str, int]:
    """uid -> how many versions wait for a decision."""
    root = _root()
    if not root.is_dir():
        return {}
    return {d.name: n for d in root.iterdir() if d.is_dir() and (n := len(_index(d)))}


def use(uid: str, entry_id: str, name: str, data_root: pathlib.Path) -> dict:
    """Make a kept version the hub's copy, then close the conflict."""
    folder = _root() / uid
    entry = next((e for e in _index(folder) if e["id"] == entry_id), None)
    if entry is None:
        raise NotFound(entry_id)
    # Same filesystem as the data dir, so the swap is a rename (as an upload).
    tmp = tempfile.mkdtemp(prefix=".conflict-", dir=data_root.parent)
    try:
        swap_in(str(folder / f"{entry_id}.zip"), tmp, name, data_root, check_name=False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    drop(uid)
    return entry


def drop(uid: str) -> None:
    """Keep the hub's copy: discard every kept version."""
    shutil.rmtree(_root() / uid, ignore_errors=True)
