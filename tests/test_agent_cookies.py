"""The agent keeps a Chrome profile's portable cookies through every sync."""

import contextlib
import io
import json
import secrets
import shutil
import zipfile

import pytest

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
    # 100 ticks of 5 s: first seen at 5 s, then one every 20 s.
    assert done[:3] == [25.0, 45.0, 65.0] and len(done) == 24


def _cached_agent(tmp_path, monkeypatch):
    """An agent that uploaded p1 (Camoufox) and kept it under stamp "s1"."""
    a = _agent(tmp_path, monkeypatch)
    a.data_root = tmp_path / "data"
    a.cache_root = tmp_path / "cache"
    local = a.data_root / "p1"
    (local / "cache2").mkdir(parents=True)
    (local / "cookies.sqlite").write_text("sessions")
    save_checkpoint(str(local), cookies_json("sid"))
    a._profiles["p1"] = agent.Profile(name="p1")
    a._cache_put("p1", local, "s1")
    return a


def _no_hub(*args, **kw):
    raise agent.urllib.error.URLError("hub down")


def test_an_unchanged_hub_copy_opens_from_this_pcs_copy(tmp_path, monkeypatch):
    a = _cached_agent(tmp_path, monkeypatch)
    kept = a.cache_root / "p1"
    assert (kept / "cookies.sqlite").exists()
    assert not (kept / "cache2").exists() and not (kept / CHECKPOINTS).exists()
    assert a.cached_names() == ["p1"]

    monkeypatch.setattr(a, "_fetch_into", _no_hub)  # never asked: the stamps match
    profile = a._download("p1", {"name": "p1", "proxy": "socks5://192.0.2.1:1080"}, "s1")
    assert profile.proxy == "socks5://192.0.2.1:1080"  # the hub's settings, not the copy's
    assert (a.data_root / "p1" / "cookies.sqlite").read_text() == "sessions"
    assert read_meta(str(a.data_root / "p1"))["restored"]["source"] == "cache"
    assert a.cached_names() == []  # in use: it comes back after the next upload


def test_a_hub_outage_falls_back_to_the_local_copy(tmp_path, monkeypatch):
    a = _cached_agent(tmp_path, monkeypatch)
    monkeypatch.setattr(a, "_fetch_into", _no_hub)
    a._download("p1", {"name": "p1"}, "changed-on-the-hub")
    assert read_meta(str(a.data_root / "p1"))["restored"]["stale"]


def test_a_hub_answer_is_not_an_outage(tmp_path, monkeypatch):
    a = _cached_agent(tmp_path, monkeypatch)

    def gone(*args, **kw):
        raise agent.urllib.error.HTTPError("u", 404, "gone", {}, None)

    monkeypatch.setattr(a, "_fetch_into", gone)
    with pytest.raises(agent.urllib.error.HTTPError):
        a._download("p1", {"name": "p1"}, "s2")
    assert a.cached_names() == ["p1"]


def test_offline_opens_only_while_the_hub_is_down(tmp_path, monkeypatch):
    a = _cached_agent(tmp_path, monkeypatch)
    opened = []
    monkeypatch.setattr(a, "launch", lambda profile, offline=False: opened.append(offline))
    a.hub.reachable = True
    a.open_offline("p1")
    a.hub.reachable = False
    a.open_offline("p1")
    assert opened == [True]


def test_copies_of_another_account_or_unused_go(tmp_path, monkeypatch):
    a = _cached_agent(tmp_path, monkeypatch)
    a.hub.user = "bob"  # switched account: ana's sessions are not bob's
    assert a.cached_names() == []
    a.prune_cache()
    assert not (a.cache_root / "p1").exists()

    a = _cached_agent(tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "CACHE_DAYS", 0)
    kept_at = json.loads((a.cache_root / "p1.json").read_text())["ts"]
    monkeypatch.setattr(agent.time, "time", lambda: kept_at)  # same tick, as on Windows (~15 ms)
    a.prune_cache()
    assert not (a.cache_root / "p1").exists()


