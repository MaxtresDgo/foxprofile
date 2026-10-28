"""Import from AdsPower: its local API, the mapping, and the import in local and hub mode."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from chrome_engine import persona as personas
from src.api.app import create_app
from src.core.container import Container
from src.services.browser import chrome_prefetch, cookies
from src.services.profile import adspower

from .test_hub import _login, hub  # noqa: F401 - the hub fixture

PHONE_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.7390.122 Mobile Safari/537.36"
)
COOKIES = [
    {
        "name": "sid",
        "value": "abc",
        "domain": ".example.com",
        "path": "/",
        "httpOnly": True,
        "secure": True,
        "session": False,
        "expires": 1893456000,
        "sameSite": "unspecified",
    }
]


def _profile(n, **kw):
    return {
        "profile_id": f"id{n}",
        "profile_no": str(n),
        "name": f"Account {n}",
        "group_name": "Ventas",
        "user_proxy_config": {"proxy_soft": "no_proxy"},
        "fbcc_proxy_acc_id": "",
        **kw,
    }


class FakeAdsPower:
    """AdsPower's local API: v2 profile list, UA, cookies and proxy list."""

    def __init__(self, profiles, api_key=""):
        self.profiles, self.api_key, self.calls = profiles, api_key, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self._answer(None)

            def do_POST(self):
                self._answer(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

            def _answer(self, body):
                url = urlsplit(self.path)
                fake.calls.append(url.path)
                if fake.api_key and self.headers.get("Authorization") != f"Bearer {fake.api_key}":
                    data = {"code": -1, "msg": "api key error"}
                else:
                    data = {"code": 0, "msg": "success", "data": fake.route(url, body)}
                out = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def route(self, url, body):
        if url.path == "/api/v2/browser-profile/list":
            items = self.profiles
            if body.get("profile_id"):
                items = [p for p in items if p["profile_id"] in body["profile_id"]]
            start = (body["page"] - 1) * body["limit"]
            return {"list": items[start : start + body["limit"]], "page": body["page"]}
        if url.path == "/api/v2/browser-profile/ua":
            return {"list": [{"profile_id": body["profile_id"][0], "ua": PHONE_UA}]}
        if url.path == "/api/v2/browser-profile/cookies":
            assert parse_qs(url.query)["profile_id"]
            return {"cookies": json.dumps(COOKIES)}
        if url.path == "/api/v2/proxy-list/list":
            return {
                "list": [
                    {
                        "proxy_id": body["proxy_id"][0],
                        "type": "socks5",
                        "host": "203.0.113.7",
                        "port": "1080",
                        "user": "user",
                        "password": "p@ss",
                    }
                ]
            }
        raise AssertionError(url.path)


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(adspower, "MIN_INTERVAL", 0)


@pytest.fixture
def fake():
    server = FakeAdsPower(
        [
            _profile(1),
            _profile(2, name="Account 1"),  # same name as #1
            _profile(
                3,
                user_proxy_config={
                    "proxy_soft": "other",
                    "proxy_type": "https",
                    "proxy_host": "198.51.100.4",
                    "proxy_port": "8080",
                    "proxy_user": "user",
                    "proxy_password": "pass",
                },
            ),
            _profile(
                4,
                fbcc_proxy_acc_id="77",
                platform="instagram.com",
                username="fox.shop",
                password="pw",
                fakey="gezd gnbv gy3t qojq",
            ),
        ]
    )
    yield server
    server.server.shutdown()


# --- mapping --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"proxy_soft": "no_proxy"}, None),
        ({}, None),
        (
            {
                "proxy_soft": "other",
                "proxy_type": "https",
                "proxy_host": "198.51.100.4",
                "proxy_port": "8080",
            },
            "http://198.51.100.4:8080",
        ),
        (
            {
                "proxy_soft": "other",
                "proxy_type": "socks5",
                "proxy_host": "203.0.113.7",
                "proxy_port": "1080",
                "proxy_user": "user",
                "proxy_password": "p@ss:1",
            },
            "socks5://user:p%40ss%3A1@203.0.113.7:1080",
        ),
    ],
)
def test_proxy_from_config(config, expected):
    assert adspower.proxy_from_config(config, []) == expected


def test_unsupported_proxy_types_are_dropped_with_a_warning():
    warnings = []
    config = {
        "proxy_soft": "other",
        "proxy_type": "ssh",
        "proxy_host": "192.0.2.1",
        "proxy_port": "22",
    }
    assert adspower.proxy_from_config(config, warnings) is None
    assert warnings


def test_os_from_ua():
    assert adspower.os_from_ua(PHONE_UA, []) == "android"
    assert adspower.os_from_ua("Mozilla/5.0 (Windows NT 10.0; Win64; x64)", []) == "windows"
    assert adspower.os_from_ua("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", []) == "macos"
    warnings = []
    assert adspower.os_from_ua("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)", warnings) == "android"
    assert warnings


