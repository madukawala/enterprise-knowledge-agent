"""Health endpoints used by Docker health checks and the README quick start.

- `/healthz` answers as long as the process is running.
- `/readyz` reports whether the API can serve requests. Checks for Postgres,
  Redis, Pinecone, the MCP server and the language models are added here as
  each of those dependencies is introduced.
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"]
    checks: dict[str, Literal["ok", "down"]]


@router.get("/healthz")
async def healthz() -> HealthResponse:
    return HealthResponse()


@router.get("/readyz")
async def readyz() -> ReadinessResponse:
    checks: dict[str, Literal["ok", "down"]] = {}
    status: Literal["ready", "degraded"] = (
        "ready" if all(result == "ok" for result in checks.values()) else "degraded"
    )
    return ReadinessResponse(status=status, checks=checks)
