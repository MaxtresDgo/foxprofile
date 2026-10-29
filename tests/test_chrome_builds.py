"""Chrome engine builds: the pins, a profile's build, installing one, and launches waiting for it."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from chrome_engine import fetch, release
from chrome_engine import persona as personas
from src.api import app as app_module
from src.api import auth
from src.api.app import create_app
from src.core.container import Container
from src.hub import platform_auth
from src.hub.store import HubStore
from src.services.browser import chrome_prefetch
from src.services.profile.manager import ProfileManager

OLD, NEW = "139.0.7258.154", "148.0.7778.215"
SECRET = "s" * 32


def test_pins_stay_within_the_limit_and_are_complete():
    assert 1 <= len(release.RELEASES) <= release.MAX_BUILDS
    assert release.DEFAULT_VERSION in release.RELEASES
    assert release.LEGACY_VERSION in release.RELEASES
    for version, rel in release.RELEASES.items():
        assert rel.assets, version
        for asset in rel.assets.values():
            assert re.fullmatch(r"[0-9a-f]{64}", asset.sha256), asset.url
            assert asset.size > 50_000_000, asset.url
            assert f"/download/{version}/" in asset.url


def test_versions_sort_numerically():
    assert release.key("139.0.7258.154") < release.key("148.0.7778.215")
    assert release.key("99.0.1.2") < release.key("100.0.0.0")
    mac = release.available("macos")
    assert mac == sorted(mac, key=release.key, reverse=True)
    assert all("macos" in release.RELEASES[v].assets for v in mac)
    assert release.available(None) == []


def test_installed_versions_reads_the_markers(tmp_path):
    exe = tmp_path / OLD / "chrome"
    exe.parent.mkdir()
    exe.write_text("")
    (tmp_path / OLD / fetch.MARKER).write_text(json.dumps({"executable": "chrome"}))
    (tmp_path / NEW).mkdir()  # no marker: an unfinished install
    assert fetch.installed_versions(tmp_path) == [OLD]


def test_android_user_agent_follows_the_build(tmp_path):
    persona = personas.generate("android", seed=7, version=OLD)
    assert "Chrome/139.0.0.0" in persona.user_agent
    personas.save(tmp_path, persona)
    assert personas.retarget(tmp_path, NEW)
    assert "Chrome/148.0.0.0" in personas.load(tmp_path).user_agent
    # A full version (an imported phone) moves to the full new one.
    personas.save(
        tmp_path,
        personas.generate(
            "android", seed=7, user_agent=persona.user_agent.replace("139.0.0.0", "139.0.7258.154")
        ),
    )
    personas.retarget(tmp_path, NEW)
    assert f"Chrome/{NEW} " in personas.load(tmp_path).user_agent
    # Desktop personas have no User-Agent of their own.
    personas.save(tmp_path, personas.generate("windows", seed=7))
    assert not personas.retarget(tmp_path, NEW)


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auth, "API_TOKEN", "")
    monkeypatch.setattr(app_module, "API_TOKEN", "")
    monkeypatch.setattr(chrome_prefetch, "host", lambda: "macos")
    monkeypatch.setattr(chrome_prefetch, "start", lambda *a, **k: True)
    installed = []
    monkeypatch.setattr(fetch, "installed_versions", lambda *a: list(installed))
    container = Container()
    return TestClient(create_app(container), base_url="http://127.0.0.1"), container, installed


def test_profiles_get_the_default_build_and_old_ones_the_legacy_one(local, tmp_path):
    client, _, _ = local
    r = client.post("/api/v1/profiles", json={"name": "c1", "engine": "chrome"})
    assert r.json()["chrome_version"] == release.DEFAULT_VERSION
    assert client.post("/api/v1/profiles", json={"name": "f1"}).json()["chrome_version"] is None
    (tmp_path / "profiles.json").write_text(
        json.dumps({"old": {"name": "old", "os_type": "macos", "engine": "chrome"}})
    )
    assert ProfileManager().profiles["old"].chrome_version == release.LEGACY_VERSION


def test_create_takes_only_builds_this_machine_can_run(local):
    client, _, _ = local
    r = client.post(
        "/api/v1/profiles", json={"name": "c1", "engine": "chrome", "chrome_version": OLD}
    )
    assert r.status_code == 201 and r.json()["chrome_version"] == OLD
    r = client.post(
        "/api/v1/profiles", json={"name": "c2", "engine": "chrome", "chrome_version": "1.2.3.4"}
    )
    assert r.status_code == 400
    # 150 has no macOS build.
    r = client.post(
        "/api/v1/profiles",
        json={"name": "c3", "engine": "chrome", "chrome_version": "150.0.7871.186"},
    )
    assert r.status_code == 400


def test_a_profile_only_moves_to_a_newer_build(local, monkeypatch):
    client, container, _ = local
    client.post("/api/v1/profiles", json={"name": "c1", "engine": "chrome", "chrome_version": OLD})
    r = client.patch("/api/v1/profiles/c1", json={"chrome_version": NEW})
    assert r.status_code == 200 and r.json()["chrome_version"] == NEW
    r = client.patch("/api/v1/profiles/c1", json={"chrome_version": OLD})
    assert r.status_code == 400
    assert client.get("/api/v1/profiles/c1").json()["chrome_version"] == NEW
    # The same build again is no change.
    assert client.patch("/api/v1/profiles/c1", json={"chrome_version": NEW}).status_code == 200

    client.post("/api/v1/profiles", json={"name": "c2", "engine": "chrome", "chrome_version": OLD})
    monkeypatch.setattr(container.browser_launcher, "is_running", lambda name: name == "c2")
    assert client.patch("/api/v1/profiles/c2", json={"chrome_version": NEW}).status_code == 409
    client.post("/api/v1/profiles", json={"name": "f1"})
    assert client.patch("/api/v1/profiles/f1", json={"chrome_version": NEW}).status_code == 400


def test_launch_waits_for_the_build_and_install_starts_it(local, monkeypatch):
    client, container, installed = local
    client.post("/api/v1/profiles", json={"name": "c1", "engine": "chrome", "chrome_version": OLD})
    r = client.post("/api/v1/browser/c1/launch?wait=false")
    assert r.status_code == 409 and OLD in r.json()["detail"]

    builds = client.get("/api/v1/chrome/builds").json()
    assert builds["online"] and builds["platform"] == "macos"
    old = next(b for b in builds["builds"] if b["version"] == OLD)
    assert old == {
        "version": OLD,
        "platforms": sorted(release.RELEASES[OLD].assets),
        "available": True,
        "installed": False,
        "error": None,
    }

    started = []
    monkeypatch.setattr(chrome_prefetch, "start", lambda v, **k: started.append(v) or True)
    assert client.post(f"/api/v1/chrome/builds/{OLD}/install").status_code == 202
    assert started == [OLD]
    assert client.post("/api/v1/chrome/builds/150.0.7871.186/install").status_code == 400

    installed.append(OLD)
    monkeypatch.setattr(container.browser_launcher, "start_thread", lambda *a, **k: True)
    assert client.post("/api/v1/browser/c1/launch?wait=false").status_code == 202


def _platform_login(email, password):
    return {"email": email, "name": "", "permisos": ["foxprofile:access"]}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auth, "API_TOKEN", SECRET)
    monkeypatch.setattr(app_module, "API_TOKEN", SECRET)
    monkeypatch.setattr(platform_auth, "authenticate", _platform_login)
    container = Container(hub=True)
    container._instances["hub"] = HubStore("hub.json", SECRET)
    client = TestClient(create_app(container))
    r = client.post("/api/v1/hub/login", json={"email": "ana", "password": "x" * 12})
    return client, container, {"Authorization": "Bearer " + r.json()["token"]}


def test_hub_asks_the_agent_which_builds_it_has(hub):
    client, container, ana = hub
    bl = container.browser_launcher
    r = client.post(
        "/api/v1/profiles",
        json={"name": "c1", "engine": "chrome", "chrome_version": OLD},
        headers=ana,
    )
    assert r.status_code == 201  # the hub itself has no build: the agent's PC runs it

    # No agent yet: nothing to install on, nothing opens.
    assert client.get("/api/v1/chrome/builds", headers=ana).json()["online"] is False
    assert client.post(f"/api/v1/chrome/builds/{OLD}/install", headers=ana).status_code == 400
    assert client.post("/api/v1/browser/c1/launch?wait=false", headers=ana).status_code == 409

    # The agent polls with its platform and builds (an idle poll answers 204).
    bl.agents["ana"] = {"os": "macos", "builds": [NEW]}
    bl.touch("ana", set())
    builds = client.get("/api/v1/chrome/builds", headers=ana).json()
    assert builds["online"] and builds["platform"] == "macos"
    assert client.post("/api/v1/browser/c1/launch?wait=false", headers=ana).status_code == 409
    assert client.post(f"/api/v1/chrome/builds/{OLD}/install", headers=ana).status_code == 202
    assert bl.next_command("ana") == {"op": "chrome_install", "version": OLD}

    bl.agents["ana"]["activity"] = {"state": "downloading", "build": OLD, "pct": 40}
    assert client.get("/api/v1/chrome/builds", headers=ana).json()["installing"] == [
        {"version": OLD, "pct": 40}
    ]
    bl.agents["ana"]["builds"] = [OLD, NEW]
    assert client.post("/api/v1/browser/c1/launch?wait=false", headers=ana).status_code == 202
    assert bl.next_command("ana")["op"] == "launch"


def test_hub_poll_records_the_agents_builds(hub):
    client, container, ana = hub
    bl = container.browser_launcher
    bl.update_now.discard("ana")
    client.get(f"/api/v1/hub/poll?chrome_os=linux-x64&chrome_builds={OLD},{NEW}", headers=ana)
    assert bl.agents["ana"]["os"] == "linux-x64"
    assert bl.agents["ana"]["builds"] == [OLD, NEW]
