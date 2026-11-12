import pytest
from fastapi.testclient import TestClient

from src.api import app as app_module
from src.api import auth
from src.api.app import create_app
from src.core.container import Container
from src.services.profile.reports import ReportConflict, ReportStore
from tests import test_hub
from tests.test_hub import _login

hub = test_hub.hub


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auth, "API_TOKEN", "")
    monkeypatch.setattr(app_module, "API_TOKEN", "")
    container = Container(hub=False)
    client = TestClient(create_app(container), base_url="http://127.0.0.1")
    assert client.post("/api/v1/profiles", json={"name": "p1"}).status_code == 201
    return client, container


def payload(letter="a", **fields):
    return {
        "request_id": letter * 32,
        "platform": "facebook",
        "status": "suspendido",
        "note": "Checkpoint",
        **fields,
    }


def test_report_api_validates_account_and_follows_profile_identity(client):
    client, container = client
    base = "/api/v1/profiles/p1/account-reports"
    account = client.post(
        "/api/v1/profiles/p1/accounts",
        json={"platform": "facebook", "username": "fox", "password": "secret"},
    ).json()
    assert client.post(base, json=payload()).status_code == 400
    assert client.post(base, json=payload(account_id="other")).status_code == 400
    assert client.post(base, json=payload(platform="myspace")).status_code == 422
    assert client.post(base, json=payload(note="x" * 1001)).status_code == 422
    assert (
        client.post(base, json=payload(account_id=account["id"], reported_by="admin")).status_code
        == 422
    )
    assert client.post(base, json=payload(account_id=account["id"])).status_code == 200
    report = client.get("/api/v1/account-reports").json()[0]
    assert report["profile"] == "p1" and report["username"] == "fox"
    assert "password" not in report and "secret" not in str(report)
    assert client.post(base + "/" + report["id"] + "/review").status_code == 200
    pm = container.profile_manager
    pm.update_profile("p1", "renamed", "", "windows")
    assert client.get("/api/v1/account-reports").json()[0]["profile"] == "renamed"
    pm.delete_profile("renamed")
    assert client.get("/api/v1/account-reports").json() == []


def test_hub_reports_respect_visibility_and_agent_lease(hub):
    client, container, admin = hub
    ana = _login(client, "ana", "anaanaanaana")
    bob = _login(client, "bob", "bobbobbobbob")
    client.post("/api/v1/profiles", json={"name": "shop"}, headers=ana)
    base = "/api/v1/profiles/shop/account-reports"
    agent_key = client.post("/api/v1/hub/me/agent-key", headers=ana).json()["key"]
    agent = {"Authorization": "Bearer " + agent_key}
    assert client.post(base, json=payload(), headers=agent).status_code == 403
    container.browser_launcher.touch("ana", set())
    container.browser_launcher.dispatch("ana", container.profile_manager.profiles["shop"])
    assert client.post(base, json=payload(), headers=agent).status_code == 200
    assert client.get("/api/v1/account-reports", headers=bob).json() == []
    assert client.get("/api/v1/account-reports", headers=agent).status_code == 403
    report = client.get("/api/v1/account-reports", headers=admin).json()[0]
    assert report["reported_by"] == "ana"
    review = base + "/" + report["id"] + "/review"
    assert client.post(review, headers=bob).status_code == 404
    assert client.post(review, headers=agent).status_code == 403
    assert client.post(review, headers=admin).status_code == 200
    assert client.post(base, json=payload("b"), headers=ana).status_code == 200
    assert client.post(review, headers=admin).status_code == 409
    container.browser_launcher.release("shop", "ana")
    assert client.post(base, json=payload("c"), headers=agent).status_code == 403


def test_extension_bridge_agent_hub_round_trip(hub, monkeypatch):
    from src.agent import Agent, HubClient
    from src.services.browser.reporting import ReportBridge
    from tests.test_reporting_bridge import send

    client, container, admin = hub
    ana = _login(client, "ana", "anaanaanaana")
    client.post("/api/v1/profiles", json={"name": "agent-profile"}, headers=ana)
    key = client.post("/api/v1/hub/me/agent-key", headers=ana).json()["key"]
    profile = container.profile_manager.profiles["agent-profile"]
    launcher = container.browser_launcher
    launcher.touch("ana", set())
    launcher.dispatch("ana", profile)
    agent = Agent.__new__(Agent)
    agent.hub = HubClient("https://hub.example", "ana", key)

    def transport(method, path, data, **kwargs):
        response = client.request(
            method,
            "/api/v1" + path,
            content=data,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        )
        assert response.status_code == 200, response.text
        return response.json()

    monkeypatch.setattr(agent.hub, "_send", transport)
    bridge = ReportBridge(lambda body: agent._account_report(profile, body))
    try:
        assert send(bridge, payload())[0] == 200
        report = client.get("/api/v1/account-reports", headers=admin).json()[0]
        assert report["profile"] == "agent-profile" and report["reported_by"] == "ana"
    finally:
        bridge.close()


def test_latest_report_persists_and_retry_does_not_reopen_review(tmp_path):
    path = tmp_path / "reports.json"
    store = ReportStore(path)
    body = {
        "request_id": "a" * 32,
        "platform": "facebook",
        "account_id": None,
        "status": "suspendido",
        "note": "Checkpoint",
    }
    store.submit("profile-uid", body, "ana", "")
    report = ReportStore(path).list("profile-uid")[0]
    assert report["status"] == "suspendido" and report["reported_by"] == "ana"
    store.review("profile-uid", report["id"], "accounts")
    store.submit("profile-uid", body, "ana", "")
    assert store.list("profile-uid")[0]["reviewed_by"] == "accounts"
    newer = {**body, "request_id": "b" * 32, "status": "videoselfie", "note": ""}
    store.submit("profile-uid", newer, "ana", "")
    store.submit("profile-uid", body, "ana", "")
    assert store.list("profile-uid")[0]["id"] == "b" * 32
    assert store.list("profile-uid")[0]["reviewed_at"] is None
    with pytest.raises(ReportConflict):
        store.review("profile-uid", report["id"], "accounts")
    with pytest.raises(ReportConflict):
        store.submit("profile-uid", {**body, "note": "changed"}, "ana", "")
    assert store.list("other-uid") == []
    store.forget_profile("profile-uid")
    assert ReportStore(path).list("profile-uid") == []


def test_failed_save_does_not_acknowledge_or_change_memory(tmp_path, monkeypatch):
    store = ReportStore(tmp_path / "reports.json")
    monkeypatch.setattr(store, "_save", lambda data: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        store.submit(
            "uid",
            {
                "request_id": "a" * 32,
                "platform": "x",
                "account_id": None,
                "status": "suspendido",
                "note": "",
            },
            "",
            "",
        )
    assert store.list("uid") == []
