"""Profile snapshots in a bucket (services/profile/snapshots.py), against an
in-memory S3 (the s3 fixture in conftest)."""

import io
import json
import pathlib
import zipfile

import pytest
from fastapi.testclient import TestClient

from src.api import app as app_module
from src.api import auth
from src.api.app import create_app
from src.core import config
from src.core.container import Container
from src.models.profile import Profile
from src.services.profile import snapshots
from src.services.profile.manager import ProfileManager


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auth, "API_TOKEN", "")
    monkeypatch.setattr(app_module, "API_TOKEN", "")
    container = Container()
    client = TestClient(create_app(container), base_url="http://127.0.0.1")
    client.post("/api/v1/profiles", json={"name": "p1"})
    return client, container


def test_only_the_newest_are_kept(s3, tmp_path):
    archive = tmp_path / "a.zip"
    archive.write_bytes(b"zip")
    profile = Profile(name="p1")
    for _ in range(7):
        snapshots.upload(profile, str(archive))
    ids = [s["id"] for s in snapshots.list_snapshots(profile.uid)]
    assert ids == [f"20261027-1200{n:02d}-000" for n in (6, 5, 4, 3, 2)]
    assert snapshots.list_snapshots(Profile(name="p2").uid) == []


def test_profiles_get_a_uid_that_survives_reload(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pathlib.Path("profiles.json").write_text(json.dumps({"old": {"name": "old"}}))
    uid = ProfileManager().profiles["old"].uid
    assert len(uid) == 32
    assert json.loads(pathlib.Path("profiles.json").read_text())["old"]["uid"] == uid
    assert ProfileManager().profiles["old"].uid == uid


def test_save_and_restore_through_the_api(s3, client):
    client, container = client
    data = pathlib.Path("camoufox_data/p1")
    (data / "cache2").mkdir(parents=True, exist_ok=True)
    (data / "cookies.sqlite").write_bytes(b"good")
    (data / "cache2/junk").write_bytes(b"x")
    accounts = container.profile_manager.accounts
    accounts.add("p1", {"platform": "x", "username": "u", "password": "pass"})

    base = "/api/v1/profiles/p1/snapshots"
    assert client.get("/api/v1/info").json()["snapshots"] is True
    good = client.post(base).json()["id"]
    names = zipfile.ZipFile(io.BytesIO(next(iter(s3.objects.values())))).namelist()
    assert "data/cookies.sqlite" in names and not any("cache2" in n for n in names)

    (data / "cookies.sqlite").write_bytes(b"corrupt")
    assert client.post(f"{base}/{good}/restore").status_code == 200
    assert (data / "cookies.sqlite").read_bytes() == b"good"
    # The state it replaced became a snapshot: the restore can be undone.
    listed = client.get(base).json()["snapshots"]
    assert len(listed) == 2 and listed[0]["id"] != good
    uid = container.profile_manager.profiles["p1"].uid
    undo = zipfile.ZipFile(io.BytesIO(s3.objects[snapshots._key(uid, listed[0]["id"])]))
    assert undo.read("data/cookies.sqlite") == b"corrupt"
    # Site accounts live apart and are never part of it.
    assert [a["username"] for a in accounts.list("p1")] == ["u"]


def test_snapshots_follow_a_rename(s3, client):
    client, _ = client
    pathlib.Path("camoufox_data/p1/cookies.sqlite").write_bytes(b"before")
    snap = client.post("/api/v1/profiles/p1/snapshots").json()["id"]
    assert client.patch("/api/v1/profiles/p1", json={"name": "p2"}).status_code == 200
    base = "/api/v1/profiles/p2/snapshots"
    assert [s["id"] for s in client.get(base).json()["snapshots"]] == [snap]
    pathlib.Path("camoufox_data/p2/cookies.sqlite").write_bytes(b"after")
    # The archive still says p1: it is the same profile all the same.
    assert client.post(f"{base}/{snap}/restore").status_code == 200
    assert pathlib.Path("camoufox_data/p2/cookies.sqlite").read_bytes() == b"before"


def test_restore_refuses_bad_ids_damaged_archives_and_running_profiles(s3, client, monkeypatch):
    client, container = client
    base = "/api/v1/profiles/p1/snapshots"
    assert client.post(f"{base}/..%2Fx/restore").status_code == 404
    assert client.post(f"{base}/20261027-120000-000/restore").status_code == 404

    uid = container.profile_manager.profiles["p1"].uid
    s3.objects[snapshots._key(uid, "20261027-120000-000")] = b"not a zip"
    pathlib.Path("camoufox_data/p1/keep").write_text("x")
    assert client.post(f"{base}/20261027-120000-000/restore").status_code == 422
    assert pathlib.Path("camoufox_data/p1/keep").exists()

    monkeypatch.setattr(container.browser_launcher, "_busy", {"p1"})
    assert client.post(base).status_code == 409


def test_orphans_can_be_put_back_or_deleted(s3, client):
    client, container = client
    pm = container.profile_manager
    client.patch("/api/v1/profiles/p1", json={"name": "p1", "proxy": "http://198.51.100.7:8080"})
    pathlib.Path("camoufox_data/p1/cookies.sqlite").write_bytes(b"session")
    client.post("/api/v1/profiles/p1/snapshots")
    uid = pm.profiles["p1"].uid
    client.post("/api/v1/profiles", json={"name": "keep"})
    client.post("/api/v1/profiles/keep/snapshots")

    assert client.delete("/api/v1/profiles/p1").status_code == 200
    rows = client.get("/api/v1/snapshots").json()["profiles"]
    assert [(r["name"], r["orphan"]) for r in rows] == [("p1", True), ("keep", False)]
    keep_uid = rows[1]["uid"]
    assert client.delete(f"/api/v1/snapshots/{keep_uid}").status_code == 409
    assert client.post(f"/api/v1/snapshots/{keep_uid}/restore", json={}).status_code == 409

    # A deleted profile's snapshots can still be looked through, by uid.
    first = client.get(f"/api/v1/snapshots/{uid}").json()["snapshots"][0]["id"]
    assert client.get("/api/v1/snapshots/not-a-uid").status_code == 404
    pathlib.Path("camoufox_data").mkdir(exist_ok=True)

    # Put back under another name: same settings, same uid, its data.
    body = {"name": "p1-back", "snap_id": first}
    r = client.post(f"/api/v1/snapshots/{uid}/restore", json=body)
    assert r.status_code == 200 and r.json() == {"name": "p1-back"}
    back = pm.profiles["p1-back"]
    assert back.uid == uid and back.proxy == "http://198.51.100.7:8080"
    assert pathlib.Path("camoufox_data/p1-back/cookies.sqlite").read_bytes() == b"session"
    assert not any(r["orphan"] for r in client.get("/api/v1/snapshots").json()["profiles"])

    client.delete("/api/v1/profiles/p1-back")
    assert client.delete(f"/api/v1/snapshots/{uid}").json() == {"deleted": 1}
    assert [r["name"] for r in client.get("/api/v1/snapshots").json()["profiles"]] == ["keep"]


def test_off_without_a_bucket(client, monkeypatch):
    client, _ = client
    monkeypatch.setattr(config, "SNAPSHOT_S3_BUCKET", "")
    assert client.get("/api/v1/info").json()["snapshots"] is False
    assert client.get("/api/v1/profiles/p1/snapshots").status_code == 404
    assert client.get("/api/v1/snapshots").status_code == 404