def test_foxprofile_name():
    taken = {"Account 1"}
    assert adspower.foxprofile_name("Account 1", "2", taken) == "Account 1-2"
    assert adspower.foxprofile_name("a/b:c*", "3", set()) == "a-b-c-"
    assert adspower.foxprofile_name("  ", "9", set()) == "ads-9"
    assert adspower.foxprofile_name("CON", "5", set()) == "ads-5"


@pytest.mark.parametrize(
    "data",
    [
        {"list": [{"profile_id": "x", "ua": PHONE_UA}]},
        {"ua": PHONE_UA},
        {"x": {"user_agent": PHONE_UA}},
        PHONE_UA,
    ],
)
def test_find_user_agent(data):
    assert adspower.find_user_agent(data) == PHONE_UA


# --- the local API --------------------------------------------------------------


def test_profiles_page_through_the_list_and_name_duplicates(fake, monkeypatch):
    monkeypatch.setattr(adspower, "_PAGE", 3)
    items = adspower.AdsPower(fake.port).profiles()
    assert [i["target"] for i in items] == ["Account 1", "Account 1-2", "Account 3", "Account 4"]
    assert items[2]["proxy"] == "http://198.51.100.4:8080"  # no credentials shown
    assert items[3]["proxy"] == "AdsPower #77"
    assert fake.calls.count("/api/v2/browser-profile/list") == 2


def test_fetch_reads_proxy_ua_and_cookies(fake):
    data = adspower.AdsPower(fake.port).fetch("id4")
    assert data["proxy"] == "socks5://user:p%40ss@203.0.113.7:1080"  # from the proxy list
    assert (data["user_agent"], data["os_type"]) == (PHONE_UA, "android")
    assert json.loads(data["cookies"])[0]["name"] == "sid"
    assert data["account"] == {
        "platform": "instagram",
        "username": "fox.shop",
        "password": "pw",
        "totp_secret": "GEZDGNBVGY3TQOJQ",
    }
    # Its credentials stay out of the listing the panel shows.
    listed = adspower.AdsPower(fake.port).profiles()[3]
    assert "fox.shop" not in json.dumps(listed) and "pw" not in listed.values()


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("facebook.com", "facebook"),
        ("https://m.facebook.com/", "facebook"),
        ("twitter.com", "x"),
        ("X.com", "x"),
        ("mail.google.com", "gmail"),
        ("outlook.live.com", "hotmail"),
        ("amazon.com", None),
        ("notfacebook.com", None),
        ("", None),
    ],
)
def test_platform_from(domain, expected):
    assert adspower.platform_from(domain) == expected


def test_account_from_skips_what_foxprofile_cannot_keep():
    warnings = []
    assert adspower.account_from({"platform": "facebook.com"}, warnings) is None and not warnings
    assert adspower.account_from({"platform": "amazon.com", "username": "u"}, warnings) is None
    assert adspower.account_from({"platform": "x.com", "password": "p"}, warnings) is None
    account = adspower.account_from({"platform": "x.com", "username": "u", "fakey": "1!"}, warnings)
    assert account == {"platform": "x", "username": "u", "password": "", "totp_secret": ""}
    assert len(warnings) == 3


def test_api_key_and_errors(fake):
    fake.api_key = "secret"
    with pytest.raises(adspower.AdsPowerError, match="api key error"):
        adspower.AdsPower(fake.port).profiles()
    assert adspower.AdsPower(fake.port, "secret").profiles()
    with pytest.raises(adspower.AdsPowerError, match="no profile"):
        adspower.AdsPower(fake.port, "secret").fetch("nope")


def test_unreachable_adspower_says_so():
    fake = FakeAdsPower([])
    port = fake.port
    fake.server.shutdown()
    fake.server.server_close()
    with pytest.raises(adspower.AdsPowerError, match=str(port)):
        adspower.AdsPower(port).profiles()


def test_calls_are_spaced_out(fake, monkeypatch):
    monkeypatch.setattr(adspower, "MIN_INTERVAL", 0.2)
    start = time.monotonic()
    adspower.AdsPower(fake.port).fetch("id1")  # list + ua + cookies
    assert time.monotonic() - start >= 0.4


# --- import, local mode ---------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(chrome_prefetch, "start", lambda log=None: False)
    monkeypatch.setattr(chrome_prefetch, "unsupported_reason", lambda: None)
    return TestClient(create_app(Container()), base_url="http://127.0.0.1")


