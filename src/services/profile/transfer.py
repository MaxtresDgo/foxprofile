import gzip
import hashlib
import json
import os
import pathlib
import secrets
import time
import zipfile
from datetime import datetime

from ...core.config import FINGERPRINT_FILE
from ...core.logging import get_logger
from ...models.profile import ENGINES, NUMBER_MAX, REMARK_MAX, Profile, label_text
from ...utils.validation import (
    valid_or_none,
    validate_locale,
    validate_profile_name,
    validate_timezone,
)
from ..browser.fingerprint import CHROME_PERSONA_FILE

logger = get_logger("profile.transfer")

# The agent's cookie checkpoints of a running profile (save_checkpoint): they
# never leave the PC, and go with the profile dir once it is uploaded.
CHECKPOINTS = "checkpoints"
# Browser caches: rebuilt on demand, often hundreds of MB. Never exported.
_CACHE_DIRS = {"cache2", "startupCache", "Cache", "Code Cache", "GPUCache", "GrShaderCache"}

# Hub agents carry a profile's cookies as portable JSON next to the browser's
# own store: Chrome encrypts its cookie DB per machine (DPAPI, keychain), so the
# DB alone loses every session on another PC. The hash travels with them.
COOKIES_GZ = "cookies.json.gz"
COOKIES_SHA = "cookies.json.gz.sha256"
# What happened to the cookies on the last close and open, and the last exit IP:
# the agent and runner write it, the start page shows it. Travels in the ZIP.
SYNC_META = "sync.json"


