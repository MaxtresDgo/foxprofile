"""Session verifier: open a profile, check it is still signed in to chatgpt.com,
close it, wait for the sync, open it again, N times. Reports each round's start
page session check (cookies saved / restored / in the browser, exit IP) and
whether ChatGPT still knows the user, so a lost session shows which step lost it.

    FOXPROFILE_URL=https://hub.example FOXPROFILE_API_TOKEN=fpk_... \\
        python tests/e2e/session_e2e.py <profile> [rounds=3]

Sign in to chatgpt.com in the profile once before. Works on a hub (through the
agent) and on a local FoxProfile. Prints only yes/no for the session, never a token.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.getenv("FOXPROFILE_URL", "http://127.0.0.1:8000").rstrip("/") + "/api/v1"
TOKEN = os.getenv("FOXPROFILE_API_TOKEN", "")
# E2E_SITE= (empty) skips the ChatGPT step: only the start page's session check.
SITE = os.getenv("E2E_SITE", "https://chatgpt.com/")
SYNC_WAIT_S = 600

# On the start page (first tab): the session check label.py renders.
START_PAGE = """(() => {
  const v = document.querySelector(".verdict");
  const c = document.getElementById("ipcheck");
  return { verdict: v ? v.textContent : null, bad: !!(v && v.classList.contains("bad")),
           ip: (document.getElementById("ip") || {}).textContent || "",
           ipcheck: c ? c.textContent : "" };
})()"""
# On chatgpt.com: its own session endpoint, reduced to signed in or not.
CHATGPT = """fetch("/api/auth/session", { credentials: "include" })
  .then(async (r) => ({ status: r.status, logged: !!((await r.json().catch(() => ({}))).user) }))
  .catch((e) => ({ status: 0, logged: false, error: String(e) }))"""


def call(method, path, body=None, timeout=300):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "foxprofile-session-e2e",
            **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(text)
        except ValueError:
            return e.code, {"detail": text[:300]}


def page(name, method, action, body=None):
    status, data = call(method, f"/browser/{name}/page/{action}", body)
    if status != 200:
        raise RuntimeError(f"{action}: HTTP {status} {data.get('detail', data)}")
    return data


def running(name):
    return call("GET", f"/browser/{name}/status")[1].get("is_running")


def wait_stopped(name):
    """On a hub the profile counts as running until its agent has uploaded it."""
    t = time.time()
    while running(name):
        if time.time() - t > SYNC_WAIT_S:
            raise RuntimeError("still running/syncing after 10 min")
        time.sleep(3)
    return round(time.time() - t, 1)


def open_profile(name):
    t = time.time()
    for _ in range(20):  # 409 while the last upload is still finishing
        status, data = call("POST", f"/browser/{name}/launch")
        if status in (200, 202):
            break
        if status != 409:
            raise RuntimeError(f"launch: HTTP {status} {data}")
        time.sleep(5)
    while not page_ready(name):
        if time.time() - t > 300:
            raise RuntimeError("browser did not come up in 5 min")
        time.sleep(2)
    return round(time.time() - t, 1)


def page_ready(name):
    return call("GET", f"/browser/{name}/page/tabs", timeout=30)[0] == 200


def round_trip(name):
    out = {"open_s": open_profile(name)}
    time.sleep(4)  # the start page looks up the exit IP
    page(name, "POST", "tabs/0/select")
    out["start"] = page(name, "POST", "evaluate", {"script": START_PAGE})["result"]
    out["chatgpt"] = {"logged": True, "status": "skipped"}
    if SITE:
        tabs = page(name, "POST", "tabs", {"url": SITE})["tabs"]
        mine = next(t["index"] for t in tabs if t["active"])
        time.sleep(8)  # Cloudflare's check and the app's first requests
        out["chatgpt"] = page(name, "POST", "evaluate", {"script": CHATGPT})["result"]
        page(name, "DELETE", f"tabs/{mine}")  # not left for tabs.json to reopen
    call("POST", f"/browser/{name}/stop")
    out["sync_s"] = wait_stopped(name)
    return out


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    name = urllib.parse.quote(sys.argv[1], safe="")
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    if running(name):
        print("Profile is open: closing it first so the test starts from a sync.")
        call("POST", f"/browser/{name}/stop")
        wait_stopped(name)
    lost = False
    for i in range(1, rounds + 1):
        r = round_trip(name)
        start, gpt = r["start"], r["chatgpt"]
        signed = "yes" if gpt.get("logged") else "NO"
        print(
            f"round {i}: open {r['open_s']}s, sync {r['sync_s']}s | ip {start['ip']} "
            f"{start['ipcheck']} | chatgpt HTTP {gpt.get('status')} signed in: {signed}"
        )
        print("   start page:", (start["verdict"] or "(no session check)").replace("\n", " / "))
        if i == 1 and not gpt.get("logged"):
            print(
                "Not signed in to chatgpt.com on the first round: sign in there once, then rerun."
            )
            return 2
        if not gpt.get("logged") or start["bad"]:
            lost = True
    print("FAIL: the session or its cookies were lost" if lost else "PASS: session kept")
    return 1 if lost else 0


if __name__ == "__main__":
    sys.exit(main())