def test_import_creates_an_android_chrome_profile_with_its_cookies(
    client, fake, tmp_path, monkeypatch
):
    imported = []
    monkeypatch.setattr(
        cookies, "import_cookies", lambda *a: imported.append(a) or len(json.loads(a[2]))
    )
    r = client.post("/api/v1/adspower/profiles", json={"port": fake.port})
    assert r.status_code == 200
    first = r.json()["profiles"][0]
    assert (first["target"], first["exists"]) == ("Account 1", False)

    body = {"port": fake.port, "profile_id": "id3", "name": "Account 3"}
    r = client.post("/api/v1/adspower/import", json=body)
    assert r.json() == {"status": "created", "name": "Account 3", "cookies": 1, "warnings": []}
    profile = client.get("/api/v1/profiles/Account 3").json()
    assert (profile["engine"], profile["os_type"]) == ("chrome", "android")
    assert profile["proxy"] == "http://user:pass@198.51.100.4:8080"
    saved = personas.load(tmp_path / "camoufox_data" / "Account 3")
    assert (saved.platform, saved.user_agent) == ("android", PHONE_UA)
    assert imported[0][:2] == ("Account 3", "android") and imported[0][3] == "chrome"

    # Again: nothing new, and the list now says it is there.
    assert client.post("/api/v1/adspower/import", json=body).json()["status"] == "exists"
    names = {
        p["target"]: p["exists"]
        for p in client.post("/api/v1/adspower/profiles", json={"port": fake.port}).json()[
            "profiles"
        ]
    }
    assert names["Account 3"] is True


def test_import_reports_adspower_errors(client, fake):
    fake.api_key = "secret"
    r = client.post("/api/v1/adspower/profiles", json={"port": fake.port})
    assert r.status_code == 400 and "api key error" in r.json()["detail"]
    r = client.post(
        "/api/v1/adspower/import", json={"port": fake.port, "profile_id": "id1", "name": "../x"}
    )
    assert r.status_code == 400


# --- import, hub mode -----------------------------------------------------------


def _agent(client, headers, replies):
    """Answer hub commands by op until every reply was used."""

    def run():
        pending = dict(replies)
        for _ in range(200):
            r = client.get("/api/v1/hub/poll", headers=headers)
            if r.status_code == 200 and "id" in r.json():
                cmd = r.json()
                answer = pending.pop(cmd["op"])(cmd)
                client.post(f"/api/v1/hub/result/{cmd['id']}", json=answer, headers=headers)
                if not pending:
                    return
            time.sleep(0.05)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_hub_import_reads_adspower_on_the_callers_pc(hub, tmp_path, monkeypatch):  # noqa: F811
    client, container, _ = hub
    monkeypatch.setattr(chrome_prefetch, "unsupported_reason", lambda: None)
    ana = _login(client, "ana", "anaanaanaana")
    container.browser_launcher.touch("ana", set())
    data = {
        "id": "id1",
        "no": "1",
        "name": "Account 1",
        "remark": "No chip",
        "proxy": None,
        "user_agent": PHONE_UA,
        "os_type": "android",
        "cookies": json.dumps(COOKIES),
        "account": {"platform": "tiktok", "username": "fox", "password": "pw", "totp_secret": ""},
        "warnings": [],
    }

    def fetch(cmd):
        assert cmd["params"] == {"port": 50325, "api_key": "", "profile_id": "id1"}
        return {"ok": True, "result": data}

    def import_cookies(cmd):
        assert cmd["action"] == "import" and cmd["profile"]["os_type"] == "android"
        return {"ok": True, "result": {"imported": 1}}

    _agent(client, ana, {"adspower_fetch": fetch, "cookies": import_cookies})
    body = {"profile_id": "id1", "name": "Account 1"}
    r = client.post("/api/v1/adspower/import", json=body, headers=ana)
    assert r.json()["status"] == "created" and r.json()["cookies"] == 1
    hub_store = container._instances["hub"]
    assert hub_store.owners["Account 1"] == "ana"
    created = container.profile_manager.profiles["Account 1"]
    assert (created.number, created.remark) == ("1", "No chip")
    saved = personas.load(tmp_path / "camoufox_data" / "Account 1")
    assert saved.user_agent == PHONE_UA  # on the hub, so it travels in the profile ZIP
    accounts = client.get("/api/v1/profiles/Account 1/accounts", headers=ana).json()
    assert [(a["platform"], a["username"], a["has_password"]) for a in accounts] == [
        ("tiktok", "fox", True)
    ]
    # Importing again leaves the existing profile, and its accounts, as they are.
    assert (
        client.post("/api/v1/adspower/import", json=body, headers=ana).json()["status"] == "exists"
    )
    assert len(client.get("/api/v1/profiles/Account 1/accounts", headers=ana).json()) == 1


def test_hub_import_needs_the_agent(hub):  # noqa: F811
    client, _, _ = hub
    ana = _login(client, "ana", "anaanaanaana")
    r = client.post("/api/v1/adspower/profiles", json={}, headers=ana)
    assert r.status_code == 409


