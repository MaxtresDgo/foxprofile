"""Bulk edit in the desktop app: only the ticked fields change, per profile."""

from src.services.profile.manager import ProfileManager
from src.ui.actions.bulk import bulk_edit_profiles


def test_bulk_edit_changes_only_the_given_fields(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pm = ProfileManager()
    pm.add_profile("a", "192.0.2.1:8080", "windows", "Europe/Paris", "fr-FR")
    pm.add_profile("b", "", "macos", engine="chrome")
    pm.add_profile("c", "", "linux")
    logs = []

    bulk_edit_profiles(
        ["a", "b"], {"proxy": "198.51.100.2:3128", "locale": ""}, pm, logs.append, lambda: None
    )
    a, b = pm.profiles["a"], pm.profiles["b"]
    assert (a.proxy, a.os_type, a.timezone, a.locale) == (
        "198.51.100.2:3128",
        "windows",
        "Europe/Paris",
        None,
    )
    assert (b.proxy, b.os_type, b.engine) == ("198.51.100.2:3128", "macos", "chrome")
    assert pm.profiles["c"].proxy in (None, "")

    # Android needs the Chrome engine: the Camoufox profile is skipped, the other changes.
    bulk_edit_profiles(["b", "c"], {"os_type": "android"}, pm, logs.append, lambda: None)
    assert pm.profiles["b"].os_type == "android" and pm.profiles["c"].os_type == "linux"
    assert any(line.startswith("c: ") for line in logs)
