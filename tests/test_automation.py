"""Steps (light RPA) and window sync, against a fake launcher."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from src.api import app as app_module
from src.api import auth
from src.api.app import create_app
from src.core.container import Container
from src.services.browser.launcher import BrowserControlError
from src.services.browser.steps import StepError, parse_script, run_steps, validate_steps
from src.services.browser.sync import SyncError, SyncManager


class FakeLauncher:
    """Records control calls; `fail` maps (name, action) to an error."""

    def __init__(self, running=("a", "b", "c")):
        self.running = set(running)
        self.calls = []
        self.fail = {}
        self.polls = []  # batches handed out by record_poll, then 409
        self._lock = threading.Lock()

    def is_running(self, name):
        return name in self.running

    def running_profile_names(self):
        return set(self.running)

    def control(self, name, action, params=None, timeout=60):
        with self._lock:
            self.calls.append((name, action, params))
        if (name, action) in self.fail:
            raise BrowserControlError(self.fail[name, action], status=422)
        if action == "record_poll":
            if self.polls:
                return {"recording": True, "steps": self.polls.pop(0)}
            raise BrowserControlError("Browser is not running", status=409)
        return {"url": "https://x.test/", "title": "T"}


# --- script parsing ------------------------------------------------------


def test_parse_script_fills_defaults_and_skips_comments():
    steps = parse_script(
        """
        // log in
        navigate example.com
        click text=Sign in
        type input[name="email"] | user@example.com
        press Enter | #email
        wait #dashboard
        sleep 250
        """
    )
    assert [s["action"] for s in steps] == [
        "navigate",
        "click",
        "type",
        "press",
        "wait_for",
        "sleep",
    ]
    assert steps[2]["params"]["selector"] == 'input[name="email"]'
    assert steps[2]["params"]["text"] == "user@example.com"
    assert steps[2]["params"]["clear"] is True
    assert steps[3]["params"] == {"key": "Enter", "selector": "#email"}
    assert steps[5]["params"] == {"ms": 250}


@pytest.mark.parametrize(
    ("script", "message"),
    [
        ("navigate a.com\nfly away", "Line 2: unknown action 'fly'"),
        ("type #email", "Line 1 (type): expected '<selector> | <text>'"),
        ("click", "Line 1 (click): missing argument"),
        ("sleep 600000", "Line 1 (sleep): ms"),
        ("click_at 10", "expected 'click_at X Y'"),
        ("// nothing", "No steps"),
        ("back\n" * 101, "At most 100 steps"),
    ],
)
def test_parse_script_errors_name_the_line(script, message):
    with pytest.raises(StepError, match=message.replace("(", r"\(").replace(")", r"\)")):
        parse_script(script)


def test_validate_steps():
    steps = validate_steps([{"action": "click", "params": {"selector": "#go"}}])
    assert steps == [{"action": "click", "params": {"selector": "#go", "timeout": 10_000}}]
    for bad in ([], [{"action": "screenshot"}], [{"action": "click", "params": {}}], ["click"]):
        with pytest.raises(StepError):
            validate_steps(bad)


# --- running -------------------------------------------------------------


def test_run_steps_isolates_a_failing_profile():
    bl = FakeLauncher()
    bl.fail["b", "click"] = "Timeout"
    steps = parse_script("navigate a.com\nclick #go\nback")
    results = run_steps(bl, ["a", "b"], steps)
    assert results["a"] == {"ok": True, "done": 3, "error": None}
    assert results["b"] == {"ok": False, "done": 1, "error": "step 2 (click): Timeout"}
    assert [c[1] for c in bl.calls if c[0] == "b"] == ["navigate", "click"]  # stopped


def test_run_steps_can_go_on_after_errors():
    bl = FakeLauncher()
    bl.fail["a", "click"] = "Timeout"
    results = run_steps(bl, ["a"], parse_script("click #x\nback"), stop_on_error=False)
    assert results["a"]["done"] == 1 and not results["a"]["ok"]


def test_sleep_waits_without_calling_the_browser():
    bl = FakeLauncher()
    start = time.monotonic()
    run_steps(bl, ["a"], parse_script("sleep 50"))
    assert time.monotonic() - start >= 0.05 and bl.calls == []


# --- sync ----------------------------------------------------------------


def _wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_sync_replays_leader_steps_on_followers_until_the_leader_closes():
    bl = FakeLauncher()
    step = {"action": "replay", "params": {"step": {}, "origin": "https://x.test"}}
    bl.polls = [[step], [], [step]]
    changes = []
    sync = SyncManager(bl, lambda: changes.append(1))
    session = sync.start("a", ["b", "c", "a"])
    assert session.followers == ["b", "c"]  # the leader never follows itself
    _wait_for(lambda: not sync.sessions())
    assert session.replayed == 2
    replays = [c[0] for c in bl.calls if c[1] == "replay"]
    assert sorted(replays) == ["b", "b", "c", "c"]
    assert ("a", "record_start", None) in bl.calls
    assert session.ended_reason.startswith("leader:")
    assert len(changes) == 2  # started, ended


def test_sync_keeps_follower_errors_and_drops_stopped_followers():
    bl = FakeLauncher()
    step = {"action": "replay", "params": {}}
    bl.fail["b", "replay"] = "Not replayed"
    bl.polls = [[step]]
    sync = SyncManager(bl)
    session = sync.start("a", ["b", "c"])
    _wait_for(lambda: not sync.sessions())
    assert "Not replayed" in session.errors["b"]

    bl.polls = [[step]]
    bl.running.discard("c")
    bl.running.discard("b")
    session = sync.start("a", ["b", "c"])
    _wait_for(lambda: not sync.sessions())
    assert session.ended_reason == "no follower is running"


def test_a_profile_is_in_one_sync_at_a_time():
    bl = FakeLauncher(running=("a", "b", "c", "d"))
    gate = threading.Event()
    real_control = bl.control

    def control(name, action, params=None, timeout=60):
        if action == "record_poll":
            gate.wait(5)
            return {"recording": False, "steps": []}
        return real_control(name, action, params, timeout)

    bl.control = control
    sync = SyncManager(bl)
    sync.start("a", ["b"])
    with pytest.raises(SyncError, match="Already syncing: a, b"):
        sync.start("b", ["a"])  # would replay its own steps back
    with pytest.raises(SyncError, match="at least one follower"):
        sync.start("c", ["c"])
    assert sync.stop("a") and not sync.stop("a")
    gate.set()
    sync.start("b", ["d"])  # free again


def test_sync_start_failure_leaves_no_session():
    bl = FakeLauncher()
    bl.fail["a", "record_start"] = "boom"
    sync = SyncManager(bl)
    with pytest.raises(BrowserControlError):
        sync.start("a", ["b"])
    assert sync.sessions() == []


# --- API -----------------------------------------------------------------


TOKEN = "t" * 32


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auth, "API_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "API_TOKEN", TOKEN)
    container = Container(hub=False)
    bl = FakeLauncher(running=("a", "b"))
    container._instances["bl"] = bl
    client = TestClient(create_app(container), headers={"Authorization": f"Bearer {TOKEN}"})
    for name in ("a", "b", "c"):
        assert client.post("/api/v1/profiles", json={"name": name}).status_code == 201
    return client, bl


def test_api_run(api):
    client, bl = api
    r = client.post(
        "/api/v1/automation/run", json={"profiles": ["a", "b", "a"], "script": "click #go"}
    )
    assert r.status_code == 200, r.text
    assert set(r.json()["results"]) == {"a", "b"}
    r = client.post(
        "/api/v1/automation/run",
        json={"profiles": ["a"], "steps": [{"action": "back", "params": {}}]},
    )
    assert r.json()["results"]["a"]["ok"]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"profiles": ["a"], "script": "fly"}, 422),
        ({"profiles": ["a"]}, 422),  # no steps
        ({"profiles": ["c"], "script": "back"}, 409),  # not running
        ({"profiles": ["zz"], "script": "back"}, 404),
        ({"profiles": [], "script": "back"}, 422),
    ],
)
def test_api_run_rejects(api, body, status):
    client, _ = api
    assert client.post("/api/v1/automation/run", json=body).status_code == status


def test_api_sync_lifecycle(api):
    client, bl = api
    gate = threading.Event()
    real_control = bl.control

    def control(name, action, params=None, timeout=60):
        if action == "record_poll":
            gate.wait(5)
            return {"recording": False, "steps": []}
        return real_control(name, action, params, timeout)

    bl.control = control
    try:
        assert (
            client.post(
                "/api/v1/automation/sync", json={"leader": "a", "followers": ["c"]}
            ).status_code
            == 409
        )  # c is not running
        r = client.post("/api/v1/automation/sync", json={"leader": "a", "followers": ["b"]})
        assert r.status_code == 201, r.text
        assert r.json()["followers"] == ["b"]
        assert client.get("/api/v1/automation/sync").json()["sessions"][0]["leader"] == "a"
        again = client.post("/api/v1/automation/sync", json={"leader": "b", "followers": ["a"]})
        assert again.status_code == 409
        assert client.delete("/api/v1/automation/sync/a").status_code == 200
        assert client.delete("/api/v1/automation/sync/a").status_code == 404
        assert client.get("/api/v1/automation/sync").json()["sessions"] == []
    finally:
        gate.set()
