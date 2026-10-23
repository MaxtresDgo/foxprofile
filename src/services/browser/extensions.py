"""Browser extensions for profiles: Chrome extensions (.crx, .zip) for the Chrome
engine and Firefox add-ons (.xpi, .zip) for Camoufox.

An extension applies to every profile of its engine (global), to the profiles of
some hub groups, or to some profiles; any profile can be left out of it. Each is
stored once as a normalized ZIP (EXTENSIONS_DIR/<id>.zip) and a browser loads it
unpacked from a cache keyed by that ZIP's SHA-256 (unpack()): on this PC or, in
hub mode, on the agent's, which downloads it from the hub.

Normalizing pins the extension's identity, which is where its own storage lives
(its logins, its settings): Chrome derives an unpacked extension's ID from its
path, which differs on every PC, unless the manifest has a "key" (the store's
own, from the .crx, when there is one); Firefox needs a gecko ID or it makes a
new one at every launch.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile

from ...core.config import EXTENSIONS_DIR, EXTENSIONS_FILE
from ...core.logging import get_logger
from ...core.strings import get_string
from ...models.profile import ENGINES

logger = get_logger("browser.extensions")

MAX_BYTES = 64 * 1024 * 1024
STORE_ID = re.compile(r"\b([a-p]{32})\b")
# The Chrome Web Store's download link for a .crx; prodversion picks the build
# compatible with that Chrome (the Chrome engine's pinned version).
STORE_URL = (
    "https://clients2.google.com/service/update2/crx?response=redirect"
    "&prodversion={version}&acceptformat=crx2,crx3&x=id%3D{id}%26uc"
)


class ExtensionError(ValueError):
    pass


# --- packages ------------------------------------------------------------------


def _proto_fields(buf: bytes) -> list[tuple[int, bytes]]:
    """The length-delimited fields of a protobuf message; others are skipped."""
    fields, i = [], 0

    def varint() -> int:
        nonlocal i
        value = shift = 0
        while True:
            byte = buf[i]
            i += 1
            value |= (byte & 0x7F) << shift
            if byte < 0x80:
                return value
            shift += 7

    while i < len(buf):
        key = varint()
        number, wire = key >> 3, key & 7
        if wire == 0:
            varint()
        elif wire == 1:
            i += 8
        elif wire == 5:
            i += 4
        elif wire == 2:
            size = varint()
            fields.append((number, buf[i : i + size]))
            i += size
        else:
            raise ExtensionError(get_string("err_ext_bad_file"))
    return fields


def chrome_id(public_key_der: bytes) -> str:
    """Chrome's extension ID for a public key: its SHA-256, hex digits as a-p."""
    digest = hashlib.sha256(public_key_der).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def _from_crx(data: bytes) -> tuple[bytes, str | None]:
    """The ZIP inside a .crx and the extension's own public key (base64 DER)."""
    version = int.from_bytes(data[4:8], "little")
    if version == 2:
        key_len = int.from_bytes(data[8:12], "little")
        sig_len = int.from_bytes(data[12:16], "little")
        key = data[16 : 16 + key_len]
        return data[16 + key_len + sig_len :], base64.b64encode(key).decode()
    if version != 3:
        raise ExtensionError(get_string("err_ext_bad_file"))
    size = int.from_bytes(data[8:12], "little")
    header, body = data[12 : 12 + size], data[12 + size :]
    fields = _proto_fields(header)
    # signed_header_data (10000) holds the crx_id (1); the key whose ID it is
    # comes from the sha256_with_rsa proofs (2), each a public_key (1) + signature.
    signed = next((v for n, v in fields if n == 10000), b"")
    crx = next((v for n, v in _proto_fields(signed) if n == 1), b"")
    for number, proof in fields:
        if number != 2:
            continue
        key = next((v for n, v in _proto_fields(proof) if n == 1), b"")
        if key and hashlib.sha256(key).digest()[:16] == crx:
            return body, base64.b64encode(key).decode()
    return body, None


def _new_key() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    public = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    der = public.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(der).decode()


