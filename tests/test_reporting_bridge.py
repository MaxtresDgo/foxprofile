import json
import urllib.error
import urllib.request

import pytest

from src.services.browser.reporting import ReportBridge


def send(bridge, body, token=None, origin=None):
    headers = {"Content-Type": "application/json", "X-Report-Token": token or bridge.token}
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(bridge.url, data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_bridge_limits_token_origin_payload_and_confirms_callback():
    received = []
    bridge = ReportBridge(lambda body: received.append(body) or {"id": body.request_id})
    other = ReportBridge(lambda body: {"id": body.request_id})
    body = {"request_id": "a" * 32, "platform": "x", "status": "videoselfie"}
    try:
        assert send(bridge, body, token=other.token)[0] == 403
        assert send(bridge, body, origin="https://facebook.com")[0] == 403
        assert send(bridge, {**body, "profile": "other"})[0] == 422
        assert send(bridge, {**body, "note": "x" * 1001})[0] == 422
        assert send(bridge, body) == (200, {"id": "a" * 32})
        assert len(received) == 1 and received[0].platform == "x"
    finally:
        bridge.close()
        other.close()
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(bridge.url, timeout=1)


def test_bridge_reports_failure_without_claiming_success():
    def failed(body):
        raise OSError("do not expose internal paths")

    bridge = ReportBridge(failed)
    try:
        status, result = send(
            bridge, {"request_id": "a" * 32, "platform": "x", "status": "suspendido"}
        )
        assert status == 503 and "do not expose" not in str(result)
    finally:
        bridge.close()
