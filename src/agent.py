"""FoxProfile agent: runs the hub's profiles on this PC.

    python -m src.agent [--hub http://192.0.2.10:8000] [--home] [--console]

Signs in through the browser: the hub's panel asks the operator to authorize this
agent and hands it a key, kept in agent.json next to .env. The agent connects out
to the hub, so the PC needs no open ports. A profile's data is downloaded when it
launches and uploaded back, then deleted here, when its browser closes.

On Windows and macOS it lives in the tray (menu bar), with no console window; its
menu opens the panel, switches account or shows the log in a window. --console
(or a system without a tray) keeps it in the terminal instead. --home runs it from
the per-user data folder (%LOCALAPPDATA%\\FoxProfile, ~/Library/Application
Support/FoxProfile), as the Windows and macOS installers do.

Installed agents follow the hub's version: when the hub runs other code, the
agent downloads it once no browser is open and restarts (src/hub/agent_code.py).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "--home" in sys.argv:
    # Before src.core.config: it reads .env and resolves data paths from the cwd.
    _base = (
        os.path.expanduser("~/Library/Application Support")
        if sys.platform == "darwin"
        else os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
    )
    _home = os.path.join(_base, "FoxProfile")
    os.makedirs(_home, exist_ok=True)
    os.chdir(_home)

import argparse  # noqa: E402
import contextlib  # noqa: E402
import hashlib  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import pathlib  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402
import webbrowser  # noqa: E402
import zipfile  # noqa: E402

from src.api.schemas.proxy import GeoCheckResponse  # noqa: E402
from src.core.config import (  # noqa: E402
    DATA_DIR,
    EXTENSIONS_DIR,
    HUB_URL,
    LAUNCH_WAIT_TIMEOUT,
    LOG_DIR,
)
from src.core.container import Container  # noqa: E402
from src.core.logging import get_logger, setup_logging  # noqa: E402
from src.core.strings import current_language, get_string, set_language  # noqa: E402
from src.hub import agent_code  # noqa: E402
from src.hub.agent_auth import SignIn, pc_name  # noqa: E402
from src.models.profile import Profile  # noqa: E402
from src.services.browser import cookies  # noqa: E402
from src.services.browser import extensions as ext_files  # noqa: E402
from src.services.browser.launcher import BrowserControlError  # noqa: E402
from src.services.profile import adspower  # noqa: E402
from src.services.profile.transfer import (  # noqa: E402
    COOKIES_GZ,
    COOKIES_SHA,
    IntegrityError,
    export_to_zip,
    file_sha256,
    import_from_zip,
    pack_cookies,
    read_cookies,
)
from src.services.proxy.geo_check import check_geo  # noqa: E402

logger = get_logger("agent")
RETRY_SECONDS = 10
# Profiles go up in pieces: Cloudflare caps a request at 100 MB and 100 s.
PART_BYTES = 8 * 1024 * 1024
# What a hub without a route answers: older than the pieces above.
OLD_HUB = (404, 405)
CREDENTIALS = pathlib.Path("agent.json")
ICON = pathlib.Path(__file__).parent / "assets" / "icon.png"


def say(msg: str) -> None:
    logger.info(msg)
    if sys.stdout:  # None under pythonw (no console)
        print(time.strftime("%H:%M:%S"), msg, flush=True)


def _request(url: str, key: str = "", timeout: float = 10):
    # Cloudflare's Browser Integrity Check answers 403 (error 1010) to Python-urllib.
    headers = {"User-Agent": "FoxProfile-Agent"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return json.loads(r.read())


def check_hub(url: str) -> str | None:
    """None when `url` is a FoxProfile hub, otherwise why not."""
    try:
        info = _request(url + "/api/v1/info")
    except (OSError, ValueError) as e:
        return get_string("agent_cannot_reach", url=url, error=e)
    if info.get("mode") != "hub":
        return get_string("agent_not_hub", url=url)
    return None


def who_is(hub: str, key: str) -> dict:
    return _request(hub + "/api/v1/hub/me", key)


class HubClient:
    def __init__(self, url: str, user: str, key: str, display: str = "") -> None:
        self.url, self.user, self.key, self.display = url, user, key, display
        # Called on a 401: the key was revoked (or replaced by another sign-in).
        self.on_unauthorized = lambda: None

    @property
    def label(self) -> str:
        """First name from the identity platform for the tray and console; else the account."""
        return next(iter((self.display or self.user).split()), "")

    def call(self, method: str, path: str, data=None, timeout: float = 60, raw=False, headers=None):
        try:
            return self._send(method, path, data, timeout=timeout, raw=raw, headers=headers)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self.on_unauthorized()
            raise

    def _send(self, method, path, data=None, timeout: float = 60, raw=False, headers=None):
        headers = {
            "User-Agent": "FoxProfile-Agent",
            "Authorization": f"Bearer {self.key}",
            **(headers or {}),
        }
        if isinstance(data, bytes):
            headers.setdefault("Content-Type", "application/json")
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
    def __init__(self, hub: HubClient, status=lambda key, **kw: None, notify=say) -> None:
        self.hub = hub
        self.status = status
        self.notify = notify
        # Reported on every poll, for the hub to decide on updates (hub.routes.poll).
        self.identity = {
            "code": agent_code.code_id(),
            "req": agent_code.installed_requirements() or "",
            "pc": pc_name(),
        }
        self.restart = False  # set when an update is unpacked: run() returns
        # Installed agents follow their hub's code; a dev hub deploys the dev branch.
        self.follow_updates = True
        # Only production agents run the hub's installer on their own: a dev agent
        # would turn into whatever channel the hub offers.
        self.auto_install = True
        self._skipped_update = ""
        # What the panel shows about this PC (hub POST /agent/status): the browser
        # download, an update, or the profiles being opened or saved right now.
        self.browser_download: int | None = None  # percent, -1 before it is known
        self.updating = False
        self._ops: dict[str, str] = {}  # profile -> "opening" | "syncing"
        # Profiles downloaded by this run: their os_type and engine open the
        # browser that packs the cookies when they are uploaded.
        self._profiles: dict[str, Profile] = {}
        # Unpacked extension folders of each launch (what the hub sent with it).
        self._ext_dirs: dict[str, list[str]] = {}
        self._sent: tuple = ()
        self._activity_lock = threading.Lock()
        threading.Thread(target=self._heartbeat, daemon=True).start()
        container = Container(hub=False)
        self.bl = container.browser_launcher
        self.bl.extensions = lambda profile: self._ext_dirs.get(profile.name, [])
        self.proxies = container.proxy_service
        self.data_root = pathlib.Path(DATA_DIR)
        self.data_root.mkdir(parents=True, exist_ok=True)

    def q(self, name: str) -> str:
        return urllib.parse.quote(name, safe="")

    # --- activity (the panel's "Your PC" line) ---------------------------------

    def activity(self) -> dict:
        if self.updating:
            return {"state": "updating"}
        if self.browser_download is not None:
            pct = self.browser_download if self.browser_download >= 0 else None
            return {"state": "downloading", "pct": pct}
        for name, state in list(self._ops.items()):
            return {"state": state, "profile": name}
        return {"state": "ready"}

    def report(self, force: bool = False) -> None:
        """Tell the hub what this PC is doing, when it changed (or `force`)."""
        if not self.hub.key:
            return
        now = self.activity()
        with self._activity_lock:
            if not force and tuple(now.items()) == self._sent:
                return
            self._sent = tuple(now.items())
        threading.Thread(target=self._send_activity, args=(now,), daemon=True).start()

    def _send_activity(self, activity: dict) -> None:
        with contextlib.suppress(OSError, urllib.error.URLError, ValueError):
            self.hub.call("POST", "/hub/agent/status", json.dumps(activity).encode(), timeout=10)

    def _heartbeat(self) -> None:
        # The hub forgets a busy state it has not heard about for a while (a long
        # upload has no percentage to report): repeat it while busy.
        while True:
            time.sleep(10)
            if self.activity()["state"] != "ready":
                self.report(force=True)

    @contextlib.contextmanager
    def busy(self, state: str, name: str):
        self._ops[name] = state
        self.report()
        try:
            yield
        finally:
            self._ops.pop(name, None)
            self.report()

    # --- commands --------------------------------------------------------------

    def launch(self, profile_dict: dict, extensions: list[dict] = ()) -> None:
        name = profile_dict["name"]
        try:
            with self.busy("opening", name):
                profile = self._download(name)
                self._ext_dirs[name] = self._extensions(extensions)
        except Exception as e:
            say(get_string("agent_download_failed", name=name, error=e))
            self._status(name, False, f"Download failed on {self.hub.user}'s PC: {e}")
            return
        say(get_string("agent_launching", name=name))
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
        say(
            get_string("agent_running", name=name)
            if ok
            else get_string("agent_failed", name=name, error=error)
        )

    def stop(self, name: str) -> None:
        # stop_profile does not fire on_stop, so sync here.
        # Time for the browser to close and write its cookies before the upload.
        if self.bl.stop_profile(name, timeout=15):
            say(get_string("agent_stopped_panel", name=name))
            self._sync(name)
        else:
            # Not running here (lost launch, or already closed): just free it.
            self._call_quietly("POST", f"/hub/release/{self.q(name)}")

    def stop_all(self) -> None:
        """Close every browser and upload its data, e.g. before switching account."""
        for name in self.bl.running_profile_names():
            self.stop(name)

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
                self._pack_cookies(name, local)
                self._upload(name, local)
                result = {"imported": count}
            say(get_string("agent_cookies", name=name, action=cmd["action"], count=count))
            self._answer(cmd["id"], ok=True, result=result)
        except cookies.CookieError as e:
            self._answer(cmd["id"], ok=False, error=str(e), status=400)
        except Exception as e:
            say(get_string("agent_cookies_failed", name=name, action=cmd["action"], error=e))
            self._answer(cmd["id"], ok=False, error=f"On {self.hub.user}'s PC: {e}", status=502)
        finally:
            shutil.rmtree(local, ignore_errors=True)

    def check(self, cmd: dict) -> None:
        """Check IP / Check proxy from the panel, run here: the browsers use the
        proxy from this PC, which may reach it when the hub cannot (Tailscale)."""
        params = cmd["params"]
        try:
            if cmd["op"].startswith("adspower_"):
                result = self._adspower(cmd["op"], params)
            elif cmd["op"] == "proxy_check":
                ok, message = self.proxies.check_proxy_sync(params["proxy"], params["timeout"])
                result = {"ok": ok, "message": message}
            else:
                geo = check_geo(params["proxy"], params["timezone"], params["locale"])
                result = GeoCheckResponse.from_result(geo).model_dump()
        except adspower.AdsPowerError as e:
            self._answer(cmd["id"], ok=False, error=str(e), status=400)
            return
        except Exception as e:
            self._answer(cmd["id"], ok=False, error=f"On {self.hub.user}'s PC: {e}", status=502)
            return
        self._answer(cmd["id"], ok=True, result=result)

    def _adspower(self, op: str, params: dict):
        """Read AdsPower on this PC for the panel's "Import from AdsPower"."""
        client = adspower.AdsPower(int(params["port"]), params.get("api_key") or "")
        if op == "adspower_list":
            return client.profiles()
        data = client.fetch(params["profile_id"])
        say(get_string("agent_adspower", name=data["name"]))
        return data

    def _answer(self, request_id: str, **answer) -> None:
        self._call_quietly("POST", f"/hub/result/{request_id}", json.dumps(answer).encode())

    def _closed(self, name: str) -> None:
        say(get_string("agent_closed", name=name))
        threading.Thread(target=self._sync, args=(name,), daemon=True).start()

    # --- data transfer ---------------------------------------------------------

    def _download(self, name: str) -> Profile:
        local = self.data_root / name
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "bundle.zip")
            self._fetch_bundle(name, zip_path)
            shutil.rmtree(local, ignore_errors=True)
            ok, result = import_from_zip(zip_path, str(self.data_root))
        if not ok:
            raise RuntimeError(result)
        self._profiles[name] = result
        try:
            self._restore_cookies(result, local)
        except IntegrityError:
            # Not left for recover() to upload: the hub keeps its own copy.
            shutil.rmtree(local, ignore_errors=True)
            raise
        return result

    def _extensions(self, wanted: list[dict]) -> list[str]:
        """The launch's extensions unpacked here, downloaded from the hub once per
        version and checked against the SHA-256 the hub sent."""
        cache = pathlib.Path(EXTENSIONS_DIR) / "unpacked"
        dirs = []
        for ext in wanted:
            target = cache / f"{ext['id']}-{ext['sha256'][:12]}"
            if (target / "manifest.json").exists():
                dirs.append(str(target.resolve()))
                continue
            with tempfile.TemporaryDirectory() as tmp:
                archive = os.path.join(tmp, "extension.zip")
                path = f"/extensions/{ext['id']}/file"
                with (
                    self.hub.call("GET", path, raw=True, timeout=300) as r,
                    open(archive, "wb") as f,
                ):
                    shutil.copyfileobj(r, f)
                if ext_files.sha256(archive) != ext["sha256"]:
                    raise IntegrityError(get_string("err_bundle_corrupt"))
                dirs.append(ext_files.unpack(archive, ext["id"], ext["sha256"], cache))
            say(get_string("agent_extension_ready", name=ext["name"]))
        return dirs

    def _fetch_bundle(self, name: str, zip_path: str) -> None:
        """The profile's ZIP in pieces, checked against its SHA-256: the hub builds
        it while this polls, so no request comes near Cloudflare's 100 MB / 100 s."""
        base = f"/hub/bundle/{self.q(name)}/download"
        try:
            job = self.hub.call("POST", base, b"{}")["id"]
        except urllib.error.HTTPError as e:
            if e.code not in OLD_HUB:
                raise
            self._fetch_whole(name, zip_path)
            return
        try:
            while not (info := self.hub.call("GET", f"{base}/{job}"))["ready"]:
                time.sleep(1)
            digest = hashlib.sha256()
            with open(zip_path, "wb") as f:
                while (offset := f.tell()) < info["size"]:
                    path = f"{base}/{job}/part?offset={offset}"
                    with self.hub.call("GET", path, raw=True, timeout=120) as r:
                        part = r.read()
                    if not part:
                        raise IntegrityError(get_string("err_bundle_corrupt"))
                    f.write(part)
                    digest.update(part)
            if digest.hexdigest() != info["sha256"]:
                raise IntegrityError(get_string("err_bundle_corrupt"))
        finally:
            self._call_quietly("DELETE", f"{base}/{job}")

    def _fetch_whole(self, name: str, zip_path: str) -> None:
        """One request, for a hub older than downloads in pieces (a dev agent can
        visit any hub). Its SHA-256 header is checked when the hub sends one."""
        digest = hashlib.sha256()
        with (
            self.hub.call("GET", f"/hub/bundle/{self.q(name)}", raw=True, timeout=600) as r,
            open(zip_path, "wb") as f,
        ):
            while chunk := r.read(1 << 20):
                f.write(chunk)
                digest.update(chunk)
            expected = r.headers.get("X-Content-SHA256")
        if expected and expected.lower() != digest.hexdigest():
            raise IntegrityError(get_string("err_bundle_corrupt"))

    def _restore_cookies(self, profile: Profile, local: pathlib.Path) -> None:
        """Load the cookies packed on the last close into this PC's browser store."""
        text = read_cookies(str(local))  # IntegrityError: the launch is refused
        # Imported now, so a stale copy is never uploaded back over newer cookies.
        for packed in (COOKIES_GZ, COOKIES_SHA):
            (local / packed).unlink(missing_ok=True)
        if not text or not json.loads(text):
            return
        try:
            count = cookies.import_cookies(profile.name, profile.os_type, text, profile.engine)
            say(get_string("agent_cookies_restored", name=profile.name, count=count))
        except cookies.CookieError as e:
            # The browser's own store still has the cookies this PC can read.
            say(get_string("agent_cookies_pack_failed", name=profile.name, error=e))

    def _pack_cookies(self, name: str, local: pathlib.Path) -> None:
        """Export the stopped profile's cookies as gzipped JSON plus SHA-256."""
        profile = self._profiles.get(name)
        if profile is None:
            # ponytail: a profile left by a crashed run has no os_type/engine here;
            # it uploads its browser store alone. Ask the hub for them if that bites.
            return
        try:
            text, count = cookies.export_cookies(name, profile.os_type, "json", profile.engine)
            pack_cookies(str(local), text)
            say(get_string("agent_cookies_packed", name=name, count=count))
        except (cookies.CookieError, OSError) as e:
            say(get_string("agent_cookies_pack_failed", name=name, error=e))

    def _sync(self, name: str) -> None:
        """Upload the profile's data and end the lease, retrying until the hub takes it."""
        with self.busy("syncing", name):
            self._sync_now(name)

    def _sync_now(self, name: str) -> None:
        local = self.data_root / name
        self._pack_cookies(name, local)
        while True:
            try:
                self._upload(name, local)
                shutil.rmtree(local, ignore_errors=True)
                self._profiles.pop(name, None)
                say(get_string("agent_synced", name=name))
                return
            except urllib.error.HTTPError as e:
                if e.code in (400, 404, 409):
                    # Deleted on the hub, or someone else holds it now: keep the
                    # local copy for an admin to recover by hand.
                    say(get_string("agent_upload_refused", name=name, code=e.code, path=local))
                    return
                # 401 included: retried with the new key once the operator signs in again.
                say(get_string("agent_upload_retry", name=name, error=e.code))
            except (OSError, urllib.error.URLError) as e:
                say(get_string("agent_upload_retry", name=name, error=e))
            time.sleep(RETRY_SECONDS)

    def _upload(self, name: str, local: pathlib.Path) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ok, zip_path = export_to_zip(Profile(name=name), str(local), tmp, include_data=True)
            if not ok:
                raise OSError(zip_path)
            bundle = f"/hub/bundle/{self.q(name)}"
            raw = {"Content-Type": "application/octet-stream"}
            sha = {"X-Content-SHA256": file_sha256(zip_path)}
            with open(zip_path, "rb") as f:
                offset = 0
                while part := f.read(PART_BYTES):
                    path = f"{bundle}/part?offset={offset}"
                    try:
                        self.hub.call("PUT", path, part, timeout=120, headers=raw)
                    except urllib.error.HTTPError as e:
                        if offset or e.code not in OLD_HUB:
                            raise
                        # A hub older than uploads in pieces: the whole ZIP at once.
                        # A profile deleted on the hub answers 404 here too.
                        f.seek(0)
                        self.hub.call("PUT", f"{bundle}?release=1", f, timeout=600, headers=sha)
                        return
                    offset += len(part)
            self.hub.call("PUT", f"{bundle}?release=1&chunked=1", b"", timeout=600, headers=sha)

    def _status(self, name: str, ok: bool, error: str | None) -> None:
        body = json.dumps({"ok": ok, "error": error}).encode()
        self._call_quietly("POST", f"/hub/status/{self.q(name)}", body)

    def _call_quietly(self, method: str, path: str, data=None) -> None:
        try:
            self.hub.call(method, path, data)
        except (OSError, urllib.error.URLError) as e:
            say(get_string("agent_call_failed", path=path, error=e))

    # --- main loop -------------------------------------------------------------

    def recover(self) -> None:
        """Upload what a previous run left behind (crash, quit, power loss):
        any profile dir still here was never synced."""
        for local in sorted(self.data_root.iterdir()):
            if local.is_dir():
                say(get_string("agent_pending_upload", name=local.name))
                self._sync(local.name)

    def run(self) -> None:
        self.report(force=True)  # clears what a previous run left on the hub
        self.recover()
        while not self.restart:
            running = ",".join(sorted(self.bl.running_profile_names()))
            query = urllib.parse.urlencode({"running": running, **self.identity})
            try:
                resp = self.hub.call("GET", "/hub/poll?" + query, timeout=60)
            except (OSError, urllib.error.URLError) as e:
                if not (isinstance(e, urllib.error.HTTPError) and e.code == 401):
                    say(get_string("agent_unreachable", error=e))
                    self.status("agent_status_offline")
                time.sleep(RETRY_SECONDS)
                continue
            if self.hub.key:
                self.status("agent_status_connected", user=self.hub.label, host=_host(self.hub.url))
            if not resp:
                continue
            if resp["op"] == "launch":
                args = (resp["profile"], resp.get("extensions") or [])
                threading.Thread(target=self.launch, args=args, daemon=True).start()
            elif resp["op"] == "stop":
                threading.Thread(target=self.stop, args=(resp["name"],), daemon=True).start()
            elif resp["op"] == "control":
                threading.Thread(target=self.control, args=(resp,), daemon=True).start()
            elif resp["op"] == "cookies":
                threading.Thread(target=self.cookie_job, args=(resp,), daemon=True).start()
            elif resp["op"] in ("proxy_check", "geo_check", "adspower_list", "adspower_fetch"):
                threading.Thread(target=self.check, args=(resp,), daemon=True).start()
            elif resp["op"] == "update":
                if self.follow_updates:
                    self.update(resp["code"])
                elif self._skipped_update != resp["code"]:
                    self._skipped_update = resp["code"]
                    say(get_string("agent_update_skipped_dev", code=resp["code"]))
            elif resp["op"] == "installer_needed":
                if self.follow_updates and self.auto_install:
                    threading.Thread(target=self.install_update, daemon=True).start()
                else:
                    self.notify(get_string("agent_installer_needed"))

    def install_update(self) -> None:
        """New libraries need the new installer: download the hub's and run it. It
        stops this agent and starts the new one (Windows asks for admin rights; on
        macOS the operator clicks through Installer). An installer already run once
        is not run again: the hub may still offer an old one."""
        system = {"win32": "windows", "darwin": "macos"}.get(sys.platform)
        if system is None or not agent_code.installed_requirements():
            self.notify(get_string("agent_installer_needed"))
            return
        try:
            offered = {i["platform"]: i for i in self.hub.call("GET", "/hub/installers") or []}
        except (OSError, urllib.error.URLError, ValueError) as e:
            say(get_string("agent_installer_failed", error=e))
            return
        item = offered.get(system)
        stamp = f"{item['name']}:{item['size']}:{item['time']}" if item else ""
        tried = pathlib.Path("installer-tried.txt")
        if not item or (tried.exists() and tried.read_text(encoding="utf-8").strip() == stamp):
            self.notify(get_string("agent_installer_needed"))
            return
        say(get_string("agent_installer_downloading"))
        self.updating = True
        self.report()
        target = pathlib.Path("installer") / pathlib.PurePath(item["name"]).name
        try:
            target.parent.mkdir(exist_ok=True)
            # Public, and from a bucket a redirect to a presigned URL that must not
            # get this agent's key.
            url = f"{self.hub.url}/api/v1/hub/installers/{system}"
            request = urllib.request.Request(url, headers={"User-Agent": "FoxProfile-Agent"})
            with urllib.request.urlopen(request, timeout=600) as r, open(target, "wb") as f:
                shutil.copyfileobj(r, f)
            tried.write_text(stamp, encoding="utf-8")
            if system == "windows":
                flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                subprocess.Popen(
                    ["msiexec", "/i", str(target.resolve()), "/passive", "/norestart"],
                    creationflags=flags,
                    close_fds=True,
                )
            else:
                subprocess.Popen(["open", str(target.resolve())])
        except (OSError, urllib.error.URLError) as e:
            say(get_string("agent_installer_failed", error=e))
            return
        finally:
            self.updating = False
            self.report()
        self.notify(get_string("agent_installer_opened"))

    def update(self, code: str) -> None:
        """Unpack the hub's code for the installer's hook to load, then restart."""
        say(get_string("agent_updating"))
        self.status("agent_status_updating")
        self.updating = True
        self.report()
        try:
            with self.hub.call("GET", "/hub/agent/code", timeout=300, raw=True) as resp:
                data = resp.read()
            agent_code.unpack(data, code, pathlib.Path.cwd())
        except (OSError, ValueError, zipfile.BadZipFile) as e:
            say(get_string("agent_update_failed", error=e))
            self.updating = False
            self.report()
            return
        self.restart = True


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc


