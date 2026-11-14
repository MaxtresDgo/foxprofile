"""The agent's local panel: a small page on this PC (127.0.0.1) to open profiles
from their local copy and stop them while the hub is degraded or unreachable.

Loopback only, and every /api call carries the token the tray's "Local panel"
item puts in the page's address (#t=...), checked with the Host header so no
website can drive it (DNS rebinding, CSRF: a custom header needs a preflight
that this server never allows). Standard library only: the installers ship it.
"""

import http.server
import json
import pathlib
import secrets
import threading
import urllib.parse

from src.core.strings import current_language, get_string

PORT = 47811  # fixed, so a bookmark keeps working; the next free one when taken
TOKEN_FILE = pathlib.Path("local-panel.json")
TOKEN_HEADER = "X-Panel-Token"
_TEXT = [
    "lp_title",
    "lp_online",
    "lp_open_hub",
    "lp_offline",
    "lp_signed_out",
    "lp_profile",
    "lp_state",
    "lp_copy",
    "lp_live",
    "lp_opening",
    "lp_syncing",
    "lp_pending",
    "lp_idle",
    "lp_open",
    "lp_stop",
    "lp_none",
    "lp_no_copy",
    "lp_bad_token",
    "lp_failed",
]


def _token() -> str:
    """Kept across restarts (an update restarts the agent): bookmarks survive."""
    try:
        token = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))["token"]
        if isinstance(token, str) and len(token) >= 32:
            return token
    except (OSError, ValueError, KeyError, TypeError):
        pass
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(json.dumps({"token": token}), encoding="utf-8")
    return token


class LocalPanel:
    def __init__(self, agent, port: int = PORT) -> None:
        self.agent = agent
        self.token = _token()
        panel = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # the agent's log is for syncs
                pass

            def _host_ok(self) -> bool:
                port = self.server.server_address[1]
                return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

            def _send(self, status: int, body: bytes, kind: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, data) -> None:
                self._send(status, json.dumps(data).encode(), "application/json")

            def _allowed(self) -> bool:
                token = self.headers.get(TOKEN_HEADER, "")
                if self._host_ok() and secrets.compare_digest(token, panel.token):
                    return True
                self._json(403, {"error": get_string("lp_bad_token")})
                return False

            def do_GET(self) -> None:
                path = urllib.parse.urlsplit(self.path).path
                if not self._host_ok():
                    self._send(403, b"", "text/plain")
                elif path == "/":
                    self._send(200, panel.page().encode(), "text/html; charset=utf-8")
                elif path != "/api/state":
                    self._send(404, b"", "text/plain")
                elif self._allowed():
                    self._json(200, panel.state())

            def do_POST(self) -> None:
                if not self._allowed():
                    return
                try:
                    length = min(int(self.headers.get("Content-Length") or 0), 4096)
                    body = json.loads(self.rfile.read(length) or b"{}")
                    name = str(body["name"])
                except (ValueError, KeyError, TypeError):
                    return self._json(400, {"error": "bad request"})
                action = urllib.parse.urlsplit(self.path).path
                error = panel.act(action, name)
                self._json(400 if error else 200, {"error": error} if error else {"ok": True})

        self.server = None
        for candidate in (port, port + 1, port + 2, 0):
            try:
                self.server = http.server.ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
                break
            except OSError:
                continue
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/#t={self.token}"

    def start(self) -> None:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()

    def state(self) -> dict:
        a = self.agent
        return {
            "online": a.online,
            "signed_in": bool(a.hub.key),
            "hub": a.hub.url,
            "user": a.hub.label,
            "profiles": a.local_profiles(),
        }

    def act(self, action: str, name: str) -> str | None:
        """Start an open or a stop; an error message, or None once it is under way."""
        a = self.agent
        known = {p["name"]: p for p in a.local_profiles()}
        if name not in known:
            return get_string("lp_failed")
        if action == "/api/open":
            if a.online or not known[name]["copy"] or known[name]["state"] != "idle":
                return get_string("lp_failed")
            target, args = a.open_offline, (name,)
        elif action == "/api/stop":
            if known[name]["state"] != "live":
                return get_string("lp_failed")
            target, args = a.stop, (name, "agent_stopped_local")
        else:
            return "unknown action"
        threading.Thread(target=target, args=args, daemon=True).start()
        return None

    def page(self) -> str:
        text = {key: get_string(key) for key in _TEXT}
        return PAGE.replace("__TEXT__", json.dumps(text)).replace("__LANG__", current_language())