def _files(zip_bytes: bytes) -> dict[str, bytes]:
    """The archive's files, relative to the folder holding manifest.json."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            manifests = sorted((n for n in names if n.endswith("manifest.json")), key=len)
            if not manifests:
                raise ExtensionError(get_string("err_ext_no_manifest"))
            root = manifests[0][: -len("manifest.json")]
            files = {}
            for name in names:
                if not name.startswith(root):
                    continue
                rel = name[len(root) :]
                parts = pathlib.PurePosixPath(rel).parts
                if rel.startswith("/") or ".." in parts:
                    raise ExtensionError(get_string("err_ext_bad_file"))
                files[rel] = z.read(name)
            return files
    except (zipfile.BadZipFile, OSError) as e:
        raise ExtensionError(get_string("err_ext_bad_file")) from e


def _message(text: str, manifest: dict, files: dict[str, bytes]) -> str:
    """A manifest string, resolving __MSG_name__ from the default locale."""
    match = re.fullmatch(r"__MSG_(\w+)__", text or "")
    if not match:
        return text
    locale = manifest.get("default_locale", "en")
    with contextlib.suppress(KeyError, ValueError, AttributeError):
        messages = json.loads(files[f"_locales/{locale}/messages.json"].decode("utf-8-sig"))
        wanted = match.group(1).lower()
        for key, value in messages.items():
            if key.lower() == wanted:
                return value["message"]
    return text


def normalize(data: bytes, filename: str = "", engine: str | None = None) -> tuple[dict, bytes]:
    """Read a .crx, .xpi or .zip: its details and the ZIP a browser loads unpacked."""
    crx_key = None
    if data[:4] == b"Cr24":
        data, crx_key = _from_crx(data)
        engine = engine or "chrome"
    files = _files(data)
    try:
        manifest = json.loads(files["manifest.json"].decode("utf-8-sig"))
    except (KeyError, ValueError) as e:
        raise ExtensionError(get_string("err_ext_no_manifest")) from e
    if not isinstance(manifest, dict):
        raise ExtensionError(get_string("err_ext_no_manifest"))
    gecko = (manifest.get("browser_specific_settings") or manifest.get("applications") or {}).get(
        "gecko"
    )
    if engine is None:
        engine = "camoufox" if filename.lower().endswith(".xpi") or gecko else "chrome"
    if engine not in ENGINES:
        raise ExtensionError(get_string("err_ext_engine"))

    if engine == "chrome":
        manifest["key"] = manifest.get("key") or crx_key or _new_key()
        native = chrome_id(base64.b64decode(manifest["key"]))
        # Chrome refuses an unpacked folder with names it reserves ("_metadata",
        # the store's signatures); _locales is the one it allows.
        keep = lambda n: not n.startswith("_") or n.startswith("_locales/")  # noqa: E731
    else:
        if not (gecko and gecko.get("id")):
            settings = manifest.setdefault("browser_specific_settings", {})
            gecko = settings.setdefault("gecko", {})
            gecko["id"] = f"{uuid.uuid4()}@foxprofile"
        native = gecko["id"]
        # The signature no longer matches once the manifest changes.
        keep = lambda n: not n.startswith("META-INF/")  # noqa: E731

    files["manifest.json"] = json.dumps(manifest, indent=2, ensure_ascii=False).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            if keep(name):
                # Fixed dates: the same extension always gives the same SHA-256.
                z.writestr(zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0)), files[name])
    meta = {
        "id": hashlib.sha256(f"{engine}:{native}".encode()).hexdigest()[:16],
        "engine": engine,
        "native_id": native,
        "name": _message(manifest.get("name", ""), manifest, files)[:120] or filename,
        "version": str(manifest.get("version", ""))[:40],
    }
    return meta, buf.getvalue()


def sha256(path: str | pathlib.Path) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def unpack(archive: str | pathlib.Path, ext_id: str, digest: str, cache: pathlib.Path) -> str:
    """The extension's folder in `cache`, unpacked from `archive` once per version."""
    target = cache / f"{ext_id}-{digest[:12]}"
    if (target / "manifest.json").exists():
        return str(target.resolve())
    cache.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix=f".{ext_id}-", dir=cache))
    try:
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                path = (tmp / name).resolve()
                if not path.is_relative_to(tmp.resolve()):
                    raise ExtensionError(get_string("err_ext_bad_file"))
            z.extractall(tmp)
        with contextlib.suppress(OSError):  # another launch unpacked it first
            tmp.rename(target)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # ponytail: older versions stay in the cache, since a running browser may still
    # read them; sweep the cache at agent start if it ever grows.
    return str(target.resolve())


def from_store(ref: str, chrome_version: str) -> tuple[bytes, str]:
    """Download a Chrome Web Store extension (its ID or its store URL) as a .crx."""
    match = STORE_ID.search(ref.strip().lower())
    if not match:
        raise ExtensionError(get_string("err_ext_store_id"))
    store_id = match.group(1)
    url = STORE_URL.format(version=urllib.parse.quote(chrome_version), id=store_id)
    request = urllib.request.Request(url, headers={"User-Agent": "FoxProfile"})
    try:
        with urllib.request.urlopen(request, timeout=60) as r:
            data = r.read(MAX_BYTES + 1)
    except OSError as e:
        raise ExtensionError(get_string("err_ext_store_download", error=e)) from e
    if data[:4] != b"Cr24":
        raise ExtensionError(get_string("err_ext_store_missing", id=store_id))
    return data, store_id


