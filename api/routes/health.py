"""Health endpoints used by Docker health checks and the README quick start.

- `/healthz` answers as long as the process is running. It never touches
  other services, so a database outage never makes the API look dead.
- `/readyz` checks the services the API needs (see `api/readiness.py`) and
  returns 503 when a required one is down. The API container's Docker health
  check calls this endpoint.
"""

from typing import Literal, cast

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from api.readiness import CheckResult, ReadinessCheck, ReadinessStatus, run_checks
from core.config import Settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    status: ReadinessStatus
    checks: dict[str, CheckResult]


@router.get("/healthz")
async def healthz() -> HealthResponse:
    return HealthResponse()


@router.get(
    "/readyz",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def readyz(request: Request, response: Response) -> ReadinessResponse:
    settings = cast(Settings, request.app.state.settings)
    checks = cast(list[ReadinessCheck], request.app.state.readiness_checks)

    report = await run_checks(checks, settings.readiness_timeout_seconds)
    if not report.is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status=report.status, checks=report.checks)