# The hub panel's tokens (DESIGN.md), a minimal subset. No external requests.
PAGE = """<!doctype html>
<html lang="__LANG__"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FoxProfile Agent</title>
<style>
:root { --paper:#F3F4EF; --paper-raised:#FBFBF8; --rule:#D5DACF; --rule-strong:#BCC3B6;
  --ink:#1E2422; --ink-2:#48514C; --ink-3:#66706A; --cover:#17332E; --cover-ink:#E8EFEA;
  --act:#C2510F; --act-hover:#A84408; --live:#5B3FA8; --err:#B42330; --warn:#8A5A00;
  --warn-tint:#F6EEDB;
  --sans:"Be Vietnam Pro", system-ui, -apple-system, "Segoe UI", sans-serif;
  --data:"JetBrains Mono", ui-monospace, "SF Mono", Consolas, monospace; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font: 14px/1.5 var(--sans); }
header { background: var(--cover); color: var(--cover-ink); padding: 14px 24px; }
header h1 { margin: 0; font-size: 19px; letter-spacing: -.01em; }
header p { margin: 2px 0 0; font: 12.5px/1.35 var(--data); opacity: .8; }
main { max-width: 860px; margin: 0 auto; padding: 20px 24px 40px; }
.note { padding: 12px 14px; border-radius: 8px; border: 1px solid var(--rule); background: var(--paper-raised); }
.note.warn { background: var(--warn-tint); border-color: var(--warn); color: var(--ink); }
.note.err { border-color: var(--err); color: var(--err); }
table { width: 100%; border-collapse: collapse; margin-top: 18px; background: var(--paper-raised);
  border: 1px solid var(--rule); border-radius: 8px; overflow: hidden; }
th { text-align: left; font: 600 12px/1.35 var(--sans); color: var(--ink-3); padding: 9px 14px;
  border-bottom: 1px solid var(--rule-strong); }
td { padding: 10px 14px; border-top: 1px solid var(--rule); vertical-align: middle; }
td b { display: block; font-weight: 600; }
.minor { color: var(--ink-3); font: 11.5px/1.35 var(--data); }
.stamp { display: inline-block; min-width: 86px; padding: 3px 9px 2px; text-align: center;
  border: 1.5px solid currentColor; outline: 1px solid currentColor; outline-offset: 2px;
  border-radius: 6px; transform: rotate(-4deg); color: var(--live);
  font: 700 10.5px/1.2 var(--data); letter-spacing: .08em; text-transform: uppercase; }
.stamp.pending { color: var(--ink-2); border-style: dashed; outline: 0; transform: rotate(-2deg); }
.idle { color: var(--ink-3); }
button { font: 600 13px/1 var(--sans); padding: 8px 14px; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--rule-strong); background: var(--paper-raised); color: var(--ink); }
button.act { background: var(--act); border-color: var(--act); color: #fff; }
button.act:hover { background: var(--act-hover); }
button:disabled { opacity: .45; cursor: default; }
a { color: var(--act); font-weight: 600; }
</style></head>
<body>
<header><h1 id="title"></h1><p id="who"></p></header>
<main><div id="note" class="note" role="status"></div><div id="list"></div></main>
<script>
const T = __TEXT__;
const lang = document.documentElement.lang;
const hash = new URLSearchParams(location.hash.slice(1)).get("t");
if (hash) { sessionStorage.setItem("t", hash); history.replaceState(null, "", "/"); }
const token = sessionStorage.getItem("t") || "";
const $ = (id) => document.getElementById(id);
const el = (tag, attrs, ...kids) => { const n = document.createElement(tag);
  Object.assign(n, attrs || {}); n.append(...kids.filter((k) => k != null)); return n; };
$("title").textContent = T.lp_title;
async function call(method, path, body) {
  const r = await fetch(path, { method, headers: { "X-Panel-Token": token, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.status);
  return data;
}
const STATE = { live: ["stamp", "lp_live"], opening: ["stamp pending", "lp_opening"],
  syncing: ["stamp pending", "lp_syncing"], pending: ["stamp pending", "lp_pending"] };
function draw(s) {
  $("who").textContent = s.signed_in ? `${s.user} · ${s.hub}` : T.lp_signed_out;
  const note = $("note");
  note.className = "note" + (s.online ? "" : " warn");
  note.replaceChildren(s.online ? T.lp_online + " " : T.lp_offline,
    s.online ? el("a", { href: s.hub, textContent: T.lp_open_hub, target: "_blank", rel: "noopener" }) : null);
  if (!s.profiles.length) return $("list").replaceChildren(el("p", { className: "idle", textContent: T.lp_none }));
  const rows = s.profiles.map((p) => {
    const [cls, key] = STATE[p.state] || [null, "lp_idle"];
    const action = p.state === "live"
      ? el("button", { textContent: T.lp_stop, onclick: () => act("stop", p.name) })
      : el("button", { className: "act", textContent: T.lp_open, disabled: s.online || !p.copy || p.state !== "idle",
          onclick: () => act("open", p.name) });
    return el("tr", {},
      el("td", {}, el("b", { textContent: p.name }), el("span", { className: "minor", textContent: [p.engine, p.os].filter(Boolean).join(" · ") })),
      el("td", {}, el("span", { className: cls || "idle", textContent: T[key] })),
      el("td", { className: "minor", textContent: p.copy ? new Date(p.copy * 1000).toLocaleString(lang) : T.lp_no_copy }),
      el("td", {}, action));
  });
  $("list").replaceChildren(el("table", {}, el("thead", {}, el("tr", {},
    ...[T.lp_profile, T.lp_state, T.lp_copy, ""].map((h) => el("th", { textContent: h })))), el("tbody", {}, ...rows)));
}
async function act(what, name) {
  try { await call("POST", "/api/" + what, { name }); } catch (e) { alert(e.message); }
  load();
}
async function load() {
  try { draw(await call("GET", "/api/state")); }
  catch (e) { const n = $("note"); n.className = "note err"; n.textContent = e.message; }
}
load();
setInterval(load, 3000);
</script></body></html>
"""
