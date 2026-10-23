"""The agent's browser sign-in: only the tab holding `state` can hand it a key or a hub."""

import http.client
import urllib.parse

import pytest

from src.hub.agent_auth import SignIn, normalize_hub_url

HUB = "https://hub.example.com"


def _who_is(hub, key):
    if key != "fpk_good":
        raise ValueError("HTTP 401")
    return {"name": "ana", "role": "operator", "display": "Ana López"}


@pytest.fixture
def signin():
    s = SignIn(HUB, check_hub=lambda url: None, who_is=_who_is)
    yield s
    s.close()


def _req(s, method, path, form=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", s.port, timeout=5)
    body = urllib.parse.urlencode(form) if form is not None else None
    headers = {"Host": host or s.local}
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=body, headers=headers)
    r = conn.getresponse()
    r.read()
    conn.close()
    return r


def test_authorize_url_points_at_the_hub_panel(signin):
    fragment = urllib.parse.parse_qs(signin.url.split("#", 1)[1])
    assert signin.url.startswith(HUB + "/#")
    assert fragment["agent"] == [str(signin.port)] and fragment["state"] == [signin.state]


def test_callback_needs_the_state_the_local_host_and_a_good_key(signin):
    good = {"state": signin.state, "key": "fpk_good"}
    assert _req(signin, "POST", "/callback", {**good, "state": "x" * 43}).status == 403
    assert _req(signin, "POST", "/callback", good, host="evil.example:80").status == 421
    assert _req(signin, "POST", "/callback", {**good, "key": "fpk_bad"}).status == 400
    assert not signin.finished

    r = _req(signin, "POST", "/callback", good)
    assert r.status == 303 and r.getheader("Location") == HUB + "/#agent-done"
    assert signin.wait(5) == {"hub": HUB, "user": "ana", "display": "Ana López", "key": "fpk_good"}


def test_callback_trades_a_one_time_code_for_the_key():
    redeemed = []

    def redeem(hub, code):
        redeemed.append((hub, code))
        if code != "c1":
            raise ValueError("HTTP 400")
        return "fpk_good"

    s = SignIn(HUB, check_hub=lambda url: None, who_is=_who_is, redeem=redeem)
    try:
        # The panel learns from the fragment that it can send a code.
        assert "flow=code" in s.url.split("#", 1)[1]
        q = lambda **kw: "/callback?" + urllib.parse.urlencode(kw)  # noqa: E731
        assert _req(s, "GET", q(state="x" * 43, code="c1")).status == 403
        assert _req(s, "GET", q(state=s.state, code="c1"), host="evil.example:80").status == 421
        assert _req(s, "GET", q(state=s.state, code="c0")).status == 400
        assert redeemed == [(HUB, "c0")] and not s.finished
        r = _req(s, "GET", q(state=s.state, code="c1"))
        assert r.status == 303 and r.getheader("Location") == HUB + "/#agent-done"
        assert s.wait(5)["key"] == "fpk_good"
    finally:
        s.close()


def test_setup_asks_for_the_hub_first():
    checked = []
    s = SignIn("", check_hub=lambda url: checked.append(url), who_is=_who_is)
    try:
        assert s.url == f"http://{s.local}/setup?state={s.state}"
        assert _req(s, "GET", "/setup?state=nope").status == 403
        assert _req(s, "GET", f"/setup?state={s.state}").status == 200
        assert _req(s, "POST", "/setup", {"state": s.state, "hub": "ftp://x"}).status == 400
        # Without a hub there is nowhere to authorize yet.
        assert _req(s, "POST", "/callback", {"state": s.state, "key": "fpk_good"}).status == 404

        r = _req(s, "POST", "/setup", {"state": s.state, "hub": HUB + "/"})
        assert checked == [HUB] and s.hub == HUB
        assert r.status == 303 and r.getheader("Location") == s.url
    finally:
        s.close()


def test_closing_ends_the_wait_without_a_result(signin):
    signin.close()
    assert signin.wait(5) is None


def test_normalize_hub_url():
    assert normalize_hub_url(" https://hub.example.com/ ") == HUB
    assert normalize_hub_url("hub.example.com") is None
    assert normalize_hub_url("javascript:alert(1)") is None


def test_a_dev_agent_can_be_asked_for_another_hub():
    s = SignIn(HUB, check_hub=lambda url: None, who_is=_who_is, ask_hub=True)
    try:
        assert s.url == f"http://{s.local}/setup?state={s.state}"
        r = _req(s, "POST", "/setup", {"state": s.state, "hub": "https://dev-hub.example.com"})
        assert s.hub == "https://dev-hub.example.com"
        assert r.getheader("Location").startswith("https://dev-hub.example.com/#")
    finally:
        s.close()
