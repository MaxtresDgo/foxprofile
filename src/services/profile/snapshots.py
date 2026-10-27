"""Snapshots of a profile's browser data in an S3 bucket (a NAS running RustFS,
MinIO, AWS; config.SNAPSHOT_S3_*): the server keeps only the current version on
its disk, the bucket the last SNAPSHOTS_KEEP of each profile.

Each snapshot is an export_to_zip archive (no caches, never the site accounts) at
<SNAPSHOT_PREFIX><uid>/<id>.zip: filed under the profile's uid, which a rename
keeps, and the id being the UTC time it was taken, so keys sort by age. Each
object also carries the profile's settings (name, proxy, OS, engine...) as
metadata: a deleted profile's snapshots stay (orphans), and the profile can be
put back from them.

A hub sync uploads the agent's checked bundle as it is; create() zips a stopped
profile; restore() saves the current data first, so a restore can be undone.
"""

from __future__ import annotations

import base64
import binascii
import functools
import json
import os
import pathlib
import re
import shutil
import tempfile
from datetime import datetime, timezone
from threading import Thread

from ...core import config
from ...core.logging import get_logger
from ...models.profile import UID_RE, Profile
from .transfer import export_to_zip, swap_in

logger = get_logger("profile.snapshots")

ID_RE = re.compile(r"\d{8}-\d{6}-\d{3}")


class SnapshotNotFound(LookupError):
    pass


def enabled() -> bool:
    return bool(config.SNAPSHOT_S3_BUCKET) and config.SNAPSHOTS_KEEP > 0


@functools.cache
def _client():
    """boto3 (requirements-hub.txt) is imported only when a bucket is set."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=config.SNAPSHOT_S3_ENDPOINT or None,
        aws_access_key_id=config.SNAPSHOT_S3_ACCESS_KEY or None,
        aws_secret_access_key=config.SNAPSHOT_S3_SECRET_KEY or None,
        region_name=config.SNAPSHOT_S3_REGION,
        # Path style: self-hosted stores rarely have a DNS name per bucket.
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def new_id() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%d-%H%M%S-") + f"{now.microsecond // 1000:03d}"


def created(snap_id: str) -> str:
    when = datetime.strptime(snap_id[:15], "%Y%m%d-%H%M%S")
    return when.replace(tzinfo=timezone.utc).isoformat()


def _key(uid: str, snap_id: str) -> str:
    return f"{config.SNAPSHOT_PREFIX}{uid}/{snap_id}.zip"


def _objects(prefix: str) -> list[dict]:
    pages = (
        _client()
        .get_paginator("list_objects_v2")
        .paginate(Bucket=config.SNAPSHOT_S3_BUCKET, Prefix=prefix)
    )
    return [o for page in pages for o in page.get("Contents", [])]


def _sizes(uid: str) -> dict[str, int]:
    """The profile's snapshots: id -> size in bytes."""
    prefix = f"{config.SNAPSHOT_PREFIX}{uid}/"
    sizes = {}
    for o in _objects(prefix):
        snap_id = o["Key"][len(prefix) :].removesuffix(".zip")
        if ID_RE.fullmatch(snap_id):
            sizes[snap_id] = o["Size"]
    return sizes


def _ids(uid: str) -> list[str]:
    """Newest first: ids are times, so they sort by age."""
    return sorted(_sizes(uid), reverse=True)


def list_snapshots(uid: str) -> list[dict]:
    sizes = _sizes(uid)
    return [{"id": i, "created": created(i), "size": sizes[i]} for i in sorted(sizes, reverse=True)]


def overview() -> dict[str, dict]:
    """Every profile in the bucket, by uid: how many snapshots, their size, the newest."""
    groups: dict[str, dict] = {}
    for o in _objects(config.SNAPSHOT_PREFIX):
        uid, _, rest = o["Key"][len(config.SNAPSHOT_PREFIX) :].partition("/")
        snap_id = rest.removesuffix(".zip")
        if not (UID_RE.fullmatch(uid) and ID_RE.fullmatch(snap_id)):
            continue
        group = groups.setdefault(uid, {"count": 0, "size": 0, "newest": snap_id})
        group["count"] += 1
        group["size"] += o["Size"]
        group["newest"] = max(group["newest"], snap_id)
    return groups


