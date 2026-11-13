"""Real extension contexts; social tab URL is supplied without a real account."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chrome_engine.engine import ChromeEngine
from chrome_engine.runner import _extension_args
from src.api import app as app_module
from src.api import auth
from src.api.app import create_app
from src.core.container import Container
from src.services.browser import label
from src.services.browser.reporting import ReportBridge

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("engine", ["chrome", "camoufox"])
def test_extension_reports_and_panel_reviews(chrome_exe, tmp_path, monkeypatch, engine):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auth, "API_TOKEN", "")
    monkeypatch.setattr(app_module, "API_TOKEN", "")
    monkeypatch.setattr(label.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(label, "current_language", lambda: "es")
    container = Container(hub=False)
    pm = container.profile_manager
    pm.add_profile("report-demo", "", "macos")
    a = pm.accounts.add("report-demo", {"platform": "facebook", "username": "first"})
    b = pm.accounts.add("report-demo", {"platform": "facebook", "username": "second"})
    profile = pm.profiles["report-demo"]
    popup_results = []
    attempts = []

    def submit(body):
        if body.request_id == "f" * 32:
            popup_results.append(body.note)
            return {"id": body.request_id}
        receipt = container._report(profile, body)
        attempts.append(body.request_id)
        if len(attempts) == 1:
            raise OSError("Simulate a lost acknowledgement after saving")
        return receipt

    bridge = ReportBridge(submit)
    tag = {**label.current("report-demo"), **label.details("", [a, b]), "report": bridge.config()}
    folder = label.write_extension(tag, engine)
    if engine == "camoufox":
        # Juggler cannot inspect moz-extension pages. Drive the real popup DOM
        # from a test-only extension script, retaining its native API/network stack.
        script = Path(folder) / "badge.js"
        script.write_text(
            script.read_text()
            + "\n"
            + r"""
          const frame = document.createElement('iframe');
          frame.src = browser.runtime.getURL('popup.html');
          document.body.append(frame);
        """
        )
        html = Path(folder) / "popup.html"
        html.write_text(
            html.read_text().replace("</body>", '<script src="test-driver.js"></script></body>')
        )
        (Path(folder) / "test-driver.js").write_text(r"""
          (async () => {
            const config = await (await fetch('config.json')).json();
            const wait = async (fn) => {
              for (let n = 0; n < 200; n++) { if (fn()) return; await new Promise(r => setTimeout(r, 25)); }
              throw new Error('Popup condition timed out');
            };
            let result = 'ok';
            try {
              const $ = id => document.getElementById(id);
              await wait(() => $('message').textContent.length > 0);
              browser.tabs.query = async () => [{url:'https://www.facebook.com/'}];
              await init();
              $('status').value = 'suspendido'; $('note').value = '<script>unsafe</script> Checkpoint';
              if ($('report-form').checkValidity()) throw new Error('Account selection not required');
              $('account').value = config.accounts[1].id;
              $('report-form').requestSubmit();
              await wait(() => $('message').dataset.error === 'true' && !$('send').disabled);
              if (!$('note').value.includes('Checkpoint')) throw new Error('Draft lost');
              $('report-form').requestSubmit();
              await wait(() => $('message').textContent.includes('Reporte enviado'));
            } catch (e) { result = e.message; }
            await fetch(config.url, {method:'POST', headers:{'Content-Type':'application/json','X-Report-Token':config.token},
              body:JSON.stringify({request_id:'f'.repeat(32),platform:'facebook',status:'videoselfie',note:result})});
          })();
        """)
    client = TestClient(create_app(container), base_url="http://127.0.0.1:8765")

    async def run():
        if engine == "chrome":
            browser = ChromeEngine(
                tmp_path / "chrome",
                executable=chrome_exe,
                headless=True,
                geoip=False,
                proxy="http://127.0.0.1:9",
                extra_args=_extension_args([folder]),
            )
            popup_url = f"chrome-extension://{label.CHROME_ID}/popup.html"
        else:
            from camoufox.async_api import AsyncCamoufox
            from camoufox.pkgman import camoufox_path

            if not Path(camoufox_path()).exists():
                pytest.skip("Camoufox not installed")
            browser = AsyncCamoufox(
                headless=True,
                geoip=False,
                proxy={"server": "http://127.0.0.1:9"},
                addons=[folder],
                persistent_context=True,
                user_data_dir=str(tmp_path / "firefox"),
            )
        async with browser as context:
            errors = []
            if engine == "camoufox":
                for _ in range(200):
                    if popup_results:
                        break
                    await asyncio.sleep(0.05)
                assert popup_results == ["ok"]
            else:
                popup = await context.new_page()
                popup.on("pageerror", lambda error: errors.append(str(error)))
                await popup.goto(popup_url, wait_until="domcontentloaded")
                await popup.wait_for_function("typeof platformFor === 'function'")
                # A popup opened as a page is itself active; supply the social URL.
                await popup.evaluate("""async () => {
                  chrome.tabs.query = async () => [{url: 'https://www.facebook.com/'}];
                  await init();
                }""")
                await popup.select_option("#status", "suspendido")
                await popup.fill("#note", "<script>unsafe</script> Checkpoint")
                await popup.click("#send")
                assert not pm.reports.list(profile.uid)
                await popup.select_option("#account", b["id"])
                await popup.click("#send")
                await popup.locator('#message[data-error="true"]').wait_for()
                assert "Checkpoint" in await popup.input_value("#note")
                await popup.set_viewport_size({"width": 340, "height": 550})
                await popup.screenshot(
                    path=str(tmp_path / "report-popup.png"), animations="disabled"
                )
                await popup.click("#send")
                await popup.locator("#message").filter(has_text="Reporte enviado").wait_for()
            assert len(attempts) == 2 and attempts[0] == attempts[1]
            report = pm.reports.list(profile.uid)[0]
            assert report["account_id"] == b["id"] and report["status"] == "suspendido"
            assert not errors

            page = await context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))

            async def route(request_route):
                request = request_route.request
                from urllib.parse import urlsplit

                parsed = urlsplit(request.url)
                response = client.request(
                    request.method,
                    parsed.path + ("?" + parsed.query if parsed.query else ""),
                    content=request.post_data,
                    headers={"Accept-Language": "es"},
                )
                await request_route.fulfill(
                    status=response.status_code,
                    body=response.content,
                    headers={"Content-Type": response.headers.get("content-type", "text/plain")},
                )

            await page.route("http://127.0.0.1:8765/**", route)
            await page.goto("http://127.0.0.1:8765/?lang=es")
            await page.wait_for_selector("#app:not(.hidden)")
            await page.click("#reports-btn")
            await page.wait_for_selector("#modal-root article")
            await page.set_viewport_size({"width": 1440, "height": 900})
            await page.screenshot(path=str(tmp_path / "reports-panel.png"), animations="disabled")
            await page.set_viewport_size({"width": 320, "height": 720})
            assert await page.locator("#modal-root .modal").evaluate(
                "el => el.getBoundingClientRect().right <= innerWidth"
            )
            assert await page.locator("#modal-root article script").count() == 0
            assert "Checkpoint" in await page.locator("#modal-root article").inner_text()
            await page.locator("#modal-root article button").click()
            await page.wait_for_function(
                "document.querySelectorAll('#modal-root article').length === 0"
            )
            assert pm.reports.list(profile.uid)[0]["reviewed_at"] is not None
            assert not errors

    try:
        asyncio.run(run())
    finally:
        bridge.close()
