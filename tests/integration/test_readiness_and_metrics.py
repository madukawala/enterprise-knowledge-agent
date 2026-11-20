import httpx
from fastapi import FastAPI

from api.main import create_app
from api.readiness import ReadinessCheck
from core.config import Settings


async def _ok() -> None:
    return None


async def _fail() -> None:
    raise ConnectionError("down")


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return await http.get(path)


async def test_readyz_lists_each_check(app: FastAPI) -> None:
    app.state.readiness_checks = [ReadinessCheck("postgres", _ok), ReadinessCheck("redis", _ok)]

    response = await _get(app, "/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"postgres": "ok", "redis": "ok"}}


async def test_readyz_returns_503_when_a_required_service_is_down(app: FastAPI) -> None:
    app.state.readiness_checks = [ReadinessCheck("postgres", _fail), ReadinessCheck("redis", _ok)]

    response = await _get(app, "/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"postgres": "failed", "redis": "ok"},
    }


async def test_readyz_stays_up_when_only_an_optional_service_is_down(app: FastAPI) -> None:
    app.state.readiness_checks = [
        ReadinessCheck("postgres", _ok),
        ReadinessCheck("llm", _fail, required=False),
    ]

    response = await _get(app, "/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


async def test_healthz_does_not_depend_on_other_services(app: FastAPI) -> None:
    app.state.readiness_checks = [ReadinessCheck("postgres", _fail)]

    response = await _get(app, "/healthz")

    assert response.status_code == 200


async def test_metrics_count_requests_but_not_health_probes(client: httpx.AsyncClient) -> None:
    await client.get("/openapi.json")
    await client.get("/healthz")

    response = await client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert 'handler="/openapi.json"' in response.text
    assert 'handler="/healthz"' not in response.text


async def test_metrics_can_be_switched_off(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"metrics_enabled": False}))

    response = await _get(app, "/metrics")

    assert response.status_code == 404
