"""Hub sign-in against the identity platform (src/hub/platform_auth.py)."""

import io
import json
import urllib.error

import pytest

from src.hub import platform_auth


def test_permissions_match_segment_by_segment_with_wildcards():
    has = platform_auth.has_permission
    assert has(["foxprofile:admin"], "foxprofile:admin")
    assert has(["*:*"], "foxprofile:admin")
    assert has(["foxprofile:*"], "foxprofile:admin")
    assert not has(["foxprofile:access"], "foxprofile:admin")
    assert not has(["*"], "foxprofile:admin")
    assert not has([], "foxprofile:admin")


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def platform(monkeypatch):
    """Answers the next login with (status, body); records the request."""
    monkeypatch.setattr(platform_auth.config, "AUTH_URL", "https://api.example.com")
    monkeypatch.setattr(platform_auth.config, "AUTH_ORIGIN", "https://hub.example.com")
    sent = {}

    def answer(status, body):
        def urlopen(req, timeout):
            sent.update(url=req.full_url, origin=req.get_header("Origin"), body=req.data)
            data = json.dumps(body).encode()
            if status >= 400:
                raise urllib.error.HTTPError(req.full_url, status, "", {}, io.BytesIO(data))
            return _Answer(data)

        monkeypatch.setattr(platform_auth.urllib.request, "urlopen", urlopen)

    return answer, sent


def test_a_good_login_returns_the_email_and_permissions(platform):
    answer, sent = platform
    answer(
        201,
        {
            "access_token": "x",
            "user": {
                "email": "Ana@Example.com ",
                "name": " Ana López",
                "role": {"permisos": ["foxprofile:access"]},
            },
        },
    )
    user = platform_auth.authenticate(" ana@example.com", "pw")
    assert user == {
        "email": "ana@example.com",
        "name": "Ana López",
        "permisos": ["foxprofile:access"],
    }
    assert sent["url"] == "https://api.example.com/auth/login"
    assert sent["origin"] == "https://hub.example.com"
    assert json.loads(sent["body"]) == {"email": "ana@example.com", "password": "pw"}


@pytest.mark.parametrize(
    ("status", "body", "answer_status"),
    [
        (401, {"message": "Credenciales inválidas"}, 401),
        (401, {"motivo": "espera_temporal", "segundosRestantes": 150}, 429),
        (403, {"message": "Forbidden"}, 403),
        (500, {}, 502),
    ],
)
def test_refusals_map_to_our_own_errors(platform, status, body, answer_status):
    answer, _ = platform
    answer(status, body)
    with pytest.raises(platform_auth.LoginError) as e:
        platform_auth.authenticate("ana@example.com", "pw")
    assert e.value.status == answer_status
    if answer_status == 429:
        assert "3" in str(e.value)  # 150 s rounds up to 3 minutes


def test_a_proxy_page_is_not_a_missing_permission(platform, monkeypatch):
    # Cloudflare answers 403 with text or HTML, not the platform's JSON.
    def blocked(req, timeout):
        assert req.get_header("User-agent") == "FoxProfile-Hub"
        page = io.BytesIO(b"error code: 1010")
        raise urllib.error.HTTPError(req.full_url, 403, "", {}, page)

    monkeypatch.setattr(platform_auth.urllib.request, "urlopen", blocked)
    with pytest.raises(platform_auth.LoginError) as e:
        platform_auth.authenticate("ana@example.com", "pw")
    assert e.value.status == 502


def test_an_unreachable_platform_is_a_502(platform, monkeypatch):
    def down(req, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(platform_auth.urllib.request, "urlopen", down)
    with pytest.raises(platform_auth.LoginError) as e:
        platform_auth.authenticate("ana@example.com", "pw")
    assert e.value.status == 502
