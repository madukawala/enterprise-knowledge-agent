import httpx


async def test_healthz_reports_the_process_is_up(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_reports_readiness_and_checks(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {}}


async def test_every_response_has_a_request_id(client: httpx.AsyncClient) -> None:
    first = await client.get("/healthz")
    second = await client.get("/healthz")

    assert first.headers["X-Request-ID"]
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]


async def test_callers_request_id_is_kept(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": "ui-1234"})

    assert response.headers["X-Request-ID"] == "ui-1234"


async def test_api_docs_are_available_outside_production(client: httpx.AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert "/healthz" in response.json()["paths"]