def saved_profile(uid: str) -> dict:
    """The settings stored with the newest snapshot: what the profile was. {} if
    the object carries none."""
    ids = _ids(uid)
    if not ids:
        raise SnapshotNotFound(uid)
    head = _client().head_object(Bucket=config.SNAPSHOT_S3_BUCKET, Key=_key(uid, ids[0]))
    try:
        return json.loads(base64.urlsafe_b64decode(head.get("Metadata", {})["profile"]))
    except (KeyError, ValueError, binascii.Error):
        return {}


def upload(profile: Profile, zip_path: str, snap_id: str | None = None) -> str:
    snap_id = snap_id or new_id()
    # Metadata is ASCII only (and 2 KB): the settings go base64-encoded.
    meta = base64.urlsafe_b64encode(json.dumps(profile.to_dict()).encode()).decode()
    _client().upload_file(
        zip_path,
        config.SNAPSHOT_S3_BUCKET,
        _key(profile.uid, snap_id),
        ExtraArgs={"Metadata": {"profile": meta}},
    )
    for old in _ids(profile.uid)[config.SNAPSHOTS_KEEP :]:
        _client().delete_object(Bucket=config.SNAPSHOT_S3_BUCKET, Key=_key(profile.uid, old))
    logger.info("Snapshot %s of %s saved", snap_id, profile.name)
    return snap_id


def upload_later(profile: Profile, zip_path: str) -> None:
    """Upload in the background (moving the file out of the caller's way first):
    a sync must not wait for the NAS, nor fail because of it."""
    snap_id = new_id()
    tmp = tempfile.mkdtemp(prefix="foxprofile-snapshot-")
    path = shutil.move(zip_path, tmp)

    def run() -> None:
        try:
            upload(profile, path, snap_id)
        except Exception as e:
            logger.warning("Snapshot of %s not saved: %s", profile.name, e)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    Thread(target=run, daemon=True).start()


def create(profile: Profile, data_dir: str) -> str:
    tmp = tempfile.mkdtemp(prefix="foxprofile-snapshot-")
    try:
        ok, result = export_to_zip(profile, data_dir, tmp, include_data=True)
        if not ok:
            raise OSError(result)
        return upload(profile, result)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def restore(profile: Profile, snap_id: str, data_root: pathlib.Path) -> None:
    """Put a snapshot back as the profile's data. The caller holds the profile
    (stopped, BrowserLauncher.exclusive)."""
    if not ID_RE.fullmatch(snap_id) or snap_id not in _ids(profile.uid):
        raise SnapshotNotFound(snap_id)
    data_root.mkdir(parents=True, exist_ok=True)
    # Same filesystem as the data dir, so the swap is a rename.
    tmp = tempfile.mkdtemp(prefix=".restore-", dir=data_root.parent)
    try:
        zip_path = os.path.join(tmp, "snapshot.zip")
        _client().download_file(config.SNAPSHOT_S3_BUCKET, _key(profile.uid, snap_id), zip_path)
        current = data_root / profile.name
        if current.is_dir() and any(current.iterdir()):
            create(profile, str(current))  # the undo point
        # The archive may carry the name the profile had back then.
        swap_in(zip_path, tmp, profile.name, data_root, check_name=False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    logger.info("Restored %s to snapshot %s", profile.name, snap_id)


def delete_all(uid: str) -> int:
    ids = _ids(uid)
    for snap_id in ids:
        _client().delete_object(Bucket=config.SNAPSHOT_S3_BUCKET, Key=_key(uid, snap_id))
    logger.info("Deleted %d snapshots of %s", len(ids), uid)
    return len(ids)
