"""Hub mode: operator sessions, per-user profile visibility and the agent protocol."""

import contextlib
import io
import json
import pathlib
import zipfile

import pytest
from fastapi.testclient import TestClient

from src.api import app as app_module
from src.api import auth
from src.api.app import create_app
from src.core.container import Container
from src.hub.store import HubStore
from src.services.browser.launcher import BrowserControlError

SECRET = "s" * 32


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auth, "API_TOKEN", SECRET)
    monkeypatch.setattr(app_module, "API_TOKEN", SECRET)
    container = Container(hub=True)
    container._instances["hub"] = HubStore("hub.json", SECRET, admin_password="admin-pass")
    client = TestClient(create_app(container))
    admin = _login(client, "admin", "admin-pass")
    for name in ("ana", "bob"):
        r = client.post(
            "/api/v1/hub/users", json={"name": name, "password": name * 4}, headers=admin
        )
        assert r.status_code == 201
    return client, container, admin


def _login(client, user, password):
    r = client.post("/api/v1/hub/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _zip(name, files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("profile.json", json.dumps({"name": name}))
        for path, content in files.items():
            z.writestr(path, content)
    return buf.getvalue()


def test_sessions(hub):
    client, container, admin = hub
    assert (
        client.post("/api/v1/hub/login", json={"username": "ana", "password": "nope"}).status_code
        == 401
    )
    assert client.get("/api/v1/profiles").status_code == 401
    ana = _login(client, "ana", "anaanaanaana")
    assert client.get("/api/v1/hub/me", headers=ana).json()["role"] == "operator"
    forged = {"Authorization": ana["Authorization"].replace("ana.", "bob.", 1)}
    assert client.get("/api/v1/profiles", headers=forged).status_code == 401
    assert client.get("/api/v1/hub/users", headers=ana).status_code == 403
    # A new password logs the old session out.
    client.patch("/api/v1/hub/users/ana", json={"password": "new-password"}, headers=admin)
    assert client.get("/api/v1/profiles", headers=ana).status_code == 401
    # The raw server token still works as an admin key.
    raw = {"Authorization": "Bearer " + SECRET}
    assert client.get("/api/v1/hub/users", headers=raw).status_code == 200


def test_profiles_are_private_to_their_owner(hub):
    client, _, admin = hub
    ana, bob = _login(client, "ana", "anaanaanaana"), _login(client, "bob", "bobbobbobbob")
    assert client.post("/api/v1/profiles", json={"name": "shop-01"}, headers=ana).status_code == 201

    def names(h):
        return [p["name"] for p in client.get("/api/v1/profiles", headers=h).json()["profiles"]]

    assert names(ana) == ["shop-01"]
    assert names(bob) == []
    assert names(admin) == ["shop-01"]
    assert client.get("/api/v1/profiles/shop-01", headers=bob).status_code == 404
    assert client.post("/api/v1/browser/shop-01/launch", headers=bob).status_code == 404
    # Reassign to bob.
    client.put("/api/v1/hub/owners/shop-01", json={"user": "bob"}, headers=admin)
    assert names(bob) == ["shop-01"] and names(ana) == []
    # Cookie tools run on bob's PC, whose agent is not connected.
    r = client.get("/api/v1/profiles/shop-01/cookies", headers=bob)
    assert r.status_code == 409 and "agent" in r.json()["detail"]


def test_launch_runs_on_the_owners_agent_and_syncs_back(hub):
    client, container, admin = hub
    bl = container.browser_launcher
    ana = _login(client, "ana", "anaanaanaana")
    client.post("/api/v1/profiles", json={"name": "shop-01"}, headers=ana)

    r = client.post("/api/v1/browser/shop-01/launch?wait=false", headers=ana)
    assert r.status_code == 409 and "agent" in r.json()["detail"]

    bl.touch("ana", set())  # ana's agent is connected
    assert client.post("/api/v1/browser/shop-01/launch?wait=false", headers=ana).status_code == 202
    assert (
        client.post("/api/v1/browser/shop-01/launch?wait=false", headers=admin).status_code == 409
    )

    cmd = client.get("/api/v1/hub/poll", headers=ana).json()
    assert cmd["op"] == "launch" and cmd["profile"]["name"] == "shop-01"
    bundle = client.get("/api/v1/hub/bundle/shop-01", headers=ana)
    assert bundle.status_code == 200
    assert "profile.json" in zipfile.ZipFile(io.BytesIO(bundle.content)).namelist()
    client.post("/api/v1/hub/status/shop-01", json={"ok": True}, headers=ana)
    assert client.get("/api/v1/browser", headers=ana).json()["running"] == ["shop-01"]
    assert client.get("/api/v1/hub/me", headers=admin).json()["leases"] == {"shop-01": "ana"}

    # Stop from the panel is forwarded to the agent.
    client.post("/api/v1/browser/shop-01/stop", headers=ana)
    assert client.get("/api/v1/hub/poll", headers=ana).json() == {"op": "stop", "name": "shop-01"}

    upload = _zip("shop-01", {"data/cookies.sqlite": b"session", "data/cache2/junk": b"x"})
    r = client.put("/api/v1/hub/bundle/shop-01?release=1", content=upload, headers=ana)
    assert r.status_code == 200, r.text
    assert client.get("/api/v1/browser", headers=ana).json()["running"] == []
    data = pathlib.Path("camoufox_data/shop-01")
    assert (data / "cookies.sqlite").read_bytes() == b"session"

    # The next download leaves the cache behind.
    bl.touch("ana", set())
    client.post("/api/v1/browser/shop-01/launch?wait=false", headers=ana)
    names = zipfile.ZipFile(
        io.BytesIO(client.get("/api/v1/hub/bundle/shop-01", headers=ana).content)
    ).namelist()
    assert "data/cookies.sqlite" in names and not any("cache2" in n for n in names)


def test_upload_rejects_unsafe_or_foreign_archives(hub):
    client, container, admin = hub
    ana = _login(client, "ana", "anaanaanaana")
    client.post("/api/v1/profiles", json={"name": "shop-01"}, headers=ana)
    evil = _zip("shop-01", {"data/../../evil": b"x"})
    assert client.put("/api/v1/hub/bundle/shop-01", content=evil, headers=ana).status_code == 400
    other = _zip("shop-02", {"data/a": b"x"})
    assert client.put("/api/v1/hub/bundle/shop-01", content=other, headers=ana).status_code == 400
    assert not pathlib.Path("evil").exists()
    # Leased to someone else: refused.
    container.browser_launcher.touch("admin", set())
    client.post("/api/v1/browser/shop-01/launch?wait=false", headers=admin)
    ok = _zip("shop-01", {"data/a": b"x"})
    assert client.put("/api/v1/hub/bundle/shop-01", content=ok, headers=ana).status_code == 409


def test_stop_with_agent_offline_frees_the_profile(hub, monkeypatch):
    client, container, _ = hub
    bl = container.browser_launcher
    ana = _login(client, "ana", "anaanaanaana")
    client.post("/api/v1/profiles", json={"name": "shop-01"}, headers=ana)
    bl.touch("ana", set())
    client.post("/api/v1/browser/shop-01/launch?wait=false", headers=ana)
    monkeypatch.setattr(bl, "online", lambda user: False)
    client.post("/api/v1/browser/shop-01/stop", headers=ana)
    assert client.get("/api/v1/browser", headers=ana).json()["running"] == []


def _fake_agent(client, headers, reply):
    """Answer the next hub command through the HTTP API, like src/agent.py does."""
    import threading
    import time

    def run():
        for _ in range(100):
            r = client.get("/api/v1/hub/poll", headers=headers)
            if r.status_code == 200 and "id" in r.json():
                cmd = r.json()
                client.post(f"/api/v1/hub/result/{cmd['id']}", json=reply(cmd), headers=headers)
                return
            time.sleep(0.05)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_page_actions_relay_to_the_agent_holding_the_profile(hub):
    client, container, admin = hub
    bl = container.browser_launcher
    ana = _login(client, "ana", "anaanaanaana")
    client.post("/api/v1/profiles", json={"name": "shop-01"}, headers=ana)
    assert client.get("/api/v1/browser/shop-01/page/tabs", headers=ana).status_code == 409

    bl.touch("ana", {"shop-01"})  # agent reports it running
    seen = {}

    def reply(cmd):
        seen.update(cmd)
        return {"ok": True, "result": {"tabs": [{"index": 0, "url": "about:blank"}]}}

    _fake_agent(client, ana, reply)
    # The admin's viewer relays to ana's PC too.
    r = client.get("/api/v1/browser/shop-01/page/tabs", headers=admin)
    assert r.status_code == 200 and r.json()["tabs"][0]["url"] == "about:blank"
    assert seen["op"] == "control" and seen["action"] == "tabs" and seen["name"] == "shop-01"

    _fake_agent(client, ana, lambda cmd: {"ok": False, "error": "Timeout 10000ms", "status": 408})
    r = client.post("/api/v1/browser/shop-01/page/click", json={"selector": "#x"}, headers=ana)
    assert r.status_code == 408


def test_answers_only_count_from_the_agent_that_was_asked(hub):
    client, container, _ = hub
    bl = container.browser_launcher
    bob = _login(client, "bob", "bobbobbobbob")
    bl.touch("ana", set())
    request_id = None

    def ask():
        with contextlib.suppress(BrowserControlError):
            bl._call("ana", {"op": "control"}, timeout=1)

    import threading

    t = threading.Thread(target=ask)
    t.start()
    for _ in range(50):
        if bl._pending:
            request_id = next(iter(bl._pending))
            break
        threading.Event().wait(0.02)
    r = client.post(f"/api/v1/hub/result/{request_id}", json={"ok": True}, headers=bob)
    assert r.status_code == 404
    t.join()


def test_cookies_run_on_the_callers_pc(hub):
    client, container, _ = hub
    bl = container.browser_launcher
    ana = _login(client, "ana", "anaanaanaana")
    client.post("/api/v1/profiles", json={"name": "shop-01"}, headers=ana)
    bl.touch("ana", set())

    def reply(cmd):
        assert cmd["op"] == "cookies" and cmd["action"] == "export"
        assert bl.holder("shop-01") == "ana"  # leased while the agent works on it
        return {"ok": True, "result": {"text": "[]", "count": 0}}

    _fake_agent(client, ana, reply)
    r = client.get("/api/v1/profiles/shop-01/cookies?format=json", headers=ana)
    assert r.status_code == 200 and r.headers["X-Cookie-Count"] == "0"
    assert bl.holder("shop-01") is None

    _fake_agent(client, ana, lambda cmd: {"ok": False, "error": "No valid cookies", "status": 400})
    r = client.post("/api/v1/profiles/shop-01/cookies", json={"content": "junk"}, headers=ana)
    assert r.json()["success"] is False and r.json()["error"] == "No valid cookies"
    assert bl.holder("shop-01") is None


def test_personal_key_for_mcp(hub):
    client, container, _ = hub
    ana = _login(client, "ana", "anaanaanaana")
    client.post("/api/v1/profiles", json={"name": "shop-01"}, headers=ana)
    key = client.post("/api/v1/hub/me/key", headers=ana).json()["key"]
    with_key = {"Authorization": "Bearer " + key}
    assert [
        p["name"] for p in client.get("/api/v1/profiles", headers=with_key).json()["profiles"]
    ] == ["shop-01"]
    # A new key replaces the old one.
    client.post("/api/v1/hub/me/key", headers=ana)
    assert client.get("/api/v1/profiles", headers=with_key).status_code == 401

    key = client.post("/api/v1/hub/me/key", headers=ana).json()["key"]
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }
    mcp_headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(client.app) as live:
        assert live.post("/mcp", json=init, headers=mcp_headers).status_code == 401
        r = live.post("/mcp", json=init, headers={**mcp_headers, "Authorization": "Bearer " + key})
        assert r.status_code == 200, r.text