def _fetched(name):
    return {
        "id": "id1",
        "no": "1",
        "name": name,
        "proxy": None,
        "user_agent": PHONE_UA,
        "os_type": "android",
        "cookies": "[]",
        "warnings": [],
    }


def _hub_import(client, headers, name, group):
    _agent(client, headers, {"adspower_fetch": lambda cmd: {"ok": True, "result": _fetched(name)}})
    body = {"profile_id": "id1", "name": name, "group": group}
    return client.post("/api/v1/adspower/import", json=body, headers=headers).json()


def test_hub_import_files_profiles_under_their_adspower_group(hub, monkeypatch):  # noqa: F811
    client, container, admin = hub
    monkeypatch.setattr(chrome_prefetch, "unsupported_reason", lambda: None)
    store = container._instances["hub"]
    container.browser_launcher.touch("admin@example.com", set())
    container.browser_launcher.touch("ana", set())

    # An admin's import creates the group, named for the hub.
    r = _hub_import(client, admin, "Cuenta 1", "Ventas México")
    assert (r["status"], r["group"]) == ("created", "Ventas-Mexico")
    assert store.profile_groups["Cuenta 1"] == "Ventas-Mexico"

    # An operator cannot create groups: imported, without one, and told why.
    ana = _login(client, "ana", "anaanaanaana")
    r = _hub_import(client, ana, "Cuenta 2", "Soporte")
    assert r["status"] == "created" and r["group"] is None and r["warnings"]
    assert "Cuenta 2" not in store.profile_groups

    # A lead of an existing group can file profiles into it, including ones
    # imported before (status "exists": no AdsPower call needed).
    client.post("/api/v1/hub/groups", json={"name": "Soporte"}, headers=admin)
    client.put("/api/v1/hub/groups/Soporte/members/ana", json={"role": "lead"}, headers=admin)
    body = {"profile_id": "id2", "name": "Cuenta 2", "group": "Soporte"}
    r = client.post("/api/v1/adspower/import", json=body, headers=ana).json()
    assert (r["status"], r["group"]) == ("exists", "Soporte")
    assert store.profile_groups["Cuenta 2"] == "Soporte"


def test_hub_reimport_refreshes_only_the_cookies(hub, tmp_path, monkeypatch):  # noqa: F811
    client, container, _ = hub
    ana, bob = _login(client, "ana", "anaanaanaana"), _login(client, "bob", "bobbobbobbob")
    container.browser_launcher.touch("ana", set())
    client.post(
        "/api/v1/profiles",
        json={"name": "Account 1", "engine": "chrome", "proxy": "192.0.2.9:80"},
        headers=ana,
    )
    data = {
        "id": "id1",
        "proxy": "http://198.51.100.1:8080",
        "user_agent": PHONE_UA,
        "os_type": "android",
        "cookies": json.dumps(COOKIES),
        "account": {"platform": "x", "username": "new", "password": "p", "totp_secret": ""},
        "warnings": [],
    }
    imported = []

    def import_cookies(cmd):
        imported.append(json.loads(cmd["params"]["content"])[0]["name"])
        return {"ok": True, "result": {"imported": 1}}

    _agent(
        client,
        ana,
        {"adspower_fetch": lambda cmd: {"ok": True, "result": data}, "cookies": import_cookies},
    )
    body = {"profile_id": "id1", "name": "Account 1", "reimport": True}
    r = client.post("/api/v1/adspower/import", json=body, headers=ana)
    assert r.json()["status"] == "reimported" and r.json()["cookies"] == 1 and imported == ["sid"]
    # Nothing else changed: proxy, accounts.
    assert client.get("/api/v1/profiles/Account 1", headers=ana).json()["proxy"] == "192.0.2.9:80"
    assert client.get("/api/v1/profiles/Account 1/accounts", headers=ana).json() == []
    # Someone else's profile, or one that is open: refused.
    assert client.post("/api/v1/adspower/import", json=body, headers=bob).status_code == 404
    container.browser_launcher._leases["Account 1"] = "ana"
    assert client.post("/api/v1/adspower/import", json=body, headers=ana).status_code == 409


def test_reimport_here_needs_cookies_and_a_closed_profile():
    import contextlib

    from src.models.profile import Profile
    from src.services.browser.launcher import ProfileBusyError

    class PM:
        profiles = {"p1": Profile(name="p1", engine="chrome")}

    class BusyBL:
        @contextlib.contextmanager
        def exclusive(self, name):
            raise ProfileBusyError(name)
            yield

    result = adspower.reimport_here(PM(), BusyBL(), {"cookies": "[]"}, "p1")
    assert result["status"] == "reimported" and result["cookies"] == 0 and result["warnings"]
    with pytest.raises(adspower.AdsPowerError, match="p1"):
        adspower.reimport_here(PM(), BusyBL(), {"cookies": json.dumps(COOKIES)}, "p1")
