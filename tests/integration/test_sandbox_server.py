"""The sandbox's HTTP server, started on a free local port."""

import json
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from sandbox.server import MAX_REQUEST_BYTES, Handler


@pytest.fixture
def server_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _post(url: str, body: Any) -> httpx.Response:
    return httpx.post(f"{url}/run", content=json.dumps(body), timeout=30)


def test_health(server_url: str) -> None:
    assert httpx.get(f"{server_url}/healthz").json() == {"status": "ok"}


def test_code_runs(server_url: str) -> None:
    reply = _post(server_url, {"code": "result = sum(data)", "mode": "analysis", "data": [1, 2]})
    assert reply.status_code == 200
    assert reply.json()["result"] == 3


def test_a_plan_runs(server_url: str) -> None:
    reply = _post(server_url, {"code": "result = [search('x')]", "mode": "plan"})
    assert reply.json()["result"][0]["op"] == "search"


@pytest.mark.parametrize(
    "body",
    [{"mode": "analysis"}, {"code": 1}, {"code": "x", "mode": "shell"}, "not an object"],
)
def test_bad_requests_get_400(server_url: str, body: Any) -> None:
    assert _post(server_url, body).status_code == 400


def test_a_request_that_is_too_large_is_refused(server_url: str) -> None:
    body = {"code": "x", "data": "a" * MAX_REQUEST_BYTES}
    assert _post(server_url, body).status_code == 413


def test_unknown_paths_get_404(server_url: str) -> None:
    assert httpx.get(f"{server_url}/run").status_code == 404
    assert httpx.post(f"{server_url}/other", content="{}").status_code == 404
