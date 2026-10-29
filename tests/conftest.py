import itertools
import pathlib

import pytest


@pytest.fixture(autouse=True)
def _no_chrome_download(monkeypatch):
    """Creating a Chrome profile starts a ~150 MB background download of the Chrome
    engine (chrome_prefetch). In a test it would also install the browser that
    chrome_engine/tests then run against. Tests of the prefetch itself set
    is_installed to False."""
    from src.services.browser import chrome_prefetch

    monkeypatch.setattr(chrome_prefetch, "is_installed", lambda *a: True)


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.metadata = {}

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix):
        found = [
            {"Key": k, "Size": len(v)} for k, v in self.objects.items() if k.startswith(Prefix)
        ]
        yield {"Contents": found} if found else {}

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.objects[key] = pathlib.Path(path).read_bytes()
        self.metadata[key] = (ExtraArgs or {}).get("Metadata", {})

    def head_object(self, Bucket, Key):
        return {"Metadata": self.metadata[Key]}

    def download_file(self, bucket, key, path):
        pathlib.Path(path).write_bytes(self.objects[key])

    def delete_object(self, Bucket, Key):
        del self.objects[Key]
        self.metadata.pop(Key, None)


class Now:
    """Thread that runs at once, so a test sees the background upload."""

    def __init__(self, target, daemon):
        self.target = target

    def start(self):
        self.target()


@pytest.fixture
def s3(monkeypatch):
    """Snapshots on, into an in-memory bucket; ids count up one second each."""
    from src.core import config
    from src.services.profile import snapshots

    fake = FakeS3()
    ids = (f"20261027-1200{n:02d}-000" for n in itertools.count())
    monkeypatch.setattr(snapshots, "_client", lambda: fake)
    monkeypatch.setattr(snapshots, "new_id", lambda: next(ids))
    monkeypatch.setattr(snapshots, "Thread", Now)
    monkeypatch.setattr(config, "SNAPSHOT_S3_BUCKET", "nas")
    monkeypatch.setattr(config, "SNAPSHOTS_KEEP", 5)
    return fake
