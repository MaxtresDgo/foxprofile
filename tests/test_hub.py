"""Hub mode: operator sessions, per-user profile visibility and the agent protocol."""

import base64
import contextlib
import hashlib
import hmac
import io
import json
import pathlib
import secrets
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from src.api import app as app_module
from src.api import auth
from src.api.app import create_app
from src.core import config
from src.core.container import Container
from src.core.strings import get_string
from src.hub import platform_auth
from src.hub.store import HubStore
from src.services.browser.launcher import BrowserControlError
from src.services.profile.transfer import COOKIES_GZ, COOKIES_SHA, pack_cookies, read_cookies

SECRET = "s" * 32
ARMY_SECRET = "a" * 32
CALLBACK = "https://army.example.com/perfiles/fox/vincular/callback"
# The identity platform: email -> (password, permissions). It only lets in users
# holding the permission it requires for FoxProfile, like the real one.
PLATFORM = {
    "admin@example.com": ("admin-pass", ["*:*"]),
    "ana": ("anaanaanaana", ["foxprofile:access"]),
    "bob": ("bobbobbobbob", ["foxprofile:access"]),
    "cid": ("cidcidcidcid", ["foxprofile:access"]),
    "eve@example.com": ("eve-pass", ["reports:read"]),
}


def _platform_login(email, password):
    entry = PLATFORM.get(email.strip().lower())
    if entry is None or entry[0] != password:
        raise platform_auth.LoginError(401, get_string("err_bad_login"))
    if not platform_auth.has_permission(entry[1], "foxprofile:access"):
        raise platform_auth.LoginError(403, get_string("err_no_access"))
    name = "Ana María López" if email == "ana" else ""
    return {"email": email.strip().lower(), "name": name, "permisos": entry[1]}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auth, "API_TOKEN", SECRET)
    monkeypatch.setattr(app_module, "API_TOKEN", SECRET)
    monkeypatch.setattr(platform_auth, "authenticate", _platform_login)
    container = Container(hub=True)
    container._instances["hub"] = HubStore("hub.json", SECRET)
    client = TestClient(create_app(container))
    admin = _login(client, "admin@example.com", "admin-pass")
    # Accounts appear on first sign-in.
    _login(client, "ana", "anaanaanaana")
    _login(client, "bob", "bobbobbobbob")
    return client, container, admin


