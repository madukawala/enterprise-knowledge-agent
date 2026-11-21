"""`POST /tools/python` through the API, with the real sandbox code in-process."""

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from agents.tools.sandbox import SandboxClient
from api.main import create_app
from core.config import Settings
from security.roles import Role
from tests.helpers.sandbox import LocalSandbox
from tests.helpers.tokens import TOKENS, bearer


def _app(settings: Settings, sandbox: Any = None) -> FastAPI:
    return create_app(settings, signing_keys=TOKENS.signing_keys, sandbox=sandbox or LocalSandbox())


async def _post(app: FastAPI, body: Any, role: Role = Role.ANALYST) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return await http.post("/tools/python", json=body, headers=bearer(role))


async def test_an_analyst_runs_python_on_data(settings: Settings) -> None:
    body = {
        "code": "import statistics\nprint('n =', len(data))\nresult = statistics.median(data)",
        "data": [3, 9, 4, 10],
    }
    response = await _post(_app(settings), body)

    assert response.status_code == 200
    assert response.json() | {"duration_ms": 0} == {
        "ok": True,
        "result": 6.5,
        "stdout": "n = 4\n",
        "error": None,
        "status": "ok",
        "duration_ms": 0,
    }


async def test_code_that_is_not_allowed_is_reported_and_logged(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    response = await _post(_app(settings), {"code": "import os\nresult = os.environ"})

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "rejected"
    assert "importing `os` is not allowed" in body["error"]
    log = [json.loads(line) for line in capsys.readouterr().out.splitlines() if "{" in line]
    refused = [line for line in log if line.get("event") == "sandbox_refused"]
    assert refused[0]["logger"] == "security"
    assert refused[0]["status"] == "rejected"


async def test_viewers_cannot_use_the_tool_and_nothing_runs(settings: Settings) -> None:
    sandbox = LocalSandbox()
    response = await _post(_app(settings, sandbox), {"code": "result = 1"}, role=Role.VIEWER)

    assert response.status_code == 403
    assert sandbox.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"code": ""},
        {"code": "x" * 8_001},
        {"code": "result = 1", "role": "admin"},
        {"code": "result = 1", "data": ["x" * 1000] * 200},
    ],
)
async def test_bad_requests_are_refused(settings: Settings, body: dict[str, Any]) -> None:
    sandbox = LocalSandbox()
    response = await _post(_app(settings, sandbox), body)
    assert response.status_code == 422
    assert sandbox.calls == []


async def test_without_a_sandbox_the_tool_says_it_is_not_configured(settings: Settings) -> None:
    app = create_app(settings, signing_keys=TOKENS.signing_keys)
    assert app.state.sandbox is None  # SANDBOX_URL is not set in tests

    response = await _post(app, {"code": "result = 1"})

    assert response.status_code == 503
    assert (
        response.json()["error"]["message"]
        == "The Python sandbox is not configured on this server."
    )


async def test_a_sandbox_that_does_not_answer_gives_502(settings: Settings) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = SandboxClient(
        "http://sandbox:8100", timeout_seconds=1, transport=httpx.MockTransport(refuse)
    )
    response = await _post(_app(settings, client), {"code": "result = 1"})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "tool_error"


async def test_the_client_sends_the_code_and_reads_the_reply() -> None:
    seen: list[dict[str, Any]] = []

    def sandbox(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": 2, "status": "ok"})

    client = SandboxClient(
        "http://sandbox:8100/", timeout_seconds=1, transport=httpx.MockTransport(sandbox)
    )
    result = await client.run("result = 1 + 1", mode="plan", data=[1])

    assert result.result == 2
    assert seen == [{"code": "result = 1 + 1", "mode": "plan", "data": [1]}]
    await client.aclose()


async def test_an_odd_reply_from_the_sandbox_is_a_tool_error() -> None:
    from core.errors import ToolError

    client = SandboxClient(
        "http://sandbox:8100",
        timeout_seconds=1,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"what": 1})),
    )
    with pytest.raises(ToolError):
        await client.run("result = 1", mode="analysis")


async def test_tool_runs_are_counted_and_rate_limited(settings: Settings) -> None:
    app = create_app(
        settings.model_copy(update={"rate_limit_burst": 2}),
        signing_keys=TOKENS.signing_keys,
        sandbox=LocalSandbox(),
    )
    statuses = [(await _post(app, {"code": "result = 1"})).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        metrics = (await http.get("/metrics")).text
    assert 'tool_runs_total{status="ok",tool="python"} 2.0' in metrics
    # The tool calls no model, so the token budget is untouched.
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        me = (await http.get("/me", headers=bearer(Role.ANALYST))).json()
    assert me["budget"]["used"] == 0
