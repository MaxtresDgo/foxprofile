"""Agents updating to the hub's code (src/hub/agent_code.py, hub routes, the
installers' .pth hook) and the installers the panel serves."""

import importlib.util
import io
import pathlib
import sys
import zipfile

import pytest

from src.hub import agent_code
from src.hub import routes as hub_routes
from tests import test_hub
from tests.test_hub import _login

hub = test_hub.hub  # the hub fixture

HOOK = pathlib.Path(__file__).resolve().parents[1] / "installer" / "agent_update_hook.py"


def _tree(root: pathlib.Path, text: str = "v1") -> pathlib.Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "agent.py").write_text(text)
    (root / "chrome_engine" / "tests").mkdir(parents=True)
    (root / "chrome_engine" / "engine.py").write_text("engine")
    return root


def test_code_id_names_the_shipped_code_only(tmp_path):
    root = _tree(tmp_path / "a")
    first = agent_code.code_id(root)
    # Caches, tests and the bundled browser are not part of it.
    (root / "chrome_engine" / "tests" / "test_x.py").write_text("x")
    (root / "src" / "__pycache__").mkdir()
    (root / "src" / "__pycache__" / "agent.cpython-312.pyc").write_bytes(b"x")
    (root / "chrome_engine" / "builds").mkdir()
    (root / "chrome_engine" / "builds" / "chrome").write_bytes(b"x")
    (root / "requirements.txt").write_text("x")
    assert agent_code.code_id(root) == first
    (root / "src" / "agent.py").write_text("v2")
    assert agent_code.code_id(root) != first


def test_requirements_id_ignores_comments_and_order():
    a = agent_code.requirements_id("flet>=1\n# tray\npystray; sys_platform == 'win32'\n")
    b = agent_code.requirements_id("pystray; sys_platform == 'win32'  # icon\n\nflet>=1\n")
    assert a == b != agent_code.requirements_id("flet>=2\npystray; sys_platform == 'win32'")