def _login(client, user, password):
    r = client.post("/api/v1/hub/login", json={"email": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


@pytest.mark.parametrize(
    "permisos, status, role",
    [
        ([], 403, None),
        (["reports:read"], 403, None),
        (["foxprofile:access"], 200, "operator"),
        (["foxprofile:*"], 200, "admin"),
        (["foxprofile:admin"], 200, "admin"),
    ],
)
def test_login_checks_the_access_permission_itself(hub, monkeypatch, permisos, status, role):
    """A platform that lets everyone through still does not open the hub to them."""
    client, _, _ = hub
    monkeypatch.setattr(
        platform_auth,
        "authenticate",
        lambda email, password: {"email": "zed@example.com", "name": "", "permisos": permisos},
    )
    r = client.post("/api/v1/hub/login", json={"email": "zed@example.com", "password": "x"})
    assert r.status_code == status, r.text
    if role:
        assert r.json()["role"] == role


def _zip(name, files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("profile.json", json.dumps({"name": name}))
        for path, content in files.items():
            z.writestr(path, content)
    return buf.getvalue()


def test_sessions(hub, monkeypatch):
    client, container, admin = hub
    login = "/api/v1/hub/login"
    # Errors follow the caller's Accept-Language (the panel sends its own).
    bad = {"email": "ana", "password": "nope"}
    r = client.post(login, json=bad, headers={"Accept-Language": "es"})
    assert r.status_code == 401 and r.json()["detail"] == "Correo o contraseña incorrectos"
    r = client.get("/api/v1/profiles/nope", headers={**admin, "Accept-Language": "es-MX,es;q=0.9"})
    assert r.json()["detail"] == "No se encontró el profile 'nope'"
    assert client.get("/api/v1/profiles").status_code == 401
    # Platform users without FoxProfile's permission never get an account.
    r = client.post(login, json={"email": "eve@example.com", "password": "eve-pass"})
    assert r.status_code == 403 and "eve@example.com" not in container.hub_store.users
    # The platform's permissions set the role; emails (with dots) work as names.
    me = client.get("/api/v1/hub/me", headers=admin).json()
    assert me["name"] == "admin@example.com" and me["role"] == "admin"
    ana = _login(client, "ana", "anaanaanaana")
    me = client.get("/api/v1/hub/me", headers=ana).json()
    assert me["role"] == "operator" and me["display"] == "Ana María López"
    forged = {"Authorization": ana["Authorization"].replace("ana.", "bob.", 1)}
    assert client.get("/api/v1/profiles", headers=forged).status_code == 401
    assert client.get("/api/v1/hub/users", headers=ana).status_code == 403
    # ...again on every sign-in, and the open session follows the local role.
    monkeypatch.setitem(
        PLATFORM, "ana", ("anaanaanaana", ["foxprofile:access", "foxprofile:admin"])
    )
    _login(client, "ana", "anaanaanaana")
    assert client.get("/api/v1/hub/users", headers=ana).status_code == 200
    # No local passwords: users are not created or edited here, only dropped.
    assert client.post("/api/v1/hub/users", json={"name": "x"}, headers=admin).status_code == 405
    bob = _login(client, "bob", "bobbobbobbob")
    assert client.delete("/api/v1/hub/users/bob", headers=admin).status_code == 200
    assert client.get("/api/v1/profiles", headers=bob).status_code == 401
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
    container.browser_launcher.touch("admin@example.com", set())
    client.post("/api/v1/browser/shop-01/launch?wait=false", headers=admin)
    ok = _zip("shop-01", {"data/a": b"x"})
    assert client.put("/api/v1/hub/bundle/shop-01", content=ok, headers=ana).status_code == 409


def test_bundles_carry_a_sha256_and_damaged_cookies_are_refused(hub, tmp_path):
    client, container, _ = hub
    ana = _login(client, "ana", "anaanaanaana")
    client.post("/api/v1/profiles", json={"name": "shop-01"}, headers=ana)
    pack_cookies(str(tmp_path), '[{"name": "sid"}]')
    gz, sha = (tmp_path / COOKIES_GZ).read_bytes(), (tmp_path / COOKIES_SHA).read_text()
    assert read_cookies(str(tmp_path)) == '[{"name": "sid"}]'

    good = _zip("shop-01", {f"data/{COOKIES_GZ}": gz, f"data/{COOKIES_SHA}": sha})
    put = "/api/v1/hub/bundle/shop-01"
    wrong = {**ana, "X-Content-SHA256": "0" * 64}
    assert client.put(put, content=good, headers=wrong).status_code == 422
    right = {**ana, "X-Content-SHA256": hashlib.sha256(good).hexdigest()}
    assert client.put(put, content=good, headers=right).status_code == 200
    damaged = _zip("shop-01", {f"data/{COOKIES_GZ}": gz[:-1] + b"x", f"data/{COOKIES_SHA}": sha})
    assert client.put(put, content=damaged, headers=ana).status_code == 422
    assert (pathlib.Path("camoufox_data/shop-01") / COOKIES_GZ).read_bytes() == gz

    container.browser_launcher.touch("ana", set())
    client.post("/api/v1/browser/shop-01/launch?wait=false", headers=ana)
    bundle = client.get(put, headers=ana)
    assert bundle.headers["X-Content-SHA256"] == hashlib.sha256(bundle.content).hexdigest()


def test_bundle_uploads_in_pieces_survive_a_retried_piece(hub):
    client, _, _ = hub
    ana = _login(client, "ana", "anaanaanaana")
    client.post("/api/v1/profiles", json={"name": "shop-01"}, headers=ana)
    bundle = _zip("shop-01", {"data/cookies.sqlite": b"session" * 1000})
    part = "/api/v1/hub/bundle/shop-01/part?offset="
    half = len(bundle) // 2
    assert client.put(part + "0", content=bundle[:half], headers=ana).json() == {"size": half}
    assert client.put(part + "0", content=bundle[:half], headers=ana).status_code == 200  # retry
    assert client.put(part + str(half + 1), content=b"x", headers=ana).status_code == 416
    client.put(part + str(half), content=bundle[half:], headers=ana)

    finish = "/api/v1/hub/bundle/shop-01?chunked=1"
    sha = {**ana, "X-Content-SHA256": hashlib.sha256(bundle).hexdigest()}
    assert client.put(finish, headers=sha).status_code == 200
    data = pathlib.Path("camoufox_data/shop-01/cookies.sqlite")
    assert data.read_bytes() == b"session" * 1000
    assert client.put(finish, headers=sha).status_code == 416  # the pieces are used up


def test_late_uploads_come_only_from_whoever_held_the_profile_last(hub):
    client, container, admin = hub
    bl = container.browser_launcher
    ana = _login(client, "ana", "anaanaanaana")
    client.post("/api/v1/profiles", json={"name": "shop-01"}, headers=ana)
    bl.touch("ana", set())
    client.post("/api/v1/browser/shop-01/launch?wait=false", headers=ana)
    client.post("/api/v1/hub/release/shop-01", headers=ana)  # the lease is gone
    put = "/api/v1/hub/bundle/shop-01"
    ok = _zip("shop-01", {"data/a": b"x"})
    # Someone who can see it but never had it open cannot overwrite it...
    assert client.put(put, content=ok, headers=admin).status_code == 409
    assert client.put(put + "/part?offset=0", content=ok, headers=admin).status_code == 409
    # ...nor take it by claiming their agent runs it.
    bl.touch("admin@example.com", {"shop-01"})
    assert bl.holder("shop-01") is None
    assert client.put(put, content=ok, headers=ana).status_code == 200
    bl.touch("ana", {"shop-01"})
    assert bl.holder("shop-01") == "ana"


def test_uploads_above_the_cap_are_refused(hub, monkeypatch):
    from src.hub import routes as hub_routes

    client, _, _ = hub
    ana = _login(client, "ana", "anaanaanaana")
    client.post("/api/v1/profiles", json={"name": "shop-01"}, headers=ana)
    put = "/api/v1/hub/bundle/shop-01"
    small = _zip("shop-01", {"data/a": b"x"})
    assert client.put(put, content=small, headers=ana).status_code == 200
    monkeypatch.setattr(hub_routes, "MAX_BUNDLE", len(small) - 1)
    big = _zip("shop-01", {"data/a": b"y"})
    assert client.put(put, content=big, headers=ana).status_code == 413
    assert client.put(put + "/part?offset=0", content=big, headers=ana).status_code == 413
    assert pathlib.Path("camoufox_data/shop-01/a").read_bytes() == b"x"


def test_deleting_a_user_revokes_their_keys_and_sessions(hub):
    client, container, admin = hub
    store = container._instances["hub"]
    ana = _login(client, "ana", "anaanaanaana")
    key, agent_key = store.new_key("ana"), store.new_key("ana", "agent_key")
    assert store.verify(key) and store.verify(agent_key)
    assert client.delete("/api/v1/hub/users/ana", headers=admin).status_code == 200
    assert store.verify(key) is None and store.verify(agent_key) is None
    assert client.get("/api/v1/hub/me", headers=ana).status_code == 401


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


def test_proxy_checks_run_on_the_callers_pc(hub):
    from src.api.schemas.proxy import GeoCheckResponse
    from src.services.proxy.geo_check import GeoCheckResult

    client, container, _ = hub
    bl = container.browser_launcher
    ana = _login(client, "ana", "anaanaanaana")
    proxy = "socks5://user:pass@exit-01.example.ts.net:1080"
    r = client.post("/api/v1/proxy/check", json={"proxy": proxy}, headers=ana)
    assert r.status_code == 409  # no agent: the hub cannot vouch for a proxy only the PC reaches
    bl.touch("ana", set())

    def proxy_reply(cmd):
        assert cmd["op"] == "proxy_check" and cmd["params"]["proxy"] == proxy
        return {"ok": True, "result": {"ok": True, "message": "OK - IP: 203.0.113.7"}}

    _fake_agent(client, ana, proxy_reply)
    r = client.post("/api/v1/proxy/check", json={"proxy": proxy}, headers=ana)
    assert r.json() == {"success": True, "message": "OK - IP: 203.0.113.7"}

    tz = "Asia/Ho_Chi_Minh"
    geo = GeoCheckResult("203.0.113.7", "VN", tz, "vi-VN", tz, False, "vi-VN", False, [], [])

    def geo_reply(cmd):
        assert cmd["op"] == "geo_check" and cmd["params"]["proxy"] == proxy
        return {"ok": True, "result": GeoCheckResponse.from_result(geo).model_dump()}

    client.post("/api/v1/profiles", json={"name": "shop-01", "proxy": proxy}, headers=ana)
    _fake_agent(client, ana, geo_reply)
    r = client.get("/api/v1/profiles/shop-01/ip-check", headers=ana)
    assert r.status_code == 200 and r.json()["exit_ip"] == "203.0.113.7"

    _fake_agent(client, ana, lambda cmd: {"ok": False, "error": "On ana's PC: boom", "status": 502})
    r = client.post("/api/v1/proxy/geo-check", json={"proxy": proxy}, headers=ana)
    assert r.status_code == 502 and r.json()["detail"] == "On ana's PC: boom"


def test_agent_key_is_separate_from_the_personal_key(hub):
    client, container, _ = hub
    ana = _login(client, "ana", "anaanaanaana")
    personal = {
        "Authorization": "Bearer " + client.post("/api/v1/hub/me/key", headers=ana).json()["key"]
    }
    r = client.post("/api/v1/hub/me/agent-key", headers=ana)
    assert r.json()["name"] == "ana" and r.json()["role"] == "operator"
    agent = {"Authorization": "Bearer " + r.json()["key"]}
    assert client.get("/api/v1/hub/me", headers=agent).json()["name"] == "ana"
    # Authorizing the agent again revokes its old key, never the personal one.
    client.post("/api/v1/hub/me/agent-key", headers=ana)
    assert client.get("/api/v1/hub/me", headers=agent).status_code == 401
    assert client.get("/api/v1/hub/me", headers=personal).status_code == 200


def test_army_key_opens_the_users_own_profiles(hub, monkeypatch):
    client, container, admin = hub
    ana = _login(client, "ana", "anaanaanaana")
    client.post("/api/v1/profiles", json={"name": "shop-01"}, headers=ana)
    # Off until the hub shares a secret with Army.
    assert client.post("/api/v1/hub/me/army-key", headers=ana).status_code == 404
    assert client.get("/api/v1/info").json()["army"] is False
    monkeypatch.setattr(config, "ARMY_SECRET", ARMY_SECRET)
    assert client.get("/api/v1/info").json()["army"] is True

    army = {
        "Authorization": "Bearer "
        + client.post("/api/v1/hub/me/army-key", headers=ana).json()["key"]
    }
    assert client.get("/api/v1/hub/me", headers=army).json()["name"] == "ana"
    assert [p["name"] for p in client.get("/api/v1/profiles", headers=army).json()["profiles"]] == [
        "shop-01"
    ]
    # Its own kind: a new MCP or agent key leaves it working.
    client.post("/api/v1/hub/me/key", headers=ana)
    client.post("/api/v1/hub/me/agent-key", headers=ana)
    assert client.get("/api/v1/hub/me", headers=army).status_code == 200


def _army_token(secret=ARMY_SECRET, **claims):
    """What Army's back signs (jsonwebtoken, HS256) when a user presses Link."""

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    claims = {
        "sub": "Dan@Example.com",
        "name": "Dan",
        "permisos": ["foxprofile:access"],
        "state": secrets.token_urlsafe(32),
        "cb": CALLBACK,
        "aud": "foxprofile",
        "exp": int(time.time()) + 600,
        **claims,
    }
    body = b64({"alg": "HS256", "typ": "JWT"}) + "." + b64(claims)
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return body + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


def test_army_login_signs_in_without_a_password(hub, monkeypatch):
    client, _, _ = hub
    url = "/api/v1/hub/army-login"
    # Off without the shared secret, even with a well-formed token.
    assert client.post(url, json={"token": _army_token()}).status_code == 401
    monkeypatch.setattr(config, "ARMY_SECRET", ARMY_SECRET)

    token = _army_token(state="s" * 43)
    r = client.post(url, json={"token": token})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["name"], body["role"], body["display"]) == ("dan@example.com", "operator", "Dan")
    assert (body["callback"], body["state"]) == (CALLBACK, "s" * 43)
    session = {"Authorization": "Bearer " + body["token"]}
    assert client.get("/api/v1/hub/me", headers=session).json()["name"] == "dan@example.com"
    assert client.post("/api/v1/hub/me/army-key", headers=session).status_code == 200
    # One session per token.
    assert client.post(url, json={"token": token}).status_code == 401

    assert client.post(url, json={"token": _army_token(secret="b" * 32)}).status_code == 401
    assert (
        client.post(url, json={"token": _army_token(exp=int(time.time()) - 1)}).status_code == 401
    )
    assert client.post(url, json={"token": _army_token(aud="other")}).status_code == 401
    assert (
        client.post(url, json={"token": _army_token(cb="javascript:alert(1)")}).status_code == 401
    )
    assert client.post(url, json={"token": "not.a.token"}).status_code == 401
    # Army's permissions decide the role, like a password login.
    r = client.post(url, json={"token": _army_token(permisos=["reports:read"])})
    assert r.status_code == 403
    r = client.post(
        url, json={"token": _army_token(sub="boss@example.com", permisos=["foxprofile:admin"])}
    )
    assert r.json()["role"] == "admin"


def test_admin_creates_accounts_to_assign_profiles(hub):
    client, container, admin = hub
    ana = _login(client, "ana", "anaanaanaana")
    client.post("/api/v1/profiles", json={"name": "shop-02"}, headers=admin)
    assert client.put("/api/v1/hub/users/dan@example.com", json={}, headers=ana).status_code == 403
    r = client.put("/api/v1/hub/users/Dan@Example.com", json={"display": "Dan"}, headers=admin)
    assert r.json() == {"name": "dan@example.com", "role": "operator", "display": "Dan"}
    assert (
        client.put(
            "/api/v1/hub/owners/shop-02", json={"user": "dan@example.com"}, headers=admin
        ).status_code
        == 200
    )
    # An existing account keeps its role and name.
    r = client.put("/api/v1/hub/users/admin@example.com", json={"role": "operator"}, headers=admin)
    assert r.json()["role"] == "admin"


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


def test_automation_only_reaches_the_callers_profiles(hub, monkeypatch):
    client, container, _ = hub
    ana, bob = _login(client, "ana", "anaanaanaana"), _login(client, "bob", "bobbobbobbob")
    client.post("/api/v1/profiles", json={"name": "ana-1"}, headers=ana)
    client.post("/api/v1/profiles", json={"name": "bob-1"}, headers=bob)
    bl = container.browser_launcher
    calls = []
    monkeypatch.setattr(bl, "is_running", lambda name: True)
    monkeypatch.setattr(bl, "control", lambda name, *a, **k: calls.append(name) or {})

    # Names in the body skip the {name} path check, so the route checks each one.
    run = {"profiles": ["ana-1", "bob-1"], "script": "back"}
    r = client.post("/api/v1/automation/run", json=run, headers=ana)
    assert r.status_code == 404 and "bob-1" in r.json()["detail"]
    sync = {"leader": "ana-1", "followers": ["bob-1"]}
    assert client.post("/api/v1/automation/sync", json=sync, headers=ana).status_code == 404
    assert calls == []
    r = client.post("/api/v1/automation/run", json={**run, "profiles": ["ana-1"]}, headers=ana)
    assert r.status_code == 200 and calls == ["ana-1"]


def test_a_dropped_agent_poll_leaves_commands_for_the_next_one(hub):
    """An agent restart leaves its old long-poll open on the hub; it must not eat commands."""
    import asyncio
    from types import SimpleNamespace

    from src.hub.routes import poll

    _, container, _ = hub
    bl = container.browser_launcher
    bl._queues["ana"].put({"op": "launch", "name": "shop-01"})

    async def gone():
        return True

    request = SimpleNamespace(
        state=SimpleNamespace(user={"name": "ana", "role": "operator"}),
        app=SimpleNamespace(state=SimpleNamespace(container=container)),
        is_disconnected=gone,
    )
    assert asyncio.run(poll(request)).status_code == 204
    assert bl.next_command("ana") == {"op": "launch", "name": "shop-01"}


def test_groups_share_profiles_and_leads_manage_their_own(hub):
    client, container, admin = hub
    api = "/api/v1/hub"
    ana, bob = _login(client, "ana", "anaanaanaana"), _login(client, "bob", "bobbobbobbob")
    cid = _login(client, "cid", "cidcidcidcid")
    client.post("/api/v1/profiles", json={"name": "shop-01"}, headers=admin)

    def names(h):
        return [p["name"] for p in client.get("/api/v1/profiles", headers=h).json()["profiles"]]

    # Only admins create groups and promote leads.
    assert client.post(f"{api}/groups", json={"name": "sales"}, headers=ana).status_code == 403
    assert client.post(f"{api}/groups", json={"name": "sales"}, headers=admin).status_code == 201
    assert client.post(f"{api}/groups", json={"name": "sales"}, headers=admin).status_code == 400
    assert client.put(f"{api}/groups/sales/members/ana", headers=ana).status_code == 403
    r = client.put(f"{api}/groups/sales/members/ana", json={"role": "lead"}, headers=admin)
    assert r.status_code == 200

    # The lead adds a member and moves a profile they can see into the group.
    assert client.get(f"{api}/users", headers=ana).status_code == 200
    assert client.get(f"{api}/users", headers=bob).status_code == 403
    assert client.put(f"{api}/groups/sales/members/bob", headers=ana).status_code == 200
    put = client.put(f"{api}/groups/sales/members/bob", json={"role": "lead"}, headers=ana)
    assert put.status_code == 403
    assert (
        client.put(f"{api}/profile-group/shop-01", json={"group": "sales"}, headers=ana).status_code
        == 404
    )
    client.put(f"{api}/profile-group/shop-01", json={"group": "sales"}, headers=admin)
    assert names(ana) == names(bob) == ["shop-01"] and names(cid) == []
    me = client.get(f"{api}/me", headers=bob).json()
    assert me["groups"] == {"shop-01": "sales"} and me["my_groups"] == {"sales": "member"}

    # A plain member cannot manage the group or move its profiles.
    assert client.put(f"{api}/groups/sales/members/cid", headers=bob).status_code == 403
    r = client.put(f"{api}/profile-group/shop-01", json={"group": None}, headers=bob)
    assert r.status_code == 403
    assert client.delete(f"{api}/groups/sales/members/ana", headers=bob).status_code == 403

    # Members on different PCs share the profile; the lease keeps it exclusive.
    bl = container.browser_launcher
    bl.touch("ana", set())
    bl.touch("bob", set())
    assert client.post("/api/v1/browser/shop-01/launch?wait=false", headers=bob).status_code == 202
    assert client.post("/api/v1/browser/shop-01/launch?wait=false", headers=ana).status_code == 409

    # Rename follows the group; deleting the user or group cleans up.
    store = container.hub_store
    store.rename_profile("shop-01", "shop-02")
    assert store.profile_groups == {"shop-02": "sales"}
    store.delete_user("bob")
    assert store.groups["sales"] == {"ana": "lead"}
    assert client.delete(f"{api}/groups/sales", headers=admin).status_code == 200
    assert store.profile_groups == {}
    # Everything survives a reload, and an old hub.json without groups still loads.
    assert HubStore("hub.json", SECRET).groups == {}
    pathlib.Path("old.json").write_text(json.dumps({"users": {}, "owners": {}}))
    assert HubStore("old.json", SECRET).profile_groups == {}


def test_each_sync_saves_the_uploaded_version_as_a_snapshot(hub, s3):
    client, _, _ = hub
    ana = _login(client, "ana", "anaanaanaana")
    client.post("/api/v1/profiles", json={"name": "shop-01"}, headers=ana)
    for session in (b"one", b"two"):
        upload = _zip("shop-01", {"data/cookies.sqlite": session})
        assert (
            client.put("/api/v1/hub/bundle/shop-01", content=upload, headers=ana).status_code == 200
        )
    listed = client.get("/api/v1/profiles/shop-01/snapshots", headers=ana).json()["snapshots"]
    assert len(listed) == 2
    # The server keeps only the current version; the bucket the earlier one too.
    first = listed[-1]["id"]
    assert (
        client.post(f"/api/v1/profiles/shop-01/snapshots/{first}/restore", headers=ana).status_code
        == 200
    )
    assert pathlib.Path("camoufox_data/shop-01/cookies.sqlite").read_bytes() == b"one"
    # Someone else's profile stays hidden.
    bob = _login(client, "bob", "bobbobbobbob")
    assert client.get("/api/v1/profiles/shop-01/snapshots", headers=bob).status_code == 404


def test_only_admins_see_the_whole_snapshot_bucket(hub, s3):
    client, _, admin = hub
    ana = _login(client, "ana", "anaanaanaana")
    assert client.get("/api/v1/snapshots", headers=ana).status_code == 403
    assert client.get("/api/v1/snapshots", headers=admin).json() == {"profiles": []}
