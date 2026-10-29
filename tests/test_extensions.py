"""Browser extensions: packages, scopes, the API, the hub's launches and the agent."""

import base64
import hashlib
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
from src.services.browser import extensions as ext
from src.services.profile.manager import ProfileManager
from tests import test_hub
from tests.test_hub import _login

hub = test_hub.hub  # the hub fixture


def _zip(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def _chrome(name="Probe", version="1.0", **manifest) -> bytes:
    body = {"manifest_version": 3, "name": name, "version": version, **manifest}
    return _zip({"manifest.json": json.dumps(body), "c.js": "1"})


def _firefox(gecko_id=None) -> bytes:
    body = {"manifest_version": 2, "name": "Fox", "version": "2.0"}
    if gecko_id:
        body["browser_specific_settings"] = {"gecko": {"id": gecko_id}}
    return _zip({"manifest.json": json.dumps(body), "META-INF/mozilla.rsa": "sig"})


def _manifest(zip_bytes: bytes) -> dict:
    return json.loads(zipfile.ZipFile(io.BytesIO(zip_bytes)).read("manifest.json"))


def _varint(n: int) -> bytes:
    out = b""
    while True:
        out += bytes([(n & 0x7F) | (0x80 if n > 0x7F else 0)])
        n >>= 7
        if not n:
            return out


def _field(number: int, value: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def _crx3(zip_bytes: bytes, key_der: bytes) -> bytes:
    proof = _field(1, key_der) + _field(2, b"signature")
    signed = _field(1, hashlib.sha256(key_der).digest()[:16])
    header = _field(2, proof) + _field(10000, signed)
    return (
        b"Cr24" + (3).to_bytes(4, "little") + len(header).to_bytes(4, "little") + header + zip_bytes
    )


def test_chrome_zip_gets_a_key_so_its_id_is_the_same_on_every_pc():
    data = _zip(
        {
            "probe/manifest.json": json.dumps(
                {"name": "__MSG_appName__", "version": "3", "default_locale": "es"}
            ),
            "probe/_locales/es/messages.json": json.dumps({"appname": {"message": "Sonda"}}),
            "probe/_metadata/verified_contents.json": "{}",
        }
    )
    meta, packed = ext.normalize(data, "probe.zip")
    manifest = _manifest(packed)
    assert meta["engine"] == "chrome" and meta["name"] == "Sonda" and meta["version"] == "3"
    assert meta["native_id"] == ext.chrome_id(base64.b64decode(manifest["key"]))
    names = zipfile.ZipFile(io.BytesIO(packed)).namelist()
    assert "_locales/es/messages.json" in names and not any(
        n.startswith("_metadata") for n in names
    )
    # The same file again: a new key would mean a new ID, so the key is kept once stored.
    again, _ = ext.normalize(packed, "probe.zip")
    assert again["native_id"] == meta["native_id"] and again["id"] == meta["id"]


def test_crx_keeps_the_stores_key_and_id():
    key = ext._new_key()
    der = base64.b64decode(key)
    meta, packed = ext.normalize(_crx3(_chrome(), der))
    assert _manifest(packed)["key"] == key and meta["native_id"] == ext.chrome_id(der)


def test_firefox_addons_get_a_gecko_id_and_lose_their_signature():
    meta, packed = ext.normalize(_firefox(), "fox.xpi")
    gecko = _manifest(packed)["browser_specific_settings"]["gecko"]["id"]
    assert meta["engine"] == "camoufox" and meta["native_id"] == gecko.lower() == gecko
    assert gecko.endswith("@foxprofile")
    assert "META-INF/mozilla.rsa" not in zipfile.ZipFile(io.BytesIO(packed)).namelist()
    meta, _ = ext.normalize(_firefox("fox@example.com"), "fox.zip")
    assert meta["engine"] == "camoufox" and meta["native_id"] == "fox@example.com"


@pytest.mark.parametrize("data", [b"not a zip", _zip({"a.txt": "x"}), _zip({"manifest.json": "["})])
def test_broken_packages_are_refused(data):
    with pytest.raises(ext.ExtensionError):
        ext.normalize(data, "x.zip")


def test_scopes_add_up_and_a_profile_can_be_left_out(tmp_path):
    store = ext.ExtensionStore(tmp_path / "extensions.json", tmp_path / "extensions")
    every = store.add(_chrome("Every"))["id"]
    team = store.add(_chrome("Team"))["id"]
    own = store.add(_chrome("Own"))["id"]
    fox = store.add(_firefox(), "fox.xpi")["id"]
    store.set_scope(every, **{"global": True, "excluded": ["b"]})
    store.set_scope(team, groups=["sales"])
    store.set_scope(own, profiles=["a"])
    store.set_scope(fox, **{"global": True})

    names = lambda *a: [e["name"] for e in store.for_profile(*a)]  # noqa: E731
    assert names("a", "chrome", "sales") == ["Every", "Own", "Team"]
    assert names("b", "chrome", "sales") == ["Team"]
    assert names("c", "chrome") == ["Every"]
    assert names("c", "camoufox") == ["Fox"]

    # An update replaces the files and keeps where the extension applies.
    updated = store.add(store.archive(own).read_bytes())
    assert updated["profiles"] == ["a"]

    store.rename_profile("a", "a2")
    store.forget_profile("b")
    reread = ext.ExtensionStore(tmp_path / "extensions.json", tmp_path / "extensions")
    assert reread.get(own)["profiles"] == ["a2"] and reread.get(every)["excluded"] == []
    store.delete(team)
    assert not (tmp_path / "extensions" / f"{team}.zip").exists()


def test_unpack_once_per_version(tmp_path):
    store = ext.ExtensionStore(tmp_path / "e.json", tmp_path / "e")
    entry = store.add(_chrome())
    [first] = store.dirs(store.for_profile("a", "chrome") or [entry])
    assert json.loads(pathlib.Path(first, "manifest.json").read_text())["name"] == "Probe"
    assert store.dirs([entry]) == [first]


def test_profile_manager_keeps_scopes_with_renamed_profiles(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pm = ProfileManager()
    pm.add_profile("a", "", "windows")
    ext_id = pm.extensions.add(_chrome())["id"]
    pm.extensions.set_scope(ext_id, profiles=["a"])
    pm.update_profile("a", "b", "", "windows")
    assert pm.extensions.get(ext_id)["profiles"] == ["b"]
    pm.delete_profile("b")
    assert pm.extensions.get(ext_id)["profiles"] == []


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auth, "API_TOKEN", "")
    monkeypatch.setattr(app_module, "API_TOKEN", "")
    container = Container()
    return TestClient(create_app(container), base_url="http://127.0.0.1"), container


def test_api_manages_extensions_and_the_launcher_loads_them(local):
    client, container = local
    client.post("/api/v1/profiles", json={"name": "p1", "engine": "chrome"})
    r = client.put("/api/v1/extensions/upload?filename=probe.zip", content=_chrome())
    assert r.status_code == 200, r.text
    ext_id = r.json()["id"]
    assert client.put("/api/v1/extensions/upload", content=b"junk").status_code == 400
    assert client.patch(f"/api/v1/extensions/{ext_id}", json={"global": True}).json()["global"]
    assert [e["id"] for e in client.get("/api/v1/extensions").json()] == [ext_id]

    file = client.get(f"/api/v1/extensions/{ext_id}/file")
    assert file.headers["X-Content-SHA256"] == hashlib.sha256(file.content).hexdigest()

    profile = container.profile_manager.profiles["p1"]
    [folder] = container.browser_launcher.extensions(profile)
    assert pathlib.Path(folder, "manifest.json").exists()
    assert client.delete(f"/api/v1/extensions/{ext_id}").status_code == 200
    assert container.browser_launcher.extensions(profile) == []
    assert client.get(f"/api/v1/extensions/{ext_id}/file").status_code == 404


def test_store_refs_must_name_an_extension(local):
    client, _ = local
    r = client.post("/api/v1/extensions/store", json={"ref": "https://example.com/nothing"})
    assert r.status_code == 400


def test_hub_admins_manage_and_launches_carry_the_groups_extensions(hub):
    client, container, admin = hub
    ana = _login(client, "ana", "anaanaanaana")
    client.post("/api/v1/profiles", json={"name": "shop-01", "engine": "chrome"}, headers=admin)
    store = container.hub_store
    store.set_owner("shop-01", "ana")
    store.add_group("sales")
    store.set_profile_group("shop-01", "sales")

    assert client.get("/api/v1/extensions", headers=ana).status_code == 403
    assert (
        client.put("/api/v1/extensions/upload", content=_chrome(), headers=ana).status_code == 403
    )
    ext_id = client.put("/api/v1/extensions/upload", content=_chrome(), headers=admin).json()["id"]
    client.patch(f"/api/v1/extensions/{ext_id}", json={"groups": ["sales"]}, headers=admin)
    # Agents download with their user's credentials.
    assert client.get(f"/api/v1/extensions/{ext_id}/file", headers=ana).status_code == 200

    bl = container.browser_launcher
    bl.touch("ana", set())
    # Her agent has the profile's Chrome build (it reports it on each poll).
    bl.agents["ana"] = {"os": "macos", "builds": ["148.0.7778.215"]}
    client.post("/api/v1/browser/shop-01/launch?wait=false", headers=ana)
    command = client.get("/api/v1/hub/poll", headers=ana).json()
    assert command["op"] == "launch"
    assert [e["id"] for e in command["extensions"]] == [ext_id]


def test_agent_downloads_each_extension_once_and_checks_it(tmp_path, monkeypatch):
    from src import agent

    monkeypatch.chdir(tmp_path)
    store = ext.ExtensionStore(tmp_path / "hub.json", tmp_path / "hub-ext")
    entry = store.add(_chrome())
    calls = []

    def call(method, path, data=None, raw=False, **kw):
        calls.append(path)
        return io.BytesIO(store.archive(entry["id"]).read_bytes())

    hub_client = agent.HubClient("https://hub.example.com", "ana", "fpk_x")
    monkeypatch.setattr(hub_client, "call", call)
    a = agent.Agent(hub_client)
    wanted = [{"id": entry["id"], "sha256": entry["sha256"], "name": entry["name"]}]
    [folder] = a._extensions(wanted)
    assert pathlib.Path(folder, "c.js").exists()
    assert a._extensions(wanted) == [folder] and len(calls) == 1  # cached

    with pytest.raises(agent.IntegrityError):
        a._extensions([{**wanted[0], "sha256": "0" * 64}])
