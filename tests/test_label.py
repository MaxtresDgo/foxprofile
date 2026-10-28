import base64
import json
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.models.profile import Profile
from src.services.browser import label
from src.services.browser.extensions import chrome_id
from src.services.profile import adspower
from src.services.profile import manager as manager_module
from src.services.profile.transfer import export_to_zip, import_from_zip


def tag(number="", remark=""):
    return {**label.current("shop-01"), "number": number, "remark": remark}


def test_titles():
    assert label.tab_title(tag()) == "shop-01"
    assert label.tab_title(tag(remark="No chip")) == "shop-01-No chip"
    assert label.full_title(tag("42", "No chip")) == "42 · shop-01-No chip"


def test_runner_reads_what_the_launcher_passed(monkeypatch):
    accounts = [{"id": "a1", "platform": "facebook", "username": "fox", "has_password": True}]
    extra = label.details("Sales", accounts)
    env = label.env(Profile(name="shop-01", number="42", remark=None), extra)
    monkeypatch.setenv(label.LABEL_ENV, env)
    got = label.current("shop-01", "socks5://user:pass@203.0.113.5:1080", "android")
    assert (got["number"], got["remark"], got["group"]) == ("42", "", "Sales")
    assert got["accounts"] == [{"platform": "facebook", "username": "fox"}]
    assert got["proxy"] == "SOCKS5 · 203.0.113.5:1080"  # never the credentials
    monkeypatch.delenv(label.LABEL_ENV)
    assert label.current("shop-01")["number"] == ""


def test_start_page_shows_what_is_known():
    page = label.page_body(
        {
            **tag("42", "No <chip>"),
            "group": "Sales",
            "os": "android",
            "accounts": [{"platform": "facebook", "username": "fox"}],
        }
    )
    for text in ("#42 · shop-01", "Sales", "Facebook", "fox", "Android", "No &lt;chip&gt;"):
        assert text in page
    assert "<script" not in page and "pass" not in page
    # Nothing given, nothing shown, except the proxy: "none" is worth saying.
    assert ">Sales<" not in label.page_body(tag())


@pytest.mark.parametrize("engine", ["chrome", "camoufox"])
def test_badge_extension(tmp_path, monkeypatch, engine):
    monkeypatch.setattr(label.tempfile, "gettempdir", lambda: str(tmp_path))
    assert label.write_extension(tag(remark="x"), engine) is None  # no number, no badge
    folder = label.write_extension(tag("42", "No chip"), engine)
    manifest = json.loads((tmp_path / folder / "manifest.json").read_text("utf-8"))
    assert manifest["name"] == "42 · shop-01-No chip"
    assert '{ text: "42" }' in (tmp_path / folder / "badge.js").read_text("utf-8")
    if engine == "chrome":
        assert chrome_id(base64.b64decode(manifest["key"])) == label.CHROME_ID
    else:
        assert manifest["browser_action"]["default_area"] == "navbar"


def test_pin_keeps_the_other_preferences(tmp_path):
    prefs = tmp_path / "Default" / "Preferences"
    prefs.parent.mkdir()
    prefs.write_text(json.dumps({"webrtc": {"ip_handling_policy": "x"}}), encoding="utf-8")
    label.pin_in_chrome(str(tmp_path))
    label.pin_in_chrome(str(tmp_path))
    saved = json.loads(prefs.read_text("utf-8"))
    assert saved["extensions"]["pinned_extensions"] == [label.CHROME_ID]
    assert saved["webrtc"] == {"ip_handling_policy": "x"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from src.api.routes.profiles import router

    monkeypatch.setattr(manager_module, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(manager_module, "DATA_DIR", str(tmp_path / "data"))
    app = FastAPI()
    app.state.container = types.SimpleNamespace(
        profile_manager=manager_module.ProfileManager(),
        browser_launcher=types.SimpleNamespace(is_running=lambda name: False),
        event_bus=types.SimpleNamespace(emit=lambda: None),
    )
    app.include_router(router)
    return TestClient(app)


def test_api_number_and_remark(client, tmp_path):
    r = client.post("/profiles", json={"name": "p", "number": " 42 ", "remark": "No  chip"})
    assert (r.json()["number"], r.json()["remark"]) == ("42", "No chip")
    # Other edits keep them, a rename takes them along, "" removes one.
    r = client.patch("/profiles/p", json={"name": "q", "os_type": "linux"})
    assert (r.json()["number"], r.json()["remark"]) == ("42", "No chip")
    r = client.patch("/profiles/q", json={"remark": ""})
    assert (r.json()["number"], r.json()["remark"]) == ("42", None)
    assert client.patch("/profiles/q", json={"number": "1" * 21}).status_code == 422
    # Kept in profiles.json.
    assert manager_module.ProfileManager().profiles["q"].number == "42"


def test_the_agent_gets_them_in_the_bundle(tmp_path):
    profile = Profile(name="shop-01", number="42", remark="No chip")
    ok, zip_path = export_to_zip(profile, str(tmp_path / "none"), str(tmp_path))
    assert ok
    ok, back = import_from_zip(zip_path, str(tmp_path / "out"))
    assert (back.number, back.remark) == ("42", "No chip")


def test_adspower_never_replaces_what_was_written_here(client):
    client.post("/profiles", json={"name": "p", "remark": "Mine"})
    pm = client.app.state.container.profile_manager
    adspower.fill_label(pm, "p", {"no": 7, "remark": "AdsPower's"})
    assert (pm.profiles["p"].number, pm.profiles["p"].remark) == ("7", "Mine")