# --- the catalogue -------------------------------------------------------------


class ExtensionStore:
    """The extensions and where they apply, in EXTENSIONS_FILE; their ZIPs in
    EXTENSIONS_DIR."""

    def __init__(self, path: str = EXTENSIONS_FILE, folder: str = EXTENSIONS_DIR) -> None:
        self.path = pathlib.Path(path)
        self.folder = pathlib.Path(folder)
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}
        with contextlib.suppress(FileNotFoundError):
            self._items = json.loads(self.path.read_text(encoding="utf-8")).get("extensions", {})

    def _save(self) -> None:
        """Caller holds self._lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"extensions": self._items}, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def list(self) -> list[dict]:
        with self._lock:
            return sorted((dict(e) for e in self._items.values()), key=lambda e: e["name"].lower())

    def get(self, ext_id: str) -> dict:
        with self._lock:
            if ext_id not in self._items:
                raise KeyError(ext_id)
            return dict(self._items[ext_id])

    def archive(self, ext_id: str) -> pathlib.Path:
        self.get(ext_id)
        return self.folder / f"{ext_id}.zip"

    def add(self, data: bytes, filename: str = "", engine: str | None = None, **source) -> dict:
        """Store an extension. The same extension again replaces its files and
        keeps where it applies (an update)."""
        if len(data) > MAX_BYTES:
            raise ExtensionError(get_string("err_ext_too_big"))
        meta, zip_bytes = normalize(data, filename, engine)
        self.folder.mkdir(parents=True, exist_ok=True)
        archive = self.folder / f"{meta['id']}.zip"
        tmp = archive.with_suffix(".tmp")
        tmp.write_bytes(zip_bytes)
        os.replace(tmp, archive)
        with self._lock:
            old = self._items.get(meta["id"], {})
            self._items[meta["id"]] = entry = {
                "global": False,
                "groups": [],
                "profiles": [],
                "excluded": [],
                **{k: old[k] for k in ("global", "groups", "profiles", "excluded") if k in old},
                **meta,
                "source": source.get("source", "upload"),
                "store_id": source.get("store_id"),
                "sha256": hashlib.sha256(zip_bytes).hexdigest(),
                "size": len(zip_bytes),
                "updated": int(time.time()),
            }
            self._save()
        logger.info("Extension %s %s (%s) stored", entry["name"], entry["version"], entry["id"])
        return dict(entry)

    def set_scope(self, ext_id: str, **scope) -> dict:
        """Change any of global (bool), groups, profiles and excluded (names)."""
        with self._lock:
            entry = self._items[ext_id]
            if scope.get("global") is not None:
                entry["global"] = bool(scope["global"])
            for key in ("groups", "profiles", "excluded"):
                if scope.get(key) is not None:
                    entry[key] = sorted({str(v) for v in scope[key] if str(v).strip()})
            self._save()
            return dict(entry)

    def delete(self, ext_id: str) -> None:
        with self._lock:
            del self._items[ext_id]
            self._save()
        (self.folder / f"{ext_id}.zip").unlink(missing_ok=True)

    def for_profile(self, name: str, engine: str, group: str | None = None) -> list[dict]:
        """What a profile loads: global + its group's + its own, minus its exclusions."""
        with self._lock:
            return [
                {"id": e["id"], "sha256": e["sha256"], "name": e["name"]}
                for e in sorted(self._items.values(), key=lambda e: e["name"].lower())
                if e["engine"] == engine
                and name not in e["excluded"]
                and (e["global"] or name in e["profiles"] or (group and group in e["groups"]))
            ]

    def dirs(self, wanted: list[dict]) -> list[str]:
        """Unpacked folders of for_profile()'s extensions, for a launch on this PC."""
        cache = self.folder / "unpacked"
        return [unpack(self.archive(e["id"]), e["id"], e["sha256"], cache) for e in wanted]

    def rename_profile(self, old: str, new: str) -> None:
        self._replace(old, [new])

    def forget_profile(self, name: str) -> None:
        self._replace(name, [])

    def _replace(self, name: str, by: list[str]) -> None:
        with self._lock:
            changed = False
            for entry in self._items.values():
                for key in ("profiles", "excluded"):
                    if name in entry[key]:
                        entry[key] = sorted({*entry[key]} - {name} | {*by})
                        changed = True
            if changed:
                self._save()
