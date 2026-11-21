"""The sandbox's HTTP server. Built on the standard library only.

    POST /run      {"code": "...", "mode": "analysis" | "plan", "data": <any JSON>}
                   -> {"ok": ..., "result": ..., "stdout": ..., "error": ..., "status": ...,
                       "duration_ms": ...}
    GET  /healthz  -> {"status": "ok"}

Only the API can reach it: the container sits on an internal Docker
network that the API shares and nothing else does, with no internet access
(compose.yaml). Requests larger than `MAX_REQUEST_BYTES` are refused.

Run with:  python -s -E -m sandbox.server   (port 8100)
"""

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from sandbox.runner import Limits, run
from sandbox.validator import Mode

MAX_REQUEST_BYTES = 256 * 1024
PORT = int(os.environ.get("SANDBOX_PORT", "8100"))
LIMITS = Limits(
    wall_seconds=float(os.environ.get("SANDBOX_WALL_SECONDS", "5")),
    cpu_seconds=int(os.environ.get("SANDBOX_CPU_SECONDS", "5")),
    memory_mb=int(os.environ.get("SANDBOX_MEMORY_MB", "256")),
)


class Handler(BaseHTTPRequestHandler):
    server_version = "sandbox"
    sys_version = ""

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._reply(HTTPStatus.OK, {"status": "ok"})
        else:
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/run":
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request too large"})
            return
        try:
            body = json.loads(self.rfile.read(length))
            code = body["code"]
            mode = Mode(body.get("mode", "analysis"))
            if not isinstance(code, str):
                raise TypeError("code must be text")
        except (ValueError, KeyError, TypeError):
            self._reply(HTTPStatus.BAD_REQUEST, {"error": "expected code and mode"})
            return
        outcome = run(code, mode, body.get("data"), LIMITS)
        self._reply(HTTPStatus.OK, outcome.to_json())

    def _reply(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        # One short line per request, without the code (it may hold data).
        print(json.dumps({"event": "sandbox_request", "line": format % args}), flush=True)


def main() -> None:
    # All interfaces of its own container, which sits on an internal network
    # shared with the API only (compose.yaml).
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)  # noqa: S104  # nosec B104
    print(json.dumps({"event": "sandbox_started", "port": PORT}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