def test_a_conflicting_upload_is_not_kept_as_the_hubs_copy(tmp_path, monkeypatch):
    a = _cached_agent(tmp_path, monkeypatch)
    a._download("p1", {"name": "p1"}, "s1")  # opens the copy of version s1
    assert read_meta(str(a.data_root / "p1"))["base_stamp"] == "s1"
    monkeypatch.setattr(a, "_pack_cookies", lambda *args: None)
    monkeypatch.setattr(a, "_upload", lambda *args: {"conflict": "c1"})
    a._sync_now("p1")
    assert not (a.data_root / "p1").exists() and a.cached_names() == []


def test_the_local_panel_lists_the_copies_on_this_pc(tmp_path, monkeypatch):
    a = _cached_agent(tmp_path, monkeypatch)
    (a.data_root / "p2").mkdir()  # left behind, not uploaded yet
    listed = {p["name"]: p for p in a.local_profiles()}
    assert listed["p1"]["state"] == "idle" and listed["p1"]["copy"]
    assert listed["p1"]["engine"] == "camoufox"
    assert listed["p2"]["state"] == "pending" and listed["p2"]["copy"] is None


def test_the_hub_counts_as_down_only_after_a_failed_call(monkeypatch):
    hub = agent.HubClient("https://hub.example.com", "ana", "fpk_x")
    assert hub.reachable is None  # not known yet: nothing opens offline

    def answer(code):
        def send(*args, **kw):
            raise agent.urllib.error.HTTPError("u", code, "x", {}, None)

        return send

    for code, reachable in ((404, True), (530, False)):
        monkeypatch.setattr(hub, "_send", answer(code))
        with pytest.raises(agent.urllib.error.HTTPError):
            hub.call("GET", "/hub/poll")
        assert hub.reachable is reachable


# --- light sync (hub FOXPROFILE_HUB_SYNC=light) --------------------------------


def _serve_light(monkeypatch, a, tmp_path, *names, files=None):
    """The hub's light bundle of p1, served to `a`: its pack and `files`."""
    hub = tmp_path / f"hub-{secrets.token_hex(3)}" / "p1"
    hub.mkdir(parents=True)
    for rel, text in (files or {}).items():
        (hub / rel).parent.mkdir(parents=True, exist_ok=True)
        (hub / rel).write_text(text)
    if names:
        pack_cookies(str(hub), cookies_json(*names))
    ok, zip_path = export_to_zip(agent.Profile(name="p1"), str(hub), str(hub.parent), light=True)
    asked = []

    def fetch(name, path, light=False):
        asked.append(light)
        shutil.copy(zip_path, path)

    monkeypatch.setattr(a, "_fetch_bundle", fetch)
    return asked


def _imports(monkeypatch):
    got = []

    def record(name, os_type, text, engine, version):
        got.append([c["name"] for c in json.loads(text)])
        return len(got[-1])

    monkeypatch.setattr(agent.cookies, "import_cookies", record)
    return got


def test_a_light_open_over_this_pcs_copy_takes_the_hubs_sessions(tmp_path, monkeypatch):
    a = _cached_agent(tmp_path, monkeypatch)
    (a.cache_root / "p1" / "places.sqlite").write_text("history")
    ls = "Default/Local Storage/leveldb/000003.log"
    asked = _serve_light(monkeypatch, a, tmp_path, "sid", files={ls: "from the other PC"})
    imported = _imports(monkeypatch)

    a._download("p1", {"name": "p1"}, "changed-on-the-hub", light=True)
    local = a.data_root / "p1"
    assert asked == [True]
    assert (local / "places.sqlite").read_text() == "history"  # not light: this PC's stays
    assert (local / ls).read_text() == "from the other PC"
    # Another PC's pack: this copy's store goes, so none of its old sessions come back.
    assert not (local / "cookies.sqlite").exists() and imported == [["sid"]]
    assert read_cookies(str(local))  # the pack stays until the close replaces it
    assert read_meta(str(local))["sync"] == {"light": True, "full": False}


