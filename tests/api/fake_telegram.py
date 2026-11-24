"""A fake Telegram Bot API on 127.0.0.1 (US-087): records every request, answers like Telegram.

Only for tests and the private run. It never talks to the real api.telegram.org.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Made up, right shape (6-12 digits, a colon, 30+ of [A-Za-z0-9_-]), obviously not real.
FAKE_TOKEN = "424242424:FAKEFAKEFAKEfakefakefake_not_a_real_token_0"
FAKE_CHAT = "5550001"


class FakeTelegram:
    """`url` is what TELEGRAM_API_BASE must be; `sent` are the sendMessage texts in order."""

    def __init__(self, token: str = FAKE_TOKEN, port: int = 0) -> None:
        self.token = token
        self.requests: list[tuple[str, str, dict[str, Any]]] = []  # (method, path, json body)
        self.updates: list[dict[str, Any]] = []
        self.send_status = 200
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence the test output
                return

            def _answer(self, status: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    body = {}
                with fake._lock:
                    fake.requests.append((method, self.path, body))
                prefix = f"/bot{fake.token}/"
                if not self.path.startswith(prefix):
                    self._answer(401, {"ok": False, "description": "Unauthorized"})
                elif self.path == prefix + "getUpdates":
                    self._answer(200, {"ok": True, "result": list(fake.updates)})
                elif self.path == prefix + "sendMessage":
                    if fake.send_status != 200:
                        self._answer(fake.send_status, {"ok": False, "description": "nope"})
                    else:
                        n = len(fake.sent) + 1000
                        self._answer(200, {"ok": True, "result": {"message_id": n}})
                else:
                    self._answer(404, {"ok": False, "description": "Not Found"})

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def sent(self) -> list[str]:
        """The text of every sendMessage received, oldest first."""
        with self._lock:
            return [b.get("text", "") for _, p, b in self.requests if p.endswith("/sendMessage")]

    def paths(self) -> list[str]:
        with self._lock:
            return [p for _, p, _ in self.requests]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