def test_unpack_checks_the_code_and_records_it_for_this_install(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    (app / "BUILD").write_text("installed1\n")
    monkeypatch.setattr(agent_code, "INSTALLED", app)
    home = tmp_path / "home"
    (home / "agent-code" / "older").mkdir(parents=True)
    root = _tree(tmp_path / "hub", "new")
    code, data = agent_code.code_id(root), agent_code.zip_bytes(root)

    target = agent_code.unpack(data, code, home)
    assert (target / "src" / "agent.py").read_text() == "new"
    assert (home / "agent-code" / "current").read_text().split() == [code, "installed1"]
    assert not (home / "agent-code" / "older").exists()

    with pytest.raises(ValueError):  # not the code announced
        agent_code.unpack(data, "0" * 16, home)
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../evil.py", "x")
    with pytest.raises(ValueError):
        agent_code.unpack(bad.getvalue(), code, home)


def test_hook_loads_the_update_only_for_the_install_it_was_made_for(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("_hook_under_test", HOOK)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delenv("FOXPROFILE_CHROME_BUILDS", raising=False)
    home, app = tmp_path / "home", tmp_path / "app"
    _tree(home / "agent-code" / "abc")
    app.mkdir()
    (app / "BUILD").write_text("b1\n")
    assert hook.activate(str(home), str(app)) is None  # nothing unpacked yet

    (home / "agent-code" / "current").write_text("abc b1\n")
    target = hook.activate(str(home), str(app))
    assert sys.path[0] == target == str(home / "agent-code" / "abc")
    monkeypatch.setattr(sys, "path", sys.path[1:])
    # A newer installer replaced the app: its own code wins until the hub sends more.
    (app / "BUILD").write_text("b2\n")
    assert hook.activate(str(home), str(app)) is None


@pytest.fixture
def hub_code(monkeypatch):
    monkeypatch.setattr(hub_routes, "POLL_WAIT", 0)
    monkeypatch.setattr(agent_code, "hub_code", lambda: ("hubcode", b"zipdata"))
    monkeypatch.setattr(agent_code, "hub_requirements", lambda: "req1")


def _poll(client, headers, **params):
    return client.get("/api/v1/hub/poll", params=params, headers=headers)


def test_poll_offers_the_update_when_the_agent_is_idle(hub, hub_code):
    client, container, admin = hub
    ana = _login(client, "ana", "anaanaanaana")
    # Same code, a script agent (no installer) and an old agent: nothing to do.
    assert _poll(client, ana, code="hubcode", req="req1").status_code == 204
    assert _poll(client, ana, code="old", req="").status_code == 204
    assert _poll(client, ana).status_code == 204

    r = _poll(client, ana, code="old", req="req1", pc="PC-1")
    assert r.json() == {"op": "update", "code": "hubcode"}
    # Not offered again right away (the agent is restarting, or failed to download).
    assert _poll(client, ana, code="old", req="req1", pc="PC-1").status_code == 204
    r = client.get("/api/v1/hub/agent/code", headers=ana)
    assert r.content == b"zipdata" and r.headers["X-Code-Id"] == "hubcode"

    agents = client.get("/api/v1/hub/agents", headers=admin).json()
    assert agents["code"] == "hubcode"
    assert agents["agents"][0]["user"] == "ana"
    assert agents["agents"][0]["pc"] == "PC-1"
    assert agents["agents"][0]["state"] == "pending"
    assert client.get("/api/v1/hub/agents", headers=ana).status_code == 403


def test_open_profiles_wait_unless_an_admin_asks(hub, hub_code):
    client, container, admin = hub
    bob = _login(client, "bob", "bobbobbobbob")
    container.profile_manager.add_profile("p1", None, "windows", None, None)
    container._instances["hub"].set_owner("p1", "bob")
    assert _poll(client, bob, running="p1", code="old", req="req1").status_code == 204
    assert client.post("/api/v1/hub/agents/nobody/update", headers=admin).status_code == 404
    assert client.post("/api/v1/hub/agents/bob/update", headers=bob).status_code == 403
    assert client.post("/api/v1/hub/agents/bob/update", headers=admin).status_code == 200
    r = _poll(client, bob, running="p1", code="old", req="req1")
    assert r.json()["op"] == "update"


def test_other_libraries_ask_for_the_installer_once(hub, hub_code):
    client, container, admin = hub
    ana = _login(client, "ana", "anaanaanaana")
    assert _poll(client, ana, code="old", req="req0").json() == {"op": "installer_needed"}
    assert _poll(client, ana, code="old", req="req0").status_code == 204
    state = client.get("/api/v1/hub/agents", headers=admin).json()["agents"][0]["state"]
    assert state == "installer"


def test_installers_upload_by_admins_and_public_download(hub, monkeypatch):
    from src.core import config

    monkeypatch.setattr(config, "S3_BUCKET", "")  # the hub's own folder
    client, container, admin = hub
    ana = _login(client, "ana", "anaanaanaana")
    url = "/api/v1/hub/installers/windows"
    assert client.put(url, params={"name": "a.msi"}, content=b"x", headers=ana).status_code == 403
    for name in ("a.pkg", "../a.msi", "a b.msi"):
        r = client.put(url, params={"name": name}, content=b"x", headers=admin)
        assert r.status_code == 400, name
    assert client.put("/api/v1/hub/installers/linux?name=a.msi", headers=admin).status_code == 404
    assert client.get(url).status_code == 404

    assert client.put(url, params={"name": "Agent-1.msi"}, content=b"one", headers=admin).is_success
    assert client.put(url, params={"name": "Agent-2.msi"}, content=b"two", headers=admin).is_success
    listed = client.get("/api/v1/hub/installers", headers=ana).json()
    assert [(i["platform"], i["name"], i["size"]) for i in listed] == [
        ("windows", "Agent-2.msi", 3)
    ]
    r = client.get(url)  # no session: a new PC downloads it before signing in
    assert r.content == b"two"
    assert "Agent-2.msi" in r.headers["content-disposition"]


@pytest.fixture
def bucket(monkeypatch):
    """A private bucket the hub reaches at one address and browsers at another."""
    from botocore.stub import Stubber

    from src.core import config
    from src.hub import installers

    for key, value in {
        "S3_ENDPOINT": "http://rustfs.internal:9000",
        "S3_PUBLIC_URL": "https://files.example.test",
        "S3_BUCKET": "fox",
        "S3_ACCESS_KEY": "user",
        "S3_SECRET_KEY": "pass",
        "S3_PREFIX": "fox-profiles/dev/installers/",
    }.items():
        monkeypatch.setattr(config, key, value)
    installers._client.cache_clear()
    stub = Stubber(installers._client())
    stub.activate()
    yield stub
    stub.deactivate()
    installers._client.cache_clear()


def _listed(stub, platform, *names):
    import datetime

    contents = [
        {
            "Key": f"fox-profiles/dev/installers/{platform}/{name}",
            "Size": 10 + i,
            "LastModified": datetime.datetime(2026, 10, 1 + i, tzinfo=datetime.timezone.utc),
        }
        for i, name in enumerate(names)
    ]
    expected = {"Bucket": "fox", "Prefix": f"fox-profiles/dev/installers/{platform}/"}
    stub.add_response("list_objects_v2", {"Contents": contents} if contents else {}, expected)


def test_installers_in_a_bucket_go_through_presigned_urls(hub, bucket):
    client, container, admin = hub
    ana = _login(client, "ana", "anaanaanaana")
    base = "/api/v1/hub/installers/windows"
    key = "fox-profiles/dev/installers/windows/A-2.msi"
    url = base + "/upload-url?name=A-2.msi&size=" + str(150 * 1024 * 1024)
    assert client.post(url, headers=ana).status_code == 403
    assert client.post(base + "/upload-url?name=A-2.msi&size=0", headers=admin).status_code == 400
    bucket.add_response(
        "create_multipart_upload",
        {"Bucket": "fox", "Key": key, "UploadId": "u1"},
        {"Bucket": "fox", "Key": key, "ContentType": "application/octet-stream"},
    )
    r = client.post(url, headers=admin).json()
    # 150 MB in three parts, each under a proxy's 100 MB cap, each signed for the
    # browsers' address (path style).
    assert r["direct"] and r["upload_id"] == "u1" and r["part_size"] == 64 * 1024 * 1024
    assert len(r["parts"]) == 3
    for n, part in enumerate(r["parts"], 1):
        assert part.startswith(f"https://files.example.test/fox/{key}?")
        assert f"partNumber={n}" in part and "uploadId=u1" in part
        assert "X-Amz-Signature=" in part
    assert client.put(base + "?name=A-2.msi", content=b"x", headers=admin).status_code == 400

    # After the browser's PUTs: join the parts; the new one stays, older ones go.
    parts = {"Parts": [{"ETag": '"e1"', "PartNumber": 1}, {"ETag": '"e2"', "PartNumber": 2}]}
    bucket.add_response(
        "complete_multipart_upload",
        {},
        {"Bucket": "fox", "Key": key, "UploadId": "u1", "MultipartUpload": parts},
    )
    _listed(bucket, "windows", "A-1.msi", "A-2.msi")
    bucket.add_response(
        "delete_object", {}, {"Bucket": "fox", "Key": "fox-profiles/dev/installers/windows/A-1.msi"}
    )
    done = {"name": "A-2.msi", "upload_id": "u1", "etags": ['"e1"', '"e2"']}
    r = client.post(base + "/uploaded", json=done, headers=admin)
    assert r.json() == {"platform": "windows", "name": "A-2.msi", "size": 11}
    # A failed upload drops its parts.
    bucket.add_response(
        "abort_multipart_upload", {}, {"Bucket": "fox", "Key": key, "UploadId": "u2"}
    )
    abort = {"name": "A-2.msi", "upload_id": "u2"}
    assert client.post(base + "/abort", json=abort, headers=admin).is_success

    _listed(bucket, "windows", "A-2.msi")
    _listed(bucket, "macos")
    listed = client.get("/api/v1/hub/installers", headers=ana).json()
    assert [(i["platform"], i["name"]) for i in listed] == [("windows", "A-2.msi")]

    _listed(bucket, "windows", "A-2.msi")
    r = client.get(base, follow_redirects=False)  # public, like the folder's
    assert r.status_code == 307
    location = r.headers["location"]
    assert location.startswith(
        "https://files.example.test/fox/fox-profiles/dev/installers/windows/A-2.msi?"
    )
    assert "response-content-disposition=attachment" in location

    _listed(bucket, "macos")
    assert client.get("/api/v1/hub/installers/macos", follow_redirects=False).status_code == 404
    bucket.add_client_error("list_objects_v2", "AccessDenied", http_status_code=403)
    assert client.get("/api/v1/hub/installers", headers=ana).status_code == 502
    bucket.assert_no_pending_responses()
