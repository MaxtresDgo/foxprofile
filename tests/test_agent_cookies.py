"""The agent keeps a Chrome profile's portable cookies through every sync."""

import contextlib
import json
import zipfile

from src import agent
from src.services.profile.transfer import (
    CHECKPOINTS,
    export_to_zip,
    last_verified,
    pack_cookies,
    read_cookies,
    read_meta,
    save_checkpoint,
    update_meta,
)


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


def cookies_json(*names):
    return json.dumps([cookie(n) for n in names])


def test_checkpoint_is_verified_only_when_it_reads_back_sane(tmp_path):
    good = save_checkpoint(str(tmp_path), cookies_json("a", "b", "c", "d"))
    assert good["verified"] and len(_files(tmp_path)) == 1

    # Under half of the last verified: kept, not trusted; the previous one stays.
    drop = save_checkpoint(str(tmp_path), cookies_json("a"))
    assert not drop["verified"] and "under half" in drop["reason"]
    assert len(_files(tmp_path)) == 2
    entry, text = last_verified(str(tmp_path))
    assert entry["ts"] == good["ts"] and len(json.loads(text)) == 4

    assert not save_checkpoint(str(tmp_path), "[]")["verified"]
    # A verified file damaged on disk afterwards is skipped, not uploaded.
    (tmp_path / CHECKPOINTS / good["file"]).write_bytes(b"x")
    assert last_verified(str(tmp_path)) is None


def _files(profile_dir):
    return [f for f in (profile_dir / CHECKPOINTS).iterdir() if f.name != "index.json"]


def _chrome_dir(tmp_path):
    local = tmp_path / "p1"
    local.mkdir()
    engine = {"os_type": "windows", "engine": "chrome", "chrome_version": None}
    update_meta(str(local), profile=engine)
    return local


def test_a_crash_uploads_the_last_verified_checkpoint(tmp_path, monkeypatch):
    a = _agent(tmp_path, monkeypatch)
    local = _chrome_dir(tmp_path)
    monkeypatch.setattr(
        a.bl, "control", lambda *a, **k: [{"domain": ".x.com", "name": "sid", "value": "1"}]
    )
    a.checkpoint("p1")
    monkeypatch.setattr(agent.cookies, "export_cookies", lambda *a, **k: (cookies_json("half"), 1))

    a._pack_cookies("p1", local, unexpected=True)
    assert [c["name"] for c in json.loads(read_cookies(str(local)))] == ["sid"]
    assert read_meta(str(local))["packed"]["source"] == "checkpoint"

    # A normal close with cookies packs them and drops the older checkpoints.
    a._pack_cookies("p1", local)
    assert [c["name"] for c in json.loads(read_cookies(str(local)))] == ["half"]
    assert "source" not in read_meta(str(local))["packed"]
    assert not (local / CHECKPOINTS).exists()


def test_an_empty_close_falls_back_to_the_checkpoint(tmp_path, monkeypatch):
    a = _agent(tmp_path, monkeypatch)
    local = _chrome_dir(tmp_path)
    save_checkpoint(str(local), cookies_json("sid", "csrf"))
    monkeypatch.setattr(agent.cookies, "export_cookies", lambda *a, **k: ("[]", 0))
    a._pack_cookies("p1", local)
    assert read_meta(str(local))["packed"]["cookies"] == 2
    assert read_meta(str(local))["packed"]["source"] == "checkpoint"


def test_a_crash_without_checkpoint_packs_as_before(tmp_path, monkeypatch):
    a = _agent(tmp_path, monkeypatch)
    local = _chrome_dir(tmp_path)
    monkeypatch.setattr(agent.cookies, "export_cookies", lambda *a, **k: (cookies_json("s"), 1))
    a._pack_cookies("p1", local, unexpected=True)
    assert read_meta(str(local))["packed"]["cookies"] == 1


def test_checkpoints_never_leave_the_pc(tmp_path):
    local = tmp_path / "p1"
    local.mkdir()
    save_checkpoint(str(local), cookies_json("sid"))
    ok, zip_path = export_to_zip(agent.Profile(name="p1"), str(local), str(tmp_path))
    assert ok
    with zipfile.ZipFile(zip_path) as z:
        assert not [n for n in z.namelist() if CHECKPOINTS in n]


def test_checkpoints_follow_the_wall_clock_per_profile(tmp_path, monkeypatch):
    a = _agent(tmp_path, monkeypatch)
    clock = [0.0]
    ticks = iter(range(100))

    def sleep(seconds):
        next(ticks)  # StopIteration ends the loop
        clock[0] += seconds

    monkeypatch.setattr(agent.time, "sleep", sleep)
    monkeypatch.setattr(agent.time, "time", lambda: clock[0])
    monkeypatch.setattr(a.bl, "running_profile_names", lambda: {"p1"})
    done = []
    monkeypatch.setattr(a, "checkpoint", lambda name: done.append(clock[0]))
    with contextlib.suppress(StopIteration):
        a._checkpoints()
    # 100 ticks of 30 s: first seen at 30 s, then one every 10 minutes.
    assert done == [630.0, 1230.0, 1830.0, 2430.0]
