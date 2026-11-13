"""The agent's browser wall: which window is whose, the tiles, and what they send."""

import io
import json

from src.agent import BrowserWall
from src.agent_wall import (
    clip,
    columns,
    common_shape,
    descendants,
    fit,
    key_action,
    match,
    to_page,
)
from src.services.browser.launcher import BrowserControlError


def test_windows_are_found_below_each_runner():
    # runner 10 -> driver 11 -> chrome 12 -> gpu 13; runner 20 -> firefox 21
    parents = {10: 1, 11: 10, 12: 11, 13: 12, 20: 1, 21: 20, 99: 1}
    assert descendants([10, 20], parents) == {10: 10, 11: 10, 12: 10, 13: 10, 20: 20, 21: 20}
    windows = [(500, 99), (501, 13), (502, 12), (503, 21)]  # front first; 99 is not ours
    assert match({"a": 10, "b": 20, "c": 30}, parents, windows) == {"a": 501, "b": 503}


def test_a_process_that_is_its_own_parent_does_not_loop():
    assert descendants([0], {0: 0, 4: 0}) == {0: 0, 4: 0}


def test_fit_keeps_the_window_shape():
    assert fit(400, 800, (0, 0, 300, 300)) == (75, 0, 225, 300)
    assert fit(1600, 900, (10, 10, 320, 360)) == (10, 100, 330, 280)
    assert fit(0, 0, (0, 0, 50, 40)) == (0, 0, 50, 40)


def test_a_tile_scrolled_half_out_shows_only_its_visible_part():
    source = (0, 100, 400, 900)  # the page in the browser window
    assert clip((0, 0, 200, 400), (0, 0, 1000, 1000), source) == ((0, 0, 200, 400), source)
    # its top half is above the wall's scroll area
    assert clip((0, -200, 200, 200), (0, 0, 1000, 1000), source) == (
        (0, 0, 200, 200),
        (0, 500, 400, 900),
    )
    assert clip((0, 1200, 200, 1600), (0, 0, 1000, 1000), source) is None


def test_a_point_on_the_tile_becomes_page_coordinates():
    shown = (10, 0, 210, 400)  # the picture, letterboxed in its frame
    assert to_page(110, 200, shown, (412, 824)) == (206.0, 412.0)
    assert to_page(0, 500, shown, (412, 824)) == (0.0, 824.0)  # clamped to the page


def test_keys():
    assert key_action("a", "a", 0) == ("type", "a")
    assert key_action("ntilde", "ñ", 0) == ("type", "ñ")
    assert key_action("??", "é", 0) == ("type", "é")  # a character Tk has no name for
    assert key_action("at", "@", 0x4 | 0x20000) == ("type", "@")  # AltGr
    assert key_action("Return", "\r", 0) == ("press", "Enter")
    assert key_action("BackSpace", "\b", 0) == ("press", "Backspace")
    assert key_action("Tab", "\t", 0x1) == ("press", "Shift+Tab")
    assert key_action("a", "\x01", 0x4) == ("press", "Control+a")
    assert key_action("v", "\x16", 0x4) == ("paste", "")
    assert key_action("Shift_L", "", 0x1) is None
    assert key_action("??", "", 0) is None


def test_columns():
    assert columns(1000, 300, 12) == 3
    assert columns(200, 300, 12) == 1


class FakeProc:
    def __init__(self, polls):
        self.stdin = io.StringIO()
        self.polls = polls

    def poll(self):
        return None if self.polls.pop(0) else 0


class FakeLauncher:
    def __init__(self, states=None):
        self.states = states or [{}]
        self.calls = []

    def session_pids(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def control(self, name, action, params, timeout=60):
        self.calls.append((name, action, params))
        if name == "gone":
            raise BrowserControlError("not running", status=409)
        return {"url": "https://example.test/"}


def test_the_wall_is_told_only_when_the_browsers_change(monkeypatch):
    monkeypatch.setattr("src.agent.time.sleep", lambda s: None)
    wall = BrowserWall(FakeLauncher([{"a": 1}, {"a": 1}, {"a": 1, "b": 2}]))
    proc = FakeProc([True, True, True, False])
    wall._feed(proc)
    lines = [json.loads(x) for x in proc.stdin.getvalue().splitlines()]
    assert lines == [{"browsers": {"a": 1}}, {"browsers": {"a": 1, "b": 2}}]


def test_the_wall_runs_page_actions_and_nothing_else():
    launcher = FakeLauncher()
    wall, proc = BrowserWall(launcher), FakeProc([])
    wall._run(proc, {"id": 1, "name": "a", "action": "click_at", "params": {"x": 1, "y": 2}})
    wall._run(proc, {"id": 2, "name": "a", "action": "evaluate", "params": {"script": "1"}})
    assert "tab_close" in BrowserWall.ACTIONS
    wall._run(proc, {"id": 3, "name": "gone", "action": "back", "params": {}})
    replies = [json.loads(x) for x in proc.stdin.getvalue().splitlines()]
    assert replies[0] == {"reply": 1, "ok": True, "result": {"url": "https://example.test/"}}
    assert replies[1]["reply"] == 2 and not replies[1]["ok"]
    assert replies[2] == {"reply": 3, "ok": False, "error": "not running"}
    assert launcher.calls == [("a", "click_at", {"x": 1, "y": 2}), ("gone", "back", {})]


def test_tiles_of_a_kind_share_one_shape():
    shapes = [2.05, 1.98, 2.17, 0.52, 0.6, None]  # three phone models, two desktops, one unknown
    assert common_shape(1.98, shapes) == 2.17
    assert common_shape(0.52, shapes) == 0.6
    assert common_shape(None, shapes) == 10 / 16


def test_the_tile_close_button_stops_the_browser_like_the_panel():
    stopped = []
    launcher = FakeLauncher([{"a": 1}])
    wall, proc = BrowserWall(launcher, stop=stopped.append), FakeProc([])
    wall._run(proc, {"id": 1, "name": "a", "action": "stop", "params": {}})
    wall._run(proc, {"id": 2, "name": "b", "action": "stop", "params": {}})  # not running here
    replies = [json.loads(x) for x in proc.stdin.getvalue().splitlines()]
    assert stopped == ["a"]
    assert replies[0] == {"reply": 1, "ok": True, "result": {}}
    assert replies[1]["reply"] == 2 and not replies[1]["ok"]
    assert launcher.calls == []  # not a page action
