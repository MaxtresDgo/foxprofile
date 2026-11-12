"""Per-launch loopback bridge: the extension can submit reports, nothing else."""

from __future__ import annotations

import json
import secrets
import threading
import urllib.error
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from pydantic import ValidationError

from ..profile.reports import ReportConflict, ReportInput


class ReportBridge:
    def __init__(self, submit: Callable[[ReportInput], dict]) -> None:
        self.token = secrets.token_urlsafe(32)
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Tokens and notes never go into the HTTP log.

            def setup(self):
                super().setup()
                self.connection.settimeout(20)

            def respond(self, status: int, body: dict):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                origin = self.headers.get("Origin")
                if (
                    self.path != "/report"
                    or self.headers.get("Host") != f"127.0.0.1:{bridge.server.server_port}"
                    or (
                        origin
                        and urlparse(origin).scheme not in ("chrome-extension", "moz-extension")
                    )
                    or not secrets.compare_digest(
                        self.headers.get("X-Report-Token", ""), bridge.token
                    )
                ):
                    return self.respond(403, {"error": "Report permission denied"})
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if (
                        not 0 < size <= 8192
                        or self.headers.get_content_type() != "application/json"
                    ):
                        return self.respond(400, {"error": "Invalid report body"})
                    body = ReportInput.model_validate_json(self.rfile.read(size))
                    result = submit(body)
                except ValidationError:
                    return self.respond(422, {"error": "Invalid report fields"})
                except ReportConflict:
                    return self.respond(
                        409, {"error": "This report was already sent with different data"}
                    )
                except urllib.error.HTTPError as e:
                    status = e.code if e.code in (400, 401, 403, 404, 409, 422) else 503
                    return self.respond(
                        status, {"error": "Hub rejected the report; reopen the profile or retry"}
                    )
                except ValueError:
                    return self.respond(
                        400, {"error": "Account changed; reopen the profile before reporting"}
                    )
                except Exception:
                    return self.respond(503, {"error": "Report could not be saved; retry"})
                self.respond(200, result)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/report"
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.1), daemon=True
        )
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def config(self) -> dict:
        return {"url": self.url, "token": self.token}