def test_a_light_open_of_its_own_pack_keeps_the_store(tmp_path, monkeypatch):
    a = _agent(tmp_path, monkeypatch)
    a.data_root, a.cache_root = tmp_path / "data", tmp_path / "cache"
    local = a.data_root / "p1"
    local.mkdir(parents=True)
    (local / "cookies.sqlite").write_text("newer than the pack")
    pack_cookies(str(local), cookies_json("sid"))
    a._profiles["p1"] = agent.Profile(name="p1")
    a._cache_put("p1", local, "s1")
    monkeypatch.setattr(a, "_fetch_bundle", _no_hub)  # never asked: the stamps match
    imported = _imports(monkeypatch)

    a._download("p1", {"name": "p1"}, "s1", light=True)
    assert imported == [] and (local / "cookies.sqlite").read_text() == "newer than the pack"
    assert read_meta(str(local))["restored"]["kept_store"]


def test_a_first_open_on_a_pc_takes_the_whole_profile(tmp_path, monkeypatch):
    a = _agent(tmp_path, monkeypatch)
    a.data_root, a.cache_root = tmp_path / "data", tmp_path / "cache"
    monkeypatch.setattr(a, "_fetch_bundle", _no_hub)  # not the light bundle

    def whole(name, local):
        local.mkdir(parents=True)
        (local / "History").write_text("visits")
        (local / "cookies.sqlite").write_text("written on another PC")
        pack_cookies(str(local), cookies_json("sid"))
        return agent.Profile(name=name), 1234

    monkeypatch.setattr(a, "_fetch_into", whole)
    imported = _imports(monkeypatch)
    a._download("p1", {"name": "p1"}, "s1", light=True, full=True)
    local = a.data_root / "p1"
    assert (local / "History").read_text() == "visits"
    # The hub's store is another PC's: the pack fills a new one.
    assert not (local / "cookies.sqlite").exists() and imported == [["sid"]]
    assert read_meta(str(local))["sync"] == {"light": True, "full": True}


def test_a_cookie_job_takes_only_the_light_bundle(tmp_path, monkeypatch):
    a = _agent(tmp_path, monkeypatch)
    a.data_root, a.cache_root = tmp_path / "data", tmp_path / "cache"
    _serve_light(monkeypatch, a, tmp_path, "sid", files={"fingerprint.json": "{}"})
    imported = _imports(monkeypatch)
    a._download("p1", light=True)
    local = a.data_root / "p1"
    assert {f.name for f in local.iterdir()} == {
        "cookies.json.gz",
        "cookies.json.gz.sha256",
        "fingerprint.json",
        "sync.json",
    }
    assert imported == [["sid"]]


def test_a_light_close_uploads_the_light_bundle_unless_a_backup_is_due(tmp_path, monkeypatch):
    a = _agent(tmp_path, monkeypatch)
    local = tmp_path / "p1"
    local.mkdir()
    (local / "History").write_text("visits")
    pack_cookies(str(local), cookies_json("sid"))
    sent = []

    def call(method, path, data=None, **kw):
        if "/part" in path:
            sent.append(data)
        return {"stamp": "s9"}

    monkeypatch.setattr(a.hub, "call", call)

    def uploaded(full):
        sent.clear()
        update_meta(str(local), sync={"light": True, "full": full})
        a._upload("p1", local)
        return zipfile.ZipFile(io.BytesIO(b"".join(sent))).namelist()

    assert "data/History" not in uploaded(full=False)
    assert "data/History" in uploaded(full=True)


def test_a_copy_of_another_chrome_build_goes(tmp_path, monkeypatch):
    a = _cached_agent(tmp_path, monkeypatch)
    monkeypatch.setattr(a, "_fetch_into", _no_hub)
    # Downgraded on the hub: the same stamp, but this copy has a newer build's data.
    with pytest.raises(agent.urllib.error.URLError):
        a._download("p1", {"name": "p1", "chrome_version": "129"}, "s1")
    assert a.cached_names() == []


