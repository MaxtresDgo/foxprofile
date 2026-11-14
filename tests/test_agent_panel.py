"""The agent's local panel: loopback only, token and Host checked, offline mode only."""

import http.client
import json
import time
import types

from src.agent_panel import TOKEN_HEADER, LocalPanel


class FakeAgent:
    def __init__(self):
        self.online = False
        self.hub = types.SimpleNamespace(key="fpk_x", url="https://hub.example.com", label="Ana")
        self.calls = []
        self.state = "idle"

    def local_profiles(self):
        return [
            {"name": "p1", "state": self.state, "copy": 1.0, "engine": "chrome", "os": "android"}
        ]

    def open_offline(self, name):
        self.calls.append(("open", name))

    def stop(self, name, said):
        self.calls.append(("stop", name, said))


def _call(panel, method, path, token=None, host=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", panel.port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{panel.port}"}
    if token:
        headers[TOKEN_HEADER] = token
    conn.request(method, path, json.dumps(body) if body else None, headers)
    r = conn.getresponse()
    return r.status, r.read()


def test_the_local_panel_opens_copies_only_while_the_hub_is_down(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = FakeAgent()
    panel = LocalPanel(agent, port=0)
    panel.start()
    try:
        assert _call(panel, "GET", "/")[0] == 200
        # No token, someone else's token or a rebinding Host: refused.
        assert _call(panel, "GET", "/api/state")[0] == 403
        assert _call(panel, "GET", "/api/state", token="x" * 43)[0] == 403
        assert _call(panel, "GET", "/api/state", panel.token, host="evil.example")[0] == 403
        status, body = _call(panel, "GET", "/api/state", panel.token)
        assert status == 200 and json.loads(body)["profiles"][0]["name"] == "p1"

        # Offline mode only: while the hub answers, nothing is listed or done here.
        agent.online = True
        status, body = _call(panel, "GET", "/api/state", panel.token)
        assert status == 409 and "profiles" not in json.loads(body)
        assert _call(panel, "POST", "/api/open", panel.token, body={"name": "p1"})[0] == 409
        agent.online = False
        assert _call(panel, "POST", "/api/open", panel.token, body={"name": "p1"})[0] == 200
        assert _call(panel, "POST", "/api/open", panel.token, body={"name": "nope"})[0] == 400
        agent.state = "live"
        assert _call(panel, "POST", "/api/stop", panel.token, body={"name": "p1"})[0] == 200
        time.sleep(0.2)  # both run in their own thread
        assert agent.calls == [("open", "p1"), ("stop", "p1", "agent_stopped_local")]

        # The token survives a restart (an update restarts the agent).
        assert LocalPanel(agent, port=0).token == panel.token
    finally:
        panel.close()
