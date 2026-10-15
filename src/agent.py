"""FoxProfile agent: runs the hub's profiles on this PC.

    python -m src.agent [--hub http://192.0.2.10:8000] [--user ana]

Reads FOXPROFILE_HUB_URL, FOXPROFILE_AGENT_USER and FOXPROFILE_AGENT_PASSWORD
(asked for when unset) from the environment or .env. The agent connects out to
the hub, so the PC needs no open ports. A profile's data is downloaded when it
launches and uploaded back, then deleted here, when its browser closes.
"""

import argparse
import getpass
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.schemas.proxy import GeoCheckResponse
from src.core.config import AGENT_USER, DATA_DIR, HUB_URL, LAUNCH_WAIT_TIMEOUT
from src.core.container import Container
from src.core.logging import get_logger
from src.models.profile import Profile
from src.services.browser import cookies
from src.services.browser.launcher import BrowserControlError
from src.services.profile.transfer import export_to_zip, import_from_zip
from src.services.proxy.geo_check import check_geo

logger = get_logger("agent")
RETRY_SECONDS = 10


def say(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class HubClient:
    def __init__(self, url: str, user: str, password: str) -> None:
        self.url, self.user, self.password = url, user, password
        self.token = ""

    def login(self) -> None:
        body = json.dumps({"username": self.user, "password": self.password}).encode()
        self.token = self._send("POST", "/hub/login", body, auth=False)["token"]

    def call(self, method: str, path: str, data=None, timeout: float = 60, raw=False):
        try:
            return self._send(method, path, data, timeout=timeout, raw=raw)
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            self.login()  # session expired (12 h) or the hub restarted with a new secret
            if hasattr(data, "seek"):
                data.seek(0)
            return self._send(method, path, data, timeout=timeout, raw=raw)

    def _send(self, method, path, data=None, timeout: float = 60, auth=True, raw=False):
        # Cloudflare's Browser Integrity Check answers 403 (error 1010) to Python-urllib.
        headers = {"User-Agent": "FoxProfile-Agent"}
        if isinstance(data, bytes):
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        if hasattr(data, "seek"):
            headers["Content-Type"] = "application/zip"
            headers["Content-Length"] = str(os.fstat(data.fileno()).st_size)
        req = urllib.request.Request(
            self.url + "/api/v1" + path, data=data, headers=headers, method=method
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        if raw:
            return resp
        with resp:
            body = resp.read()
        return json.loads(body) if body else None


class Agent:
    def __init__(self, hub: HubClient) -> None:
        self.hub = hub
        container = Container(hub=False)
        self.bl = container.browser_launcher
        self.proxies = container.proxy_service
        self.data_root = pathlib.Path(DATA_DIR)
        self.data_root.mkdir(parents=True, exist_ok=True)

    def q(self, name: str) -> str:
        return urllib.parse.quote(name, safe="")

    # --- commands --------------------------------------------------------------

    def launch(self, profile_dict: dict) -> None:
        name = profile_dict["name"]
        try:
            profile = self._download(name)
        except Exception as e:
            say(f"[{name}] download failed: {e}")
            self._status(name, False, f"Download failed on {self.hub.user}'s PC: {e}")
            return
        say(f"[{name}] launching")
        try:
            started = self.bl.start_thread(
                profile, lambda msg: logger.info(msg), on_stop=lambda: self._closed(name)
            )
        except Exception as e:
            self._status(name, False, str(e))
            return
        if not started:
            self._status(name, True, None)  # already running here
            return
        ok, error = self.bl.wait_for_launch(name, LAUNCH_WAIT_TIMEOUT)
        if error != "timeout":
            self._status(name, ok, error)
        say(f"[{name}] " + ("running" if ok else f"failed: {error}"))

    def stop(self, name: str) -> None:
        # stop_profile does not fire on_stop, so sync here.
        if self.bl.stop_profile(name):
            say(f"[{name}] stopped from the panel")
            self._sync(name)
        else:
            # Not running here (lost launch, or already closed): just free it.
            self._call_quietly("POST", f"/hub/release/{self.q(name)}")

    def control(self, cmd: dict) -> None:
        """A page action from the hub (remote viewer, MCP) for a browser running here."""
        try:
            result = self.bl.control(cmd["name"], cmd["action"], cmd["params"])
        except BrowserControlError as e:
            self._answer(cmd["id"], ok=False, error=str(e), status=e.status)
            return
        self._answer(cmd["id"], ok=True, result=result)

    def cookie_job(self, cmd: dict) -> None:
        """Export or import a stopped profile's cookies; the hub holds its lease meanwhile."""
        name, params = cmd["profile"]["name"], cmd["params"]
        local = self.data_root / name
        try:
            profile = self._download(name)
            if cmd["action"] == "export":
                text, count = cookies.export_cookies(
                    name, profile.os_type, params["format"], profile.engine
                )
                result = {"text": text, "count": count}
            else:
                count = cookies.import_cookies(
                    name, profile.os_type, params["content"], profile.engine
                )
                self._upload(name, local)
                result = {"imported": count}
            say(f"[{name}] cookies {cmd['action']}: {count}")
            self._answer(cmd["id"], ok=True, result=result)
        except cookies.CookieError as e:
            self._answer(cmd["id"], ok=False, error=str(e), status=400)
        except Exception as e:
            say(f"[{name}] cookies {cmd['action']} failed: {e}")
            self._answer(cmd["id"], ok=False, error=f"On {self.hub.user}'s PC: {e}", status=502)
        finally:
            shutil.rmtree(local, ignore_errors=True)

    def check(self, cmd: dict) -> None:
        """Check IP / Check proxy from the panel, run here: the browsers use the
        proxy from this PC, which may reach it when the hub cannot (Tailscale)."""
        params = cmd["params"]
        try:
            if cmd["op"] == "proxy_check":
                ok, message = self.proxies.check_proxy_sync(params["proxy"], params["timeout"])
                result = {"ok": ok, "message": message}
            else:
                geo = check_geo(params["proxy"], params["timezone"], params["locale"])
                result = GeoCheckResponse.from_result(geo).model_dump()
        except Exception as e:
            self._answer(cmd["id"], ok=False, error=f"On {self.hub.user}'s PC: {e}", status=502)
            return
        self._answer(cmd["id"], ok=True, result=result)

    def _answer(self, request_id: str, **answer) -> None:
        self._call_quietly("POST", f"/hub/result/{request_id}", json.dumps(answer).encode())

    def _closed(self, name: str) -> None:
        say(f"[{name}] browser closed")
        threading.Thread(target=self._sync, args=(name,), daemon=True).start()

    # --- data transfer ---------------------------------------------------------

    def _download(self, name: str) -> Profile:
        local = self.data_root / name
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "bundle.zip")
            with (
                self.hub.call("GET", f"/hub/bundle/{self.q(name)}", raw=True, timeout=600) as r,
                open(zip_path, "wb") as f,
            ):
                shutil.copyfileobj(r, f)
            shutil.rmtree(local, ignore_errors=True)
            ok, result = import_from_zip(zip_path, str(self.data_root))
        if not ok:
            raise RuntimeError(result)
        return result

    def _sync(self, name: str) -> None:
        """Upload the profile's data and end the lease, retrying until the hub takes it."""
        local = self.data_root / name
        while True:
            try:
                self._upload(name, local)
                shutil.rmtree(local, ignore_errors=True)
                say(f"[{name}] synced to the hub")
                return
            except urllib.error.HTTPError as e:
                if e.code in (400, 404, 409):
                    # Deleted on the hub, or someone else holds it now: keep the
                    # local copy for an admin to recover by hand.
                    say(f"[{name}] hub refused the upload ({e.code}); data kept in {local}")
                    return
                say(f"[{name}] upload failed ({e.code}), retrying")
            except (OSError, urllib.error.URLError) as e:
                say(f"[{name}] upload failed ({e}), retrying")
            time.sleep(RETRY_SECONDS)

    def _upload(self, name: str, local: pathlib.Path) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ok, zip_path = export_to_zip(Profile(name=name), str(local), tmp, include_data=True)
            if not ok:
                raise OSError(zip_path)
            with open(zip_path, "rb") as f:
                self.hub.call("PUT", f"/hub/bundle/{self.q(name)}?release=1", f, timeout=600)

    def _status(self, name: str, ok: bool, error: str | None) -> None:
        body = json.dumps({"ok": ok, "error": error}).encode()
        self._call_quietly("POST", f"/hub/status/{self.q(name)}", body)

    def _call_quietly(self, method: str, path: str, data=None) -> None:
        try:
            self.hub.call(method, path, data)
        except (OSError, urllib.error.URLError) as e:
            say(f"hub call {path} failed: {e}")

    # --- main loop -------------------------------------------------------------

    def recover(self) -> None:
        """Upload what a previous run left behind (crash, Ctrl+C, power loss):
        any profile dir still here was never synced."""
        for local in sorted(self.data_root.iterdir()):
            if local.is_dir():
                say(f"[{local.name}] uploading data from the last session")
                self._sync(local.name)

    def run(self) -> None:
        say(f"Connected to {self.hub.url} as {self.hub.user}. Leave this window open.")
        self.recover()
        while True:
            running = ",".join(sorted(self.bl.running_profile_names()))
            try:
                resp = self.hub.call("GET", "/hub/poll?running=" + self.q(running), timeout=60)
            except (OSError, urllib.error.URLError) as e:
                say(f"hub unreachable ({e}), retrying")
                time.sleep(RETRY_SECONDS)
                continue
            if not resp:
                continue
            if resp["op"] == "launch":
                threading.Thread(target=self.launch, args=(resp["profile"],), daemon=True).start()
            elif resp["op"] == "stop":
                threading.Thread(target=self.stop, args=(resp["name"],), daemon=True).start()
            elif resp["op"] == "control":
                threading.Thread(target=self.control, args=(resp,), daemon=True).start()
            elif resp["op"] == "cookies":
                threading.Thread(target=self.cookie_job, args=(resp,), daemon=True).start()
            elif resp["op"] in ("proxy_check", "geo_check"):
                threading.Thread(target=self.check, args=(resp,), daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="FoxProfile agent (runs hub profiles here)")
    parser.add_argument("--hub", default=HUB_URL, help="Hub URL (FOXPROFILE_HUB_URL)")
    parser.add_argument("--user", default=AGENT_USER, help="Operator (FOXPROFILE_AGENT_USER)")
    args = parser.parse_args()
    hub_url = (args.hub or input("Hub URL (http://...:8000): ")).strip().rstrip("/")
    user = (args.user or input("User: ")).strip()
    password = os.getenv("FOXPROFILE_AGENT_PASSWORD") or getpass.getpass(f"Password for {user}: ")
    hub = HubClient(hub_url, user, password)
    try:
        hub.login()
    except urllib.error.HTTPError as e:
        sys.exit(f"Login refused: {e.code} {e.read().decode(errors='replace')}")
    except (OSError, urllib.error.URLError) as e:
        sys.exit(f"Cannot reach the hub at {hub_url}: {e}")
    if not (args.hub and args.user):
        # Remember what was typed (never the password) for the next start.
        with open(".env", "a", encoding="utf-8") as f:
            f.write(f"\nFOXPROFILE_HUB_URL={hub_url}\nFOXPROFILE_AGENT_USER={user}\n")
    agent = Agent(hub)
    try:
        agent.run()
    except urllib.error.HTTPError as e:
        sys.exit(f"Hub refused: {e.code} {e.read().decode(errors='replace')}")
    except KeyboardInterrupt:
        say("Stopping; open browsers close and sync on the next start.")
    finally:
        agent.bl.shutdown_all()


if __name__ == "__main__":
    main()
