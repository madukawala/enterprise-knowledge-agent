"""STRIDE: information disclosure through error responses and request IDs.

See docs/security/THREAT_MODEL.md. Errors must not reveal stack traces,
internal error text or values the user submitted.
"""

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel

INTERNAL_DETAIL = "connection to postgres://admin:s3cret@db:5432 failed"


class Login(BaseModel):
    username: str
    pin: int


@pytest.fixture
def app(app: FastAPI) -> FastAPI:
    @app.get("/test/crash")
    async def crash() -> None:
        raise RuntimeError(INTERNAL_DETAIL)

    @app.post("/test/login")
    async def login(body: Login) -> Login:
        return body

    return app


async def test_unexpected_errors_return_a_generic_message(client: httpx.AsyncClient) -> None:
    response = await client.get("/test/crash")

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert "s3cret" not in response.text
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text


async def test_unexpected_errors_are_logged_with_the_request_id(
    client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    response = await client.get("/test/crash", headers={"X-Request-ID": "crash-1"})

    logs = capsys.readouterr().out
    assert response.status_code == 500
    assert '"event": "request_failed"' in logs
    assert '"request_id": "crash-1"' in logs


async def test_validation_errors_do_not_echo_submitted_values(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/test/login", json={"username": "vera.viewer", "pin": "secret-pin-value"}
    )

    assert response.status_code == 422
    assert "secret-pin-value" not in response.text


@pytest.mark.parametrize(
    "unsafe_id",
    [
        "a" * 65,
        "id with spaces",
        "abcé",
        "<script>alert(1)</script>",
        '"; DROP TABLE audit_log; --',
    ],
)
async def test_unsafe_request_ids_are_replaced(client: httpx.AsyncClient, unsafe_id: str) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": unsafe_id.encode("utf-8")})

    returned = response.headers["X-Request-ID"]
    assert returned != unsafe_id
    assert len(returned) == 32
    assert returned.isalnum()
