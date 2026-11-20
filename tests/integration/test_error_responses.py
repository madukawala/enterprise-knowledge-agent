"""Every error leaves the API in the same JSON shape, with the request ID."""

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from core.errors import PermissionDeniedError, RetrievalError


class Question(BaseModel):
    text: str
    top_k: int = 5


@pytest.fixture
def app(app: FastAPI) -> FastAPI:
    """The normal app plus a few routes that fail on purpose."""

    @app.get("/test/forbidden")
    async def forbidden() -> None:
        raise PermissionDeniedError()

    @app.get("/test/search-down")
    async def search_down() -> None:
        raise RetrievalError(details={"namespace": "policies"})

    @app.post("/test/questions")
    async def ask(question: Question) -> Question:
        return question

    return app


async def test_app_errors_use_their_status_code_and_code(client: httpx.AsyncClient) -> None:
    response = await client.get("/test/forbidden", headers={"X-Request-ID": "req-42"})

    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "code": "forbidden",
            "message": "Your role does not allow this action.",
            "request_id": "req-42",
        }
    }
    assert response.headers["X-Request-ID"] == "req-42"


async def test_app_error_details_are_returned(client: httpx.AsyncClient) -> None:
    response = await client.get("/test/search-down")

    assert response.status_code == 503
    assert response.json()["error"]["details"] == {"namespace": "policies"}


async def test_unknown_routes_return_the_standard_shape(client: httpx.AsyncClient) -> None:
    response = await client.get("/does-not-exist")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["request_id"] == response.headers["X-Request-ID"]


async def test_wrong_method_keeps_the_allow_header(client: httpx.AsyncClient) -> None:
    response = await client.post("/healthz")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"
    assert "GET" in response.headers["Allow"]


async def test_invalid_body_lists_the_fields_at_fault(client: httpx.AsyncClient) -> None:
    response = await client.post("/test/questions", json={"top_k": "many"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert {item["field"] for item in error["details"]["fields"]} == {"body.text", "body.top_k"}
