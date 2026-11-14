"""The agent keeps a Chrome profile's portable cookies through every sync."""

import json

from src import agent
from src.services.profile.transfer import pack_cookies, read_cookies, read_meta, update_meta


def cookie(name, value="v", domain=".example.com"):
    return {"domain": domain, "name": name, "path": "/", "value": value}


def test_merge_keeps_what_the_browser_never_got():
    text, kept = agent.merge_cookies(
        json.dumps([cookie("a", "new")]), json.dumps([cookie("a", "old"), cookie("b")])
    )
    assert kept == 1
    assert {(c["name"], c["value"]) for c in json.loads(text)} == {("a", "new"), ("b", "v")}


def _agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = agent.Agent(agent.HubClient("https://hub.example.com", "ana", "fpk_x"))
    a.data_root = tmp_path
    return a


def test_chrome_pack_survives_a_crash_and_a_failed_restore(tmp_path, monkeypatch):
    a = _agent(tmp_path, monkeypatch)
    local = tmp_path / "p1"
    local.mkdir()
    pack_cookies(str(local), json.dumps([cookie("sid"), cookie("csrf")]))
    engine = {"os_type": "windows", "engine": "chrome", "chrome_version": None}
    update_meta(str(local), profile=engine)

    def refuse(*args, **kw):
        raise agent.cookies.CookieError("store locked")

    monkeypatch.setattr(agent.cookies, "import_cookies", refuse)
    profile = agent.Profile(name="p1", engine="chrome", os_type="windows")
    a._restore_cookies(profile, local)
    assert read_cookies(str(local))  # Chrome's pack stays until the close replaces it

    # This PC's browser could not read them: the close packs them again.
    monkeypatch.setattr(agent.cookies, "export_cookies", lambda *a, **k: ("[]", 0))
    a._pack_cookies("p1", local)  # no self._profiles entry: as after a crash
    assert {c["name"] for c in json.loads(read_cookies(str(local)))} == {"sid", "csrf"}
    assert read_meta(str(local))["packed"]["cookies"] == 2

    # An export that fails uploads the last good pack instead of none.
    monkeypatch.setattr(agent.cookies, "export_cookies", refuse)
    a._pack_cookies("p1", local)
    assert read_cookies(str(local)) and read_meta(str(local))["packed"]["error"]
