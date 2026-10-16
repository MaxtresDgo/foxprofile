"""Browser sign-in for the PC agent (loopback redirect, as in RFC 8252).

While a sign-in is pending the agent serves 127.0.0.1:<random port> and opens the
hub's panel at `#agent=<port>&state=<state>&pc=<host>`. The operator logs in there,
or is already logged in, and presses Authorize: the panel creates an agent key and
POSTs it to /callback here as a form. The key therefore only reaches this PC, and
only with the random `state` that nobody but this PC and that browser tab has seen.
When the agent does not know the hub yet, /setup asks for its URL first.
"""

from __future__ import annotations

import hmac
import html
import secrets
import socket
import threading
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..core.strings import get_string

MAX_FORM = 16 * 1024


def normalize_hub_url(url: str) -> str | None:
    url = url.strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return url


class SignIn:
    """One pending sign-in. `check_hub(url)` returns an error message or None;
    `who_is(hub, key)` returns {"name", "role"} for a key, or raises."""

    def __init__(
        self,
        hub_url: str,
        check_hub: Callable[[str], str | None],
        who_is: Callable[[str, str], dict],
    ) -> None:
        self.hub = hub_url
        self.check_hub, self.who_is = check_hub, who_is
        self.state = secrets.token_urlsafe(32)
        self.result: dict | None = None
        self._done = threading.Event()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.signin = self  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def local(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        """Where the operator's browser goes to sign in."""
        if not self.hub:
            return f"http://{self.local}/setup?state={self.state}"
        fragment = urllib.parse.urlencode(
            {"agent": self.port, "state": self.state, "pc": socket.gethostname()}
        )
        return f"{self.hub}/#{fragment}"

    @property
    def finished(self) -> bool:
        return self._done.is_set()

    def wait(self, timeout: float | None = None) -> dict | None:
        """{"hub", "user", "key"} once authorized; None if closed first."""
        self._done.wait(timeout)
        return self.result

    def close(self) -> None:
        if not self._done.is_set():
            self._done.set()
            threading.Thread(target=self._server.shutdown, daemon=True).start()

    def _authorized(self, result: dict) -> None:
        self.result = result
        self.close()


class _Handler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer

    def log_message(self, *args) -> None:  # the agent logs what matters itself
        pass

    @property
    def signin(self) -> SignIn:
        return self.server.signin  # type: ignore[attr-defined]

    def _allowed(self, state: str) -> bool:
        # A foreign Host means DNS rebinding; a wrong state means another site (or
        # tab) trying to plant its own key or hub on this agent.
        if self.headers.get("Host") != self.signin.local:
            self._send(421, "")
            return False
        if not hmac.compare_digest(state.encode(), self.signin.state.encode()):
            self._send(403, _message(get_string("agent_web_expired")))
            return False
        return True

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        state = urllib.parse.parse_qs(query).get("state", [""])[0]
        if path != "/setup":
            self._send(404, "")
        elif self._allowed(state):
            self._send(200, _setup_page(self.signin.state, self.signin.hub))

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_FORM:
            self._send(413, "")
            return
        form = urllib.parse.parse_qs(self.rfile.read(length).decode(errors="replace"))
        field = lambda k: form.get(k, [""])[0]  # noqa: E731
        if not self._allowed(field("state")):
            return
        signin = self.signin
        if self.path == "/setup":
            url = normalize_hub_url(field("hub"))
            error = get_string("agent_web_bad_url") if url is None else signin.check_hub(url)
            if error:
                self._send(400, _setup_page(signin.state, field("hub"), error))
                return
            signin.hub = url
            self._redirect(signin.url)
        elif self.path == "/callback" and signin.hub:
            key = field("key")
            try:
                user = signin.who_is(signin.hub, key)
            except Exception as e:
                self._send(400, _message(get_string("agent_web_key_refused", error=e)))
                return
            self._redirect(f"{signin.hub}/#agent-done")
            signin._authorized({"hub": signin.hub, "user": user["name"], "key": key})
        else:
            self._send(404, "")

    def _redirect(self, url: str) -> None:
        self.send_response(303)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, status: int, body: str) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


# Same tokens as the panel's :root (src/web/index.html), light theme only.
_STYLE = """
:root { --paper: #F3F4EF; --paper-raised: #FBFBF8; --rule: #D5DACF; --ink: #1E2422;
  --ink-2: #48514C; --act: #C2510F; --act-hover: #A84408; --err: #B42330; --cover: #17332E;
  --ui: "Be Vietnam Pro", system-ui, -apple-system, "Segoe UI", sans-serif; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 16px;
  box-sizing: border-box; background: var(--paper); color: var(--ink); font: 14px/1.5 var(--ui); }
main { width: 100%; max-width: 400px; background: var(--paper-raised); border: 1px solid var(--rule);
  border-radius: 8px; padding: 28px; }
h1 { font-size: 20px; margin: 0 0 6px; } p { color: var(--ink-2); margin: 0 0 18px; }
label { display: block; font-weight: 600; margin-bottom: 6px; }
input { width: 100%; box-sizing: border-box; font: inherit; padding: 9px 11px;
  border: 1px solid var(--rule); border-radius: 6px; background: #fff; color: var(--ink); }
input:focus { outline: 2px solid var(--cover); outline-offset: 1px; }
.err { color: var(--err); margin: 10px 0 0; min-height: 1em; }
button { margin-top: 16px; width: 100%; font: inherit; font-weight: 600; padding: 10px;
  border: 0; border-radius: 6px; background: var(--act); color: #fff; cursor: pointer; }
button:hover { background: var(--act-hover); }
"""


def _page(body: str) -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>FoxProfile Agent</title><style>{_STYLE}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )


def _message(text: str) -> str:
    return _page(f"<p>{html.escape(text)}</p>")


def _setup_page(state: str, hub: str, error: str = "") -> str:
    e = html.escape
    return _page(
        f'<form method="post" action="/setup">'
        f"<h1>{e(get_string('agent_web_setup_title'))}</h1>"
        f"<p>{e(get_string('agent_web_setup_hint'))}</p>"
        f'<label for="hub">{e(get_string("agent_web_setup_url"))}</label>'
        f'<input id="hub" name="hub" type="url" required autofocus value="{e(hub)}"'
        ' placeholder="https://hub.example.com">'
        f'<input type="hidden" name="state" value="{e(state)}">'
        f'<div class="err" role="alert">{e(error)}</div>'
        f"<button>{e(get_string('agent_web_setup_next'))}</button></form>"
    )
