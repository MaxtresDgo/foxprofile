"""The agent installers the panel serves (Settings > Agents): one per system, the
newest wins.

Kept in an S3 bucket (RustFS, MinIO, AWS; FOXPROFILE_S3_*) when one is set,
otherwise in installers/<system>/ next to HUB_FILE. The bucket stays private:
the hub hands out presigned URLs, so browsers upload to it and download from it
directly and the ~300 MB never go through the hub. Uploads go in parts
(multipart, PART_SIZE each): proxies in front of a store, like Cloudflare,
refuse bodies over 100 MB or that take over 100 s. Uploading from the panel needs a CORS rule on the
bucket for the panel's origin that exposes ETag (docs/HUB.md); files put
there with any S3 tool under fox-profiles/<dev|prod>/installers/<system>/ are
picked up too (config.S3_PREFIX).
"""

from __future__ import annotations

import functools
import pathlib
import re

from ..core import config

PLATFORMS = {"windows": ".msi", "macos": ".pkg"}
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# Each part's URL is checked when that part starts: room for ~300 MB on a slow line.
UPLOAD_EXPIRES = 6 * 3600
# Cloudflare caps a request at 100 MB and at 100 s for the answer (524, no CORS
# headers): 8 MB parts finish in time down to ~1 Mbit/s of upload. S3 wants 5 MB
# or more (but the last).
PART_SIZE = 8 * 1024 * 1024
MAX_SIZE = 4 * 1024**3
DOWNLOAD_EXPIRES = 900
CONTENT_TYPE = "application/octet-stream"


class InstallerError(Exception):
    """A request the caller can fix: unknown system, bad name, missing file.
    Carries a strings.py key and its fields."""

    def __init__(self, key: str, **fields) -> None:
        super().__init__(key)
        self.key, self.fields = key, fields


def check(platform: str, name: str | None = None) -> None:
    if platform not in PLATFORMS:
        raise InstallerError("err_no_installer")
    if name is not None and (not _NAME.match(name) or not name.endswith(PLATFORMS[platform])):
        raise InstallerError("err_installer_name", ext=PLATFORMS[platform])


def in_bucket() -> bool:
    return bool(config.S3_BUCKET)


# --- S3 -----------------------------------------------------------------------


@functools.cache
def _client(public: bool = False):
    """boto3 (requirements-hub.txt) is imported only when a bucket is set. The
    public client only signs URLs, for the address browsers use."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=(config.S3_PUBLIC_URL if public else config.S3_ENDPOINT) or None,
        aws_access_key_id=config.S3_ACCESS_KEY or None,
        aws_secret_access_key=config.S3_SECRET_KEY or None,
        region_name=config.S3_REGION,
        # Path style: self-hosted stores rarely have a DNS name per bucket.
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _key(platform: str, name: str) -> str:
    return f"{config.S3_PREFIX}{platform}/{name}"


def _objects(platform: str) -> list[dict]:
    resp = _client().list_objects_v2(
        Bucket=config.S3_BUCKET, Prefix=f"{config.S3_PREFIX}{platform}/"
    )
    return [o for o in resp.get("Contents", []) if o["Key"].endswith(PLATFORMS[platform])]


def start_upload(platform: str, name: str, size: int) -> dict:
    """A multipart upload and a presigned PUT per part, for the browser."""
    if not 0 < size <= MAX_SIZE:
        raise InstallerError("err_installer_size")
    key = _key(platform, name)
    upload_id = _client().create_multipart_upload(
        Bucket=config.S3_BUCKET, Key=key, ContentType=CONTENT_TYPE
    )["UploadId"]
    sign = _client(public=True).generate_presigned_url
    parts = [
        sign(
            "upload_part",
            Params={
                "Bucket": config.S3_BUCKET,
                "Key": key,
                "UploadId": upload_id,
                "PartNumber": number,
            },
            ExpiresIn=UPLOAD_EXPIRES,
        )
        for number in range(1, -(-size // PART_SIZE) + 1)
    ]
    return {"upload_id": upload_id, "part_size": PART_SIZE, "parts": parts}


def finish_upload(platform: str, name: str, upload_id: str, etags: list[str]) -> dict:
    """Join the parts the browser sent (their ETags, in order)."""
    _client().complete_multipart_upload(
        Bucket=config.S3_BUCKET,
        Key=_key(platform, name),
        UploadId=upload_id,
        MultipartUpload={
            "Parts": [{"ETag": etag, "PartNumber": n} for n, etag in enumerate(etags, 1)]
        },
    )
    return uploaded(platform, name)


def abort_upload(platform: str, name: str, upload_id: str) -> None:
    """Drop the parts of an upload that failed, so they take no space."""
    _client().abort_multipart_upload(
        Bucket=config.S3_BUCKET, Key=_key(platform, name), UploadId=upload_id
    )


def uploaded(platform: str, name: str) -> dict:
    """After an upload: check it landed and drop the older installers."""
    key = _key(platform, name)
    found = {o["Key"]: o for o in _objects(platform)}
    if key not in found:
        raise InstallerError("err_no_installer")
    for old in found.keys() - {key}:
        _client().delete_object(Bucket=config.S3_BUCKET, Key=old)
    return {"platform": platform, "name": name, "size": found[key]["Size"]}


def download_url(platform: str) -> str | None:
    item = newest(platform)
    if item is None:
        return None
    return _client(public=True).generate_presigned_url(
        "get_object",
        Params={
            "Bucket": config.S3_BUCKET,
            "Key": _key(platform, item["name"]),
            "ResponseContentDisposition": f'attachment; filename="{item["name"]}"',
        },
        ExpiresIn=DOWNLOAD_EXPIRES,
    )


# --- local folder ---------------------------------------------------------------


def folder(platform: str) -> pathlib.Path:
    return pathlib.Path(config.HUB_FILE).resolve().parent / "installers" / platform


def local_file(platform: str) -> pathlib.Path | None:
    item = newest(platform)
    return folder(platform) / item["name"] if item else None


# --- both -------------------------------------------------------------------------


def newest(platform: str) -> dict | None:
    """{platform, name, size, time} of the installer the panel offers, or None."""
    if in_bucket():
        items = [
            (o["LastModified"].timestamp(), o["Key"].rsplit("/", 1)[-1], o["Size"])
            for o in _objects(platform)
        ]
    else:
        path = folder(platform)
        files = path.glob("*" + PLATFORMS[platform]) if path.is_dir() else []
        items = [(f.stat().st_mtime, f.name, f.stat().st_size) for f in files]
    if not items:
        return None
    time, name, size = max(items)
    return {"platform": platform, "name": name, "size": size, "time": time}


def listing() -> list[dict]:
    return [item for p in PLATFORMS if (item := newest(p)) is not None]