def _load_credentials() -> dict:
    try:
        return json.loads(CREDENTIALS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_credentials(creds: dict) -> None:
    # ponytail: plain file in the user's own folder, like ~/.config tokens of other
    # CLIs; the OS keychain if operators ever share a Windows account.
    CREDENTIALS.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    if os.name != "nt":
        CREDENTIALS.chmod(0o600)


def _single_instance():
    """A held lock file, or None when another agent already runs from this folder."""
    f = open("agent.lock", "a")  # noqa: SIM115 - held for the process lifetime
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def _log_file() -> str:
    for h in logging.getLogger("foxprofile").handlers:
        if isinstance(h, logging.FileHandler):
            return h.baseFilename
    return os.path.abspath(LOG_DIR)


def open_logs() -> None:
    """The log in a window of its own (src/agent_logs.py), following new lines."""
    path = _log_file()
    if importlib.util.find_spec("tkinter") is None:  # Python built without Tk
        if os.name == "nt":
            os.startfile(path)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
        return
    env = {**os.environ, "PYTHONPATH": str(pathlib.Path(__file__).resolve().parent.parent)}
    subprocess.Popen([sys.executable, "-m", "src.agent_logs", path, current_language()], env=env)


def camoufox_installed() -> bool:
    from camoufox.pkgman import installed_verstr

    try:
        installed_verstr()
        return True
    except Exception:
        return False


def ensure_browser(status) -> None:
    """Download Camoufox (~1.3 GB) on the first run: the installers ship the Chrome
    engine but not Camoufox, which lives in each user's cache. Progress goes to
    the tray as a percentage, the rest of fetch's output to the log."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**os.environ, "PYTHONPATH": root + os.pathsep + os.getenv("PYTHONPATH", "")}
    while not camoufox_installed():
        status("agent_status_downloading")
        say(get_string("agent_status_downloading"))
        shown = -1
        with open(_log_file(), "a", encoding="utf-8") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", "src.services.browser.camoufox_fetch"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                env=env,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                if not line.startswith("PROGRESS:"):
                    log.write(line)
                    continue
                done, total = (int(n) for n in line.split(":")[1:3])
                pct = done * 100 // total if total else 0
                if pct != shown:
                    shown = pct
                    status("agent_status_downloading_pct", pct=pct)
            code = proc.wait()
        if code == 0 and camoufox_installed():
            say(get_string("agent_browser_ready"))
            return
        say(get_string("agent_fetch_failed", code=code, seconds=RETRY_SECONDS * 6))
        time.sleep(RETRY_SECONDS * 6)


class App:
    """Signs the agent in, keeps it running and drives the tray icon."""

    def __init__(self, hub_url: str, creds: dict, dev: bool = False) -> None:
        self.dev = dev
        self.title = "FoxProfile Agent (dev)" if dev else "FoxProfile Agent"
        self.client = HubClient(
            hub_url or creds.get("hub", ""),
            creds.get("user", ""),
            creds.get("key", ""),
            creds.get("display", ""),
        )
        self.client.on_unauthorized = self._unauthorized
        self.agent = Agent(self.client, self.set_status, self.notify)
        # Run from source: no update hook would load the hub's code, so an
        # update would only restart into the same code, again and again.
        self.agent.follow_updates = (agent_code.INSTALLED / "BUILD").exists()
        self.agent.auto_install = not dev
        self.ready = threading.Event()
        if self.client.key:
            self.ready.set()
        self.signin: SignIn | None = None
        self.lock = threading.Lock()
        self.icon = None
        self.status_text = get_string("agent_status_sign_in")

    def set_status(self, key: str, **kwargs) -> None:
        text = get_string(key, **kwargs)
        if text == self.status_text:
            return
        self.status_text = text
        if self.icon is not None:
            self.icon.title = self.title + " · " + text
            self.icon.update_menu()

    # --- sign-in -------------------------------------------------------------

    def sign_in(self, reopen: bool = True) -> None:
        """Start a browser sign-in, or bring the pending one back to the browser."""
        with self.lock:
            if self.signin is None or self.signin.finished:
                self.signin = SignIn(self.client.url, check_hub, who_is)
                threading.Thread(target=self._finish, args=(self.signin,), daemon=True).start()
                reopen = True
            url = self.signin.url
        if reopen:
            say(get_string("agent_authorize", url=url))
            webbrowser.open(url)

    def change_hub(self) -> None:
        """Dev builds: sign in to another hub, starting on the hub page."""
        with self.lock:
            if self.signin is not None:
                self.signin.close()
            self.signin = SignIn(self.client.url, check_hub, who_is, ask_hub=True)
            threading.Thread(target=self._finish, args=(self.signin,), daemon=True).start()
            url = self.signin.url
        say(get_string("agent_authorize", url=url))
        webbrowser.open(url)

    def _finish(self, signin: SignIn) -> None:
        result = signin.wait()
        if result is None:
            return
        if self.client.key and (result["user"], result["hub"]) != (
            self.client.user,
            self.client.url,
        ):
            # The old account's browsers sync with the old key before switching.
            self.agent.stop_all()
        self.client.url, self.client.user, self.client.key = (
            result["hub"],
            result["user"],
            result["key"],
        )
        self.client.display = result.get("display", "")
        _save_credentials(result)
        self.agent.report(force=True)
        say(get_string("agent_connected", url=self.client.url, user=self.client.label))
        self.set_status(
            "agent_status_connected", user=self.client.label, host=_host(self.client.url)
        )
        self.ready.set()

    def _unauthorized(self) -> None:
        if self.ready.is_set():
            say(get_string("agent_key_revoked"))
        self.ready.clear()
        self.set_status("agent_status_sign_in")
        self.sign_in(reopen=False)

    # --- running ---------------------------------------------------------------

    def _download(self) -> None:
        first_run = not camoufox_installed()

        def status(key: str, **kwargs) -> None:
            self.set_status(key, **kwargs)
            self.agent.browser_download = kwargs.get("pct", -1)
            self.agent.report()

        ensure_browser(status)
        if self.agent.browser_download is not None:
            self.agent.browser_download = None
            self.agent.report()
        if first_run:
            if self.ready.is_set():
                self.set_status(
                    "agent_status_connected", user=self.client.label, host=_host(self.client.url)
                )
            self.notify(get_string("agent_browser_ready"))

    def notify(self, text: str) -> None:
        if self.icon is not None and getattr(self.icon, "HAS_NOTIFICATION", False):
            with contextlib.suppress(Exception):
                self.icon.notify(text, "FoxProfile Agent")

    def work(self) -> None:
        # Downloading and signing in at the same time: the operator authorizes the
        # agent in the browser while the ~1.3 GB browser is still coming.
        download = threading.Thread(target=self._download, daemon=True)
        download.start()
        if self.ready.is_set():
            say(get_string("agent_connected", url=self.client.url, user=self.client.label))
        else:
            self.set_status("agent_status_sign_in")
            self.sign_in()  # _finish says when it is connected
        self.ready.wait()
        download.join()  # the hub's profiles need the browser
        if not os.getenv("FOXPROFILE_LANG"):
            # Speak the hub's language unless this PC picked one.
            with contextlib.suppress(OSError, ValueError):
                set_language(_request(self.client.url + "/api/v1/info").get("language", ""))
        self.agent.run()
        if self.icon is not None:  # updated: leave the tray, main() restarts
            self.icon.stop()

    def run_tray(self) -> bool:
        """Run in the tray until Quit; False when this system has no tray."""
        try:
            import pystray
            from PIL import Image
        except Exception:  # not installed (Linux) or no display server
            return False
        item = pystray.MenuItem
        menu = pystray.Menu(
            item(lambda _: self.status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            item(
                lambda _: get_string(
                    "agent_menu_switch" if self.ready.is_set() else "agent_menu_sign_in"
                ),
                lambda: threading.Thread(target=self.sign_in, daemon=True).start(),
            ),
            item(
                lambda _: get_string("agent_menu_panel"),
                lambda: webbrowser.open(self.client.url),
                enabled=lambda _: bool(self.client.url),
            ),
            *(
                [
                    item(
                        lambda _: get_string("agent_menu_change_hub"),
                        lambda: threading.Thread(target=self.change_hub, daemon=True).start(),
                    )
                ]
                if self.dev
                else []
            ),
            item(lambda _: get_string("agent_menu_logs"), open_logs),
            item(lambda _: get_string("agent_menu_quit"), self.quit),
        )
        self.icon = pystray.Icon("foxprofile-agent", Image.open(ICON), self.title, menu)

        def setup(icon) -> None:
            icon.visible = True
            threading.Thread(target=self.work, daemon=True).start()

        self.icon.run(setup=setup)  # main thread: required on macOS
        return True

    def quit(self) -> None:
        say(get_string("agent_stopping"))
        if self.signin is not None:
            self.signin.close()
        self.agent.bl.shutdown_all()
        if self.icon is not None:
            self.icon.stop()


def _built(name: str) -> str:
    """A file the installer was built with: HUB_URL, CHANNEL (build-msi.sh / build-pkg.sh)."""
    with contextlib.suppress(OSError):
        return (agent_code.INSTALLED / name).read_text(encoding="utf-8").strip()
    return ""


def _built_hub() -> str:
    return _built("HUB_URL")


def _is_dev() -> bool:
    """A dev installer (AGENT_CHANNEL=dev), or the agent run from source."""
    return _built("CHANNEL") == "dev" or not (agent_code.INSTALLED / "BUILD").exists()


def start_hub(asked: str, creds: dict, built: str, dev: bool) -> tuple[str, dict]:
    """The hub to start with and the credentials still good for it.

    `asked` is --hub or FOXPROFILE_HUB_URL. A production agent stays on the hub it
    was built for; a dev agent keeps the last hub it signed in to, and the built
    one is only where it starts."""
    hub = asked.strip().rstrip("/")
    if not hub and not dev:
        hub = built
    if hub and creds.get("hub") not in (None, hub):
        creds = {}  # a key for another hub is no use here
    if not hub and not creds.get("hub"):
        hub = built
    return hub, creds


def main() -> None:
    parser = argparse.ArgumentParser(description="FoxProfile agent (runs hub profiles here)")
    parser.add_argument(
        "--hub",
        default=HUB_URL,
        help="Hub URL (default: FOXPROFILE_HUB_URL, then the one the installer was built for)",
    )
    parser.add_argument("--home", action="store_true", help="Run from the per-user data folder")
    parser.add_argument("--console", action="store_true", help="Stay in the terminal, no tray")
    args = parser.parse_args()
    setup_logging(LOG_DIR)
    lock = _single_instance()
    if lock is None:
        say(get_string("agent_already_running"))
        return
    dev = _is_dev()
    hub_url, creds = start_hub(args.hub or "", _load_credentials(), _built_hub(), dev)
    app = App(hub_url, creds, dev)
    try:
        if args.console or not app.run_tray():
            app.work()
    except KeyboardInterrupt:
        say(get_string("agent_stopping"))
    finally:
        app.agent.bl.shutdown_all()
    if app.agent.restart:
        lock.close()  # the new process takes it
        _restart()


def _restart() -> None:
    """Start this agent again, with the code the update unpacked (the installer's
    hook picks it). Its own session, so launchd ending this job does not end it."""
    if os.name == "nt":
        subprocess.Popen(sys.orig_argv, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        subprocess.Popen(sys.orig_argv, start_new_session=True)


if __name__ == "__main__":
    main()
