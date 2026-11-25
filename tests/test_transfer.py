import json
import zipfile

import pytest

from src.services.browser.launcher import BrowserLauncher, ProfileBusyError, _LaunchResult
from src.services.profile.transfer import import_from_zip


def _archive(path, name, files):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("profile.json", json.dumps({"name": name, "os_type": "windows"}))
        for arcname, data in files.items():
            z.writestr(arcname, data)
    return str(path)


def test_import_rejects_traversal_profile_name(tmp_path):
    zip_path = _archive(tmp_path / "p.zip", "..", {})
    ok, msg = import_from_zip(zip_path, str(tmp_path / "data"))
    assert not ok and "name" in msg


def test_import_rejects_zip_slip_entry(tmp_path):
    zip_path = _archive(tmp_path / "p.zip", "good", {"data/../../escaped.txt": "x"})
    ok, msg = import_from_zip(zip_path, str(tmp_path / "data"))
    assert not ok and "Unsafe path" in msg
    assert not (tmp_path / "escaped.txt").exists()


def test_import_extracts_normal_archive(tmp_path):
    zip_path = _archive(tmp_path / "p.zip", "good", {"data/fingerprint.json": "{}"})
    ok, profile = import_from_zip(zip_path, str(tmp_path / "data"))
    assert ok and profile.name == "good"
    assert (tmp_path / "data" / "good" / "fingerprint.json").read_text() == "{}"


def test_exclusive_blocks_second_holder():
    bl = BrowserLauncher()
    with bl.exclusive("p"), pytest.raises(ProfileBusyError), bl.exclusive("p"):
        pass
    with bl.exclusive("p"):  # released after the block
        pass


def test_launch_result_settles_once():
    r = _LaunchResult()
    r.settle(True)
    r.settle(False, "late failure")
    assert r.ok is True and r.error is None


def test_export_without_data_keeps_the_identity(tmp_path):
    from src.models.profile import Profile
    from src.services.profile.transfer import export_to_zip

    data = tmp_path / "data"
    (data / "Default").mkdir(parents=True)
    for name in ("fingerprint.json", "chrome_persona.json", "geo_locale.json"):
        (data / name).write_text("{}", encoding="utf-8")
    (data / "Default" / "Cookies").write_text("x", encoding="utf-8")
    ok, zip_path = export_to_zip(Profile(name="alice"), str(data), str(tmp_path), False)
    assert ok
    names = set(zipfile.ZipFile(zip_path).namelist())
    assert {"data/fingerprint.json", "data/chrome_persona.json", "data/geo_locale.json"} <= names
    assert "data/Default/Cookies" not in names


def test_export_leaves_chrome_caches_behind(tmp_path):
    from src.models.profile import Profile
    from src.services.profile.transfer import export_to_zip

    data = tmp_path / "data"
    files = {
        "Default/Local Storage/leveldb/000003.log": "kept",
        "Default/Service Worker/Database/CURRENT": "kept",
        "Default/Service Worker/CacheStorage/abc/index": "cache",
        "Default/DawnWebGPUCache/data_0": "cache",
        "OptGuideOnDeviceModel/weights.bin": "cache",
        "ShaderCache/data_0": "cache",
    }
    for rel, text in files.items():
        (data / rel).parent.mkdir(parents=True, exist_ok=True)
        (data / rel).write_text(text, encoding="utf-8")
    ok, zip_path = export_to_zip(Profile(name="alice"), str(data), str(tmp_path))
    assert ok
    names = set(zipfile.ZipFile(zip_path).namelist())
    kept = {f"data/{rel}" for rel, text in files.items() if text == "kept"}
    assert {n for n in names if n.startswith("data/")} == kept


