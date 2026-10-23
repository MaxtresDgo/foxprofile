"""Site accounts per profile: the encrypted store, 2FA codes, the REST routes and
filling a login form."""

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from src.api import app as app_module
from src.api import auth
from src.api.app import create_app
from src.core import config
from src.core.container import Container
from src.services.browser import login as login_module
from src.services.profile.accounts import AccountsLocked, AccountStore, totp, valid_totp_secret
from src.services.profile.manager import ProfileManager
from tests import test_hub
from tests.test_hub import _login

hub = test_hub.hub  # the hub fixture

# RFC 6238's SHA-1 seed ("12345678901234567890") in base32.
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def test_totp_matches_rfc_6238():
    assert totp(RFC_SECRET, 59) == ("287082", 1)
    assert totp(RFC_SECRET, 1111111109)[0] == "081804"
    # Spaces and lower case, as sites show the key, are the same secret.
    assert totp("gezd gnbv gy3t qojq gezd gnbv gy3t qojq", 59)[0] == "287082"
    assert valid_totp_secret(RFC_SECRET) and not valid_totp_secret("not base32!")


def test_store_is_encrypted_and_locks_on_a_wrong_key(tmp_path):
    path = tmp_path / "accounts.json"
    store = AccountStore(str(path), "")
    a = store.add("p1", {"platform": "x", "username": "fox@example.com", "password": "hunter2"})
    raw = path.read_bytes()
    assert b"fox@example.com" not in raw and b"hunter2" not in raw
    assert (tmp_path / "accounts.key").exists()
    assert AccountStore(str(path), "").get("p1", a["id"])["password"] == "hunter2"

    other = AccountStore(str(path), Fernet.generate_key().decode())
    with pytest.raises(AccountsLocked):
        other.list("p1")
    with pytest.raises(AccountsLocked):
        other.add("p1", {"platform": "x", "username": "u"})
    assert path.read_bytes() == raw  # a locked store never writes