def read_meta(profile_dir: str) -> dict:
    try:
        data = json.loads(pathlib.Path(profile_dir, SYNC_META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def update_meta(profile_dir: str, **fields) -> None:
    """Merge `fields` into SYNC_META. Diagnostics only: never fails a sync."""
    try:
        merged = {**read_meta(profile_dir), **fields}
        pathlib.Path(profile_dir, SYNC_META).write_text(json.dumps(merged), encoding="utf-8")
    except OSError as e:
        logger.warning("Could not write %s in %s: %s", SYNC_META, profile_dir, e)


class IntegrityError(ValueError):
    pass


class WrongProfileError(ValueError):
    pass


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def pack_cookies(profile_dir: str, text: str) -> None:
    """Write the cookies (extension JSON) gzipped, with their SHA-256."""
    data = gzip.compress(text.encode("utf-8"))
    pathlib.Path(profile_dir, COOKIES_GZ).write_bytes(data)
    pathlib.Path(profile_dir, COOKIES_SHA).write_text(hashlib.sha256(data).hexdigest())


def read_cookies(profile_dir: str) -> str | None:
    """The packed cookies' JSON, None if there are none; IntegrityError if damaged."""
    gz, sha = pathlib.Path(profile_dir, COOKIES_GZ), pathlib.Path(profile_dir, COOKIES_SHA)
    if not gz.exists():
        return None
    data = gz.read_bytes()
    if not sha.exists() or hashlib.sha256(data).hexdigest() != sha.read_text().strip():
        raise IntegrityError(f"{COOKIES_GZ}: SHA-256 mismatch")
    try:
        return gzip.decompress(data).decode("utf-8")
    except (OSError, EOFError, UnicodeDecodeError) as e:  # gzip checks its own CRC too
        raise IntegrityError(f"{COOKIES_GZ}: {e}") from e


def _checkpoint_index(folder: pathlib.Path) -> list[dict]:
    try:
        index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return index if isinstance(index, list) else []


def _write_atomic(path: pathlib.Path, data: bytes) -> None:
    """A power cut leaves the old file or the new one, never half of it."""
    part = path.with_name(path.name + ".part")
    with open(part, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, path)


def _checkpoint_problem(path: pathlib.Path, sha: str, floor: int) -> str:
    """Why the checkpoint on disk cannot be trusted, "" when it can."""
    try:
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            return "sha256 mismatch"
        items = json.loads(gzip.decompress(data).decode("utf-8"))
    except (OSError, EOFError, ValueError) as e:  # UnicodeDecodeError is a ValueError
        return f"unreadable: {e}"
    if not isinstance(items, list) or not items:
        return "no cookies"
    if len(items) * 2 < floor:
        return f"{len(items)} cookies, under half of the last verified ({floor})"
    return ""


def save_checkpoint(profile_dir: str, text: str) -> dict:
    """Keep a running profile's cookies (extension JSON) in CHECKPOINTS and check
    them back from disk: "verified" only if they read back whole and are not under
    half of the last verified ones. Keeps the newest and the last verified."""
    folder = pathlib.Path(profile_dir, CHECKPOINTS)
    # Not parents=True: a profile already uploaded and removed must not come back.
    folder.mkdir(exist_ok=True)
    index = _checkpoint_index(folder)
    data = gzip.compress(text.encode("utf-8"))
    ts = time.time()
    entry = {
        # Unique even within one clock tick (Windows' is ~15 ms).
        "file": f"{int(ts * 1000)}-{secrets.token_hex(3)}.json.gz",
        "ts": ts,
        "cookies": len(json.loads(text)),
        "sha256": hashlib.sha256(data).hexdigest(),
        "verified": False,
        "reason": "",
    }
    last = next((e for e in reversed(index) if e.get("verified")), None)
    _write_atomic(folder / entry["file"], data)
    _write_atomic(folder / "index.json", json.dumps([*index, entry]).encode())
    entry["reason"] = _checkpoint_problem(
        folder / entry["file"], entry["sha256"], last["cookies"] if last else 0
    )
    entry["verified"] = not entry["reason"]
    keep = [e for e in (last, entry) if e is not None and (e is entry or not entry["verified"])]
    _write_atomic(folder / "index.json", json.dumps(keep).encode())
    for f in folder.iterdir():
        if f.name != "index.json" and f.name not in {e["file"] for e in keep}:
            f.unlink(missing_ok=True)
    return entry


def last_verified(profile_dir: str) -> tuple[dict, str] | None:
    """(entry, text) of the newest verified checkpoint that still reads back whole:
    the last one if it passed its check, else the one before. None if there is none."""
    folder = pathlib.Path(profile_dir, CHECKPOINTS)
    for entry in reversed(_checkpoint_index(folder)):
        path = folder / str(entry.get("file"))
        if entry.get("verified") and not _checkpoint_problem(path, entry.get("sha256", ""), 0):
            return entry, gzip.decompress(path.read_bytes()).decode("utf-8")
    return None


def export_to_zip(
    profile: Profile,
    profile_data_dir: str,
    export_path: str,
    include_data: bool = True,
) -> tuple[bool, str]:
    """Create a ZIP archive of a profile and optionally its browser data."""
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_filename = f"{profile.name}_{timestamp}.zip"
        zip_path = os.path.join(export_path, zip_filename)

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            zipf.writestr("profile.json", json.dumps(profile.to_dict(), indent=2))

            if include_data and pathlib.Path(profile_data_dir).exists():
                for root, dirs, files in os.walk(profile_data_dir):
                    dirs[:] = [d for d in dirs if d not in _CACHE_DIRS]
                    if root == profile_data_dir and CHECKPOINTS in dirs:
                        dirs.remove(CHECKPOINTS)
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.join(
                            "data",
                            os.path.relpath(file_path, profile_data_dir),
                        )
                        zipf.write(file_path, arcname)
            else:
                # The fingerprint is the profile's identity, not browser data:
                # keep it so the imported profile presents the same device.
                for identity in (FINGERPRINT_FILE, CHROME_PERSONA_FILE):
                    fp_file = os.path.join(profile_data_dir, identity)
                    if pathlib.Path(fp_file).exists():
                        zipf.write(fp_file, os.path.join("data", identity))

        logger.info("Exported profile %s to %s", profile.name, zip_path)
        return True, zip_path
    except Exception as e:
        logger.exception("Error exporting profile %s: %s", profile.name, e)
        return False, str(e)


def read_profile_meta(zip_path: str) -> dict | None:
    """profile.json of an archive without extracting anything, or None if unreadable."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zipf:
            return json.loads(zipf.read("profile.json"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None


def import_from_zip(
    zip_path: str,
    data_dir: str,
) -> tuple[bool, Profile | str]:
    """Extract a profile from a ZIP archive.

    Returns (True, Profile) on success or (False, error_message) on failure.
    The caller is responsible for registering the profile in the manager.
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zipf:
            if "profile.json" not in zipf.namelist():
                return False, "Invalid profile archive (missing profile.json)"

            profile_data = json.loads(zipf.read("profile.json"))
            name = profile_data.get("name")

            if not name:
                return False, "Invalid profile data (missing name)"
            valid, msg = validate_profile_name(name)
            if not valid:
                return False, f"Invalid profile name in archive: {msg}"

            profile = Profile(
                name=name,
                proxy=profile_data.get("proxy"),
                os_type=profile_data.get("os_type", "windows"),
                timezone=valid_or_none(profile_data.get("timezone"), validate_timezone),
                locale=valid_or_none(profile_data.get("locale"), validate_locale),
                engine=profile_data.get("engine")
                if profile_data.get("engine") in ENGINES
                else "camoufox",
                number=label_text(profile_data.get("number"), NUMBER_MAX),
                remark=label_text(profile_data.get("remark"), REMARK_MAX),
                chrome_version=profile_data.get("chrome_version"),
            )

            data_files = [f for f in zipf.namelist() if f.startswith("data/")]
            if data_files:
                profile_data_dir = os.path.join(data_dir, name)
                root = pathlib.Path(profile_data_dir).resolve()
                root.mkdir(exist_ok=True, parents=True)

                for file in data_files:
                    if file.endswith("/"):
                        continue
                    target_path = os.path.join(
                        profile_data_dir,
                        os.path.relpath(file, "data"),
                    )
                    # Reject entries such as data/../../evil that would be
                    # written outside the profile's own directory.
                    if not pathlib.Path(target_path).resolve().is_relative_to(root):
                        return False, f"Unsafe path in archive: {file}"
                    pathlib.Path(target_path).parent.mkdir(
                        exist_ok=True,
                        parents=True,
                    )
                    with zipf.open(file) as src, open(target_path, "wb") as dst:
                        dst.write(src.read())

            logger.info("Imported profile from zip: %s", name)
            return True, profile
    except zipfile.BadZipFile:
        return False, "Invalid zip file"
    except Exception as e:
        logger.exception("Error importing profile: %s", e)
        return False, str(e)


def swap_in(
    zip_path: str, tmp: str, name: str, data_root: pathlib.Path, check_name: bool = True
) -> None:
    """Replace data_root/<name> with an archive's data, through `tmp` on the same
    filesystem (the old data ends up in tmp/old). ValueError if the archive is
    unreadable, WrongProfileError if it is another profile's (check_name: a
    snapshot may carry the name the profile had then), IntegrityError if its
    cookies are damaged: good data is never replaced with bad."""
    extract = os.path.join(tmp, "data")
    ok, result = import_from_zip(zip_path, extract)
    if not ok:
        raise ValueError(result)
    if check_name and result.name != name:
        raise WrongProfileError(result.name)
    new = pathlib.Path(extract, result.name)
    new.mkdir(parents=True, exist_ok=True)
    read_cookies(str(new))
    target = data_root / name
    if target.exists():
        target.rename(pathlib.Path(tmp, "old"))
    new.rename(target)
