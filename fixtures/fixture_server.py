"""
Ephemeral loopback fixture server for the seeded fault corpus.

Binds port 0 (OS-assigned) so parallel test runs and a developer's own browser
never collide -- the defect the progress log records for fixed ports.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse

from fixtures.fault_corpus import CASES_BY_ID


def _make_handler():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, status, headers, body):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _dispatch(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/" or path == "/index.json":
                payload = {"cases": sorted(CASES_BY_ID), "count": len(CASES_BY_ID)}
                return self._send(200, {"Content-Type": "application/json"}, json.dumps(payload))
            if path.startswith("/case/"):
                case = CASES_BY_ID.get(path[len("/case/"):].strip("/"))
                if not case:
                    return self._send(404, {"Content-Type": "application/json"}, '{"error":"no case"}')
                return self._send(200, {"Content-Type": "text/html; charset=utf-8"}, case["html"])
            # Endpoints are shared across cases; first matching spec wins.
            for case in CASES_BY_ID.values():
                spec = case["endpoints"].get(path)
                if spec is not None:
                    delay = spec.get("delay_ms") or 0
                    if delay:
                        time.sleep(delay / 1000.0)
                    return self._send(spec["status"], spec.get("headers") or {}, spec.get("body") or "")
            return self._send(404, {"Content-Type": "application/json"}, '{"error":"not found"}')

        do_GET = _dispatch
        do_POST = _dispatch
        do_PUT = _dispatch
        do_OPTIONS = _dispatch
        do_HEAD = _dispatch

    return Handler


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Browsers routinely reset keep-alive sockets on close; that is not
        # a fixture failure and must not pollute benchmark output.
        import sys as _sys
        exc = _sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class FixtureServer:
    def __init__(self):
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def case_url(self, case_id: str) -> str:
        return f"{self.base_url}/case/{case_id}"

    def __enter__(self) -> "FixtureServer":
        self._server = _QuietServer(("127.0.0.1", 0), _make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        return False