def _light_dir(root, cookies, **extra):
    from src.services.profile.transfer import pack_cookies

    files = {
        "fingerprint.json": "{}",
        "tabs.json": "[]",
        "sync.json": "{}",
        "History": "visits",
        "Default/Cache/data_0": "cache",
        "Default/Local Storage/leveldb/000003.log": "ls",
        "Default/IndexedDB/https_a.example_0.indexeddb.leveldb/000003.log": "idb a",
        "Default/IndexedDB/https_a.example_0.indexeddb.blob/1/00/1": "blob a",
        **extra,
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    pack_cookies(str(root), json.dumps([{"name": c} for c in cookies]))
    return root


def test_a_light_bundle_carries_the_sessions_and_the_sites_storage(tmp_path, monkeypatch):
    from src.models.profile import Profile
    from src.services.profile import transfer

    big = "Default/IndexedDB/https_big.example_0.indexeddb.leveldb/000003.log"
    data = _light_dir(tmp_path / "data", ["sid"], **{big: "x" * 2048})
    monkeypatch.setattr(transfer, "LIGHT_UNIT_MB", 1 / 1024)  # 1 KB: big.example stays here
    ok, zip_path = transfer.export_to_zip(Profile(name="p1"), str(data), str(tmp_path), light=True)
    assert ok
    with zipfile.ZipFile(zip_path) as z:
        names = {n for n in z.namelist() if n.startswith("data/")}
        assert json.loads(z.read("profile.json"))["bundle"] == transfer.LIGHT
    assert names == {
        "data/cookies.json.gz",
        "data/cookies.json.gz.sha256",
        "data/fingerprint.json",
        "data/tabs.json",
        "data/sync.json",
        "data/Default/Local Storage/leveldb/000003.log",
        "data/Default/IndexedDB/https_a.example_0.indexeddb.leveldb/000003.log",
        "data/Default/IndexedDB/https_a.example_0.indexeddb.blob/1/00/1",
    }


def test_a_light_bundle_replaces_only_what_it_carries(tmp_path):
    from src.models.profile import Profile
    from src.services.profile.transfer import (
        IntegrityError,
        export_to_zip,
        read_cookies,
        swap_in,
    )

    hub = tmp_path / "hub"
    _light_dir(
        hub / "p1",
        ["old"],
        **{
            "Default/Local Storage/leveldb/000001.log": "old ls",
            "Default/IndexedDB/https_b.example_0.indexeddb.leveldb/000003.log": "idb b",
        },
    )
    pc = _light_dir(tmp_path / "pc" / "p1", ["new"])
    (pc / "Default/IndexedDB/https_a.example_0.indexeddb.blob/1/00/1").unlink()
    (pc / "Default/IndexedDB/https_a.example_0.indexeddb.blob/1/00").rmdir()
    (pc / "Default/IndexedDB/https_a.example_0.indexeddb.blob/1").rmdir()
    (pc / "Default/IndexedDB/https_a.example_0.indexeddb.blob").rmdir()
    (pc / "History").write_text("pc visits", encoding="utf-8")
    ok, zip_path = export_to_zip(Profile(name="p1"), str(pc), str(tmp_path), light=True)
    assert ok

    swap_in(zip_path, str(tmp_path / "t1"), "p1", hub)
    p1 = hub / "p1"
    assert json.loads(read_cookies(str(p1))) == [{"name": "new"}]
    assert (p1 / "History").read_text() == "visits"  # not light: the hub's stays
    # Local Storage goes whole; site a goes whole (its blobs too); site b stays.
    assert not (p1 / "Default/Local Storage/leveldb/000001.log").exists()
    assert not (p1 / "Default/IndexedDB/https_a.example_0.indexeddb.blob").exists()
    assert (p1 / "Default/IndexedDB/https_b.example_0.indexeddb.leveldb/000003.log").exists()

    # Damaged cookies: nothing of the hub's copy is touched.
    (pc / "cookies.json.gz.sha256").write_text("0" * 64)
    (pc / "tabs.json").write_text('["https://bad.example"]')
    ok, zip_path = export_to_zip(Profile(name="p1"), str(pc), str(tmp_path), light=True)
    with pytest.raises(IntegrityError):
        swap_in(zip_path, str(tmp_path / "t3"), "p1", hub)
    assert (p1 / "tabs.json").read_text() == "[]"


def test_the_stamp_follows_the_light_bundle_only(tmp_path):
    from src.services.profile.transfer import data_stamp

    data = _light_dir(tmp_path / "p1", ["sid"])
    stamp = data_stamp(str(data))
    (data / "History").write_text("more visits", encoding="utf-8")
    assert data_stamp(str(data)) == stamp  # stays on each PC
    (data / "Default/Local Storage/leveldb/000003.log").write_text("ls changed")
    assert data_stamp(str(data)) != stamp