def test_accounts_follow_rename_and_delete(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pm = ProfileManager()
    pm.add_profile("a", "", "windows")
    pm.accounts.add("a", {"platform": "gmail", "username": "a@example.com"})
    pm.update_profile("a", "b", "", "windows")
    assert pm.accounts.list("a") == [] and len(pm.accounts.list("b")) == 1
    pm.delete_profile("b")
    assert pm.accounts.list("b") == []


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # A token in the developer's .env must not turn these into 401s.
    monkeypatch.setattr(auth, "API_TOKEN", "")
    monkeypatch.setattr(app_module, "API_TOKEN", "")
    container = Container()
    client = TestClient(create_app(container), base_url="http://127.0.0.1")
    client.post("/api/v1/profiles", json={"name": "p1"})
    return client, container


def test_api_hides_secrets_and_patches_only_given_fields(client):
    client, _ = client
    base = "/api/v1/profiles/p1/accounts"
    r = client.post(
        base,
        json={
            "platform": "instagram",
            "username": " fox ",
            "password": "pw",
            "totp_secret": RFC_SECRET.lower(),
        },
    )
    assert r.status_code == 201
    acc = r.json()
    assert acc == {
        "id": acc["id"],
        "platform": "instagram",
        "username": "fox",
        "has_password": True,
        "has_totp": True,
    }
    assert "pw" not in client.get(base).text
    assert client.get(f"{base}/{acc['id']}/password").json() == {"password": "pw"}
    code = client.get(f"{base}/{acc['id']}/totp").json()
    assert code["code"] == totp(RFC_SECRET)[0] and 0 < code["remaining"] <= 30

    # Omitted fields stay; "" clears.
    r = client.patch(f"{base}/{acc['id']}", json={"username": "fox2", "totp_secret": ""})
    assert r.json()["username"] == "fox2" and r.json()["has_password"] and not r.json()["has_totp"]
    assert client.get(f"{base}/{acc['id']}/totp").status_code == 404

    assert client.post(base, json={"platform": "myspace", "username": "u"}).status_code == 400
    assert (
        client.post(base, json={"platform": "x", "username": "u", "totp_secret": "1!"}).status_code
        == 400
    )
    assert client.get("/api/v1/profiles/nope/accounts").status_code == 404
    assert client.delete(f"{base}/{acc['id']}").status_code == 200
    assert client.get(base).json() == []


def test_login_route_needs_a_running_profile(client):
    client, _ = client
    acc = client.post(
        "/api/v1/profiles/p1/accounts", json={"platform": "x", "username": "u"}
    ).json()
    r = client.post(f"/api/v1/profiles/p1/accounts/{acc['id']}/login")
    assert r.status_code == 409


def test_hub_accounts_are_private_and_closed_to_personal_keys(hub, monkeypatch):
    client, _, admin = hub
    ana, bob = _login(client, "ana", "anaanaanaana"), _login(client, "bob", "bobbobbobbob")
    client.post("/api/v1/profiles", json={"name": "shop-01"}, headers=ana)
    base = "/api/v1/profiles/shop-01/accounts"
    assert (
        client.post(base, json={"platform": "tiktok", "username": "u"}, headers=ana).status_code
        == 201
    )
    assert len(client.get(base, headers=admin).json()) == 1
    assert client.get(base, headers=bob).status_code == 404
    # A group's members see its profiles' accounts.
    client.post("/api/v1/hub/groups", json={"name": "team"}, headers=admin)
    client.put("/api/v1/hub/groups/team/members/bob", json={"role": "member"}, headers=admin)
    client.put("/api/v1/hub/profile-group/shop-01", json={"group": "team"}, headers=admin)
    assert len(client.get(base, headers=bob).json()) == 1
    key = client.post("/api/v1/hub/me/key", headers=ana).json()["key"]
    assert client.get(base, headers={"Authorization": "Bearer " + key}).status_code == 403
    # The summary for the profile list: only what the caller sees, never to personal keys.
    client.post("/api/v1/profiles", json={"name": "ana-only"}, headers=ana)
    client.post(
        "/api/v1/profiles/ana-only/accounts", json={"platform": "x", "username": "a"}, headers=ana
    )
    summary = client.get("/api/v1/accounts", headers=bob).json()
    assert list(summary) == ["shop-01"] and summary["shop-01"][0]["platform"] == "tiktok"
    assert "password" not in summary["shop-01"][0]
    assert sorted(client.get("/api/v1/accounts", headers=ana).json()) == ["ana-only", "shop-01"]
    assert (
        client.get("/api/v1/accounts", headers={"Authorization": "Bearer " + key}).status_code
        == 403
    )
    # Army's key reads the summary (no secrets) for its panel, and nothing else.
    monkeypatch.setattr(config, "ARMY_CALLBACK", "https://army.example.com/fox/callback")
    army = {
        "Authorization": "Bearer "
        + client.post("/api/v1/hub/me/army-key", headers=bob).json()["key"]
    }
    assert list(client.get("/api/v1/accounts", headers=army).json()) == ["shop-01"]
    assert client.get(base, headers=army).status_code == 403
    acc = summary["shop-01"][0]["id"]
    assert client.get(f"{base}/{acc}/password", headers=army).status_code == 403
    agent = client.post("/api/v1/hub/me/agent-key", headers=bob).json()["key"]
    assert (
        client.get("/api/v1/accounts", headers={"Authorization": "Bearer " + agent}).status_code
        == 403
    )


class FakeBrowser:
    """The page control: `lands_on` is where navigating leaves the page, `later`
    where it is after waiting (a site that redirects once loaded)."""

    def __init__(self, missing=(), lands_on="", later=None):
        self.calls, self.missing = [], set(missing)
        self.lands_on, self.later = lands_on, later

    def control(self, name, action, params):
        self.calls.append((action, params))
        if action == "tab_new":
            return {"tabs": [{"index": 0, "active": False}, {"index": 1, "active": True}]}
        if action == "navigate":
            self.url = self.lands_on or params["url"]
            return {"url": self.url, "title": ""}
        if action == "evaluate":
            return {"result": self.later or self.url}
        if params.get("selector") in self.missing:
            raise RuntimeError("timeout")
        return {}


def test_login_fills_fields_in_order_and_the_code_only_when_asked():
    recipe = login_module.RECIPES["x"]
    account = {"platform": "x", "username": "fox", "password": "pw", "totp_secret": RFC_SECRET}
    bl = FakeBrowser()
    assert login_module.login(bl, "p1", account, clock=lambda: 59.0 - 20) == {
        "ok": True,
        "error": None,
        "signed_in": False,
    }
    typed = [(p["selector"], p["text"], p["submit"]) for a, p in bl.calls if a == "type"]
    vis = login_module.visible
    assert typed == [
        (vis(recipe["fields"][0][0]), "fox", True),
        (vis(recipe["fields"][1][0]), "pw", True),
        (vis(recipe["otp"]), totp(RFC_SECRET, 39)[0], True),
    ]
    # In a new tab, so the profile's own tabs stay as they were.
    assert bl.calls[0] == ("tab_new", {"url": None})
    assert bl.calls[1] == ("navigate", {"url": recipe["url"], "wait_until": "load"})
    assert "tab_close" not in [a for a, _ in bl.calls]  # the login tab stays

    # Near the end of a window it waits for the next code.
    slept = []
    bl = FakeBrowser()
    login_module.login(bl, "p1", account, sleep=slept.append, clock=lambda: 58.0)
    assert slept == [2] and bl.calls[-1][1]["text"] == totp(RFC_SECRET, 58.0)[0]

    # No 2FA prompt: done without a code.
    bl = FakeBrowser(missing={login_module.visible(recipe["otp"])})
    assert login_module.login(bl, "p1", account)["ok"]
    assert [a for a, _ in bl.calls].count("type") == 2

    # A missing field ends it with an error naming the field.
    bl = FakeBrowser(missing={login_module.visible(recipe["fields"][1][0])})
    result = login_module.login(bl, "p1", account)
    assert not result["ok"] and "Password" in result["error"]


def test_login_leaves_a_signed_in_profile_alone():
    account = {"platform": "facebook", "username": "fox", "password": "pw", "totp_secret": ""}
    first = login_module.visible(login_module.RECIPES["facebook"]["fields"][0][0])
    # Sent away from the login page right away: nothing typed, no wait, and the
    # tab it opened is closed again.
    bl = FakeBrowser(lands_on="https://www.facebook.com/")
    assert login_module.login(bl, "p1", account) == {"ok": True, "error": None, "signed_in": True}
    assert bl.calls == [
        ("tab_new", {"url": None}),
        ("navigate", {"url": login_module.RECIPES["facebook"]["url"], "wait_until": "load"}),
        ("tab_close", {"index": 1}),
    ]
    # X loads its login page, then moves a signed-in profile to /home.
    x = {**account, "platform": "x"}
    x_first = login_module.visible(login_module.RECIPES["x"]["fields"][0][0])
    bl = FakeBrowser(missing={x_first}, later="https://x.com/home")
    assert login_module.login(bl, "p1", x)["signed_in"]
    assert "type" not in [a for a, _ in bl.calls]
    # Still on the login page without its form: an error, not "signed in".
    bl = FakeBrowser(missing={first})
    result = login_module.login(bl, "p1", account)
    assert not result["ok"] and not result["signed_in"] and "Username" in result["error"]
    # Google's pages on accounts.google.com (chooser, challenge) are not signed in yet.
    google = login_module.RECIPES["gmail"]
    assert login_module.on_login_page(
        google, "https://accounts.google.com/v3/signin/accountchooser"
    )
    assert not login_module.on_login_page(google, "https://myaccount.google.com/")