def test_copies_past_the_space_cap_go_least_used_first(tmp_path, monkeypatch):
    a = _agent(tmp_path, monkeypatch)
    a.data_root, a.cache_root = tmp_path / "data", tmp_path / "cache"
    monkeypatch.setattr(agent, "CACHE_MAX_GB", 3000 / 2**30)  # room for one of them
    for name in ("p1", "p2"):
        local = a.data_root / name
        local.mkdir(parents=True)
        (local / "cookies.sqlite").write_text("x" * 2000)
        a._profiles[name] = agent.Profile(name=name)
        a._cache_put(name, local, "s1")
    assert a.cached_names() == ["p2"]

    # A nearly full disk: none is kept.
    monkeypatch.setattr(agent.shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(1, 1, 0))
    a.prune_cache()
    assert a.cached_names() == []


def test_a_stop_checkpoints_the_live_browser_first(tmp_path, monkeypatch):
    a = _agent(tmp_path, monkeypatch)
    _chrome_dir(tmp_path)
    monkeypatch.setattr(
        a.bl, "control", lambda *a, **k: [{"domain": ".x.com", "name": "sid", "value": "1"}]
    )
    monkeypatch.setattr(a.bl, "running_profile_names", lambda: {"p1"})
    monkeypatch.setattr(a.bl, "stop_profile", lambda name, timeout: True)
    monkeypatch.setattr(a, "_sync", lambda name, unexpected=False: None)
    saved = []

    def save(profile_dir, text):
        saved.append(text)
        return {"verified": True, "cookies": 1, "reason": ""}

    monkeypatch.setattr(agent, "save_checkpoint", save)
    a.stop("p1")
    assert len(saved) == 1  # before the browser closes: its export afterwards can fail
    a.checkpoint("p1")
    assert len(saved) == 1  # the same cookies are not saved again


# --- where the profiles live (C:\FoxProfiles\<Windows user>) -------------------


def test_each_account_keeps_its_own_copies(tmp_path, monkeypatch):
    a = _agent(tmp_path, monkeypatch)
    local = a.data_root / "p1"
    local.mkdir()
    (local / "cookies.sqlite").write_text("ana's sessions")
    a._profiles["p1"] = agent.Profile(name="p1")
    a._cache_put("p1", local, "s1")
    assert (tmp_path / "profile-cache" / "ana" / "p1.json").exists()

    a.hub.user = "bob@example.com"  # switched account on the same PC
    assert a.cached_names() == [] and a.cache_root.name == "bob@example.com"
    a.prune_cache()
    assert (tmp_path / "profile-cache" / "ana" / "p1").is_dir()  # for ana's next sign-in
    a.hub.user = "ana"
    assert a.cached_names() == ["p1"]
    assert agent.account_dir("a/b:c") == "a_b_c"


def test_an_earlier_agents_folders_move_to_the_new_place(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "camoufox_data" / "p2").mkdir(parents=True)  # still to upload
    old = tmp_path / "profile-cache"
    (old / "p1").mkdir(parents=True)
    record = {"user": "ana", "hub": "https://hub.example.com", "stamp": "s1"}
    (old / "p1.json").write_text(json.dumps(record))
    mine = tmp_path / "FoxProfiles" / "braya"
    monkeypatch.setattr(agent, "DATA_DIR", str(mine / "perfiles"))

    a = agent.Agent(agent.HubClient("https://hub.example.com", "ana", "fpk_x"))
    assert a.cache_root == mine / "cache" / "ana"
    assert (mine / "perfiles" / "p2").is_dir() and not (tmp_path / "camoufox_data").exists()
    assert (mine / "cache" / "ana" / "p1").is_dir() and a.cached_names() == ["p1"]
    assert not old.exists()


def test_a_pc_without_copies_yet_starts_polling(tmp_path, monkeypatch):
    """A first start in FoxProfiles finds no cache folder (3.0.123 stopped polling there)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent, "DATA_DIR", str(tmp_path / "FoxProfiles" / "braya" / "perfiles"))
    a = agent.Agent(agent.HubClient("https://hub.example.com", "ana", "fpk_x"))
    assert a.cache_base.is_dir()  # made at the start, next to perfiles
    a.cache_base.rmdir()  # deleted by hand: still no reason to stop
    a.recover()  # uploads what is left, then prunes the copies: none yet
    assert a.cached_names() == []
