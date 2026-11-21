"""Readiness checks behind `/readyz`.

Each check is a small async function that raises if its service is not
usable. All checks run at the same time, each with its own time limit, so
one slow service cannot hold up the whole probe.

Checks are either required or optional:

- A failed **required** check (Postgres, Redis) makes the API "not_ready"
  and `/readyz` returns 503. Docker then marks the container unhealthy and
  services that depend on the API wait.
- A failed **optional** check makes the API "degraded" but still ready:
  the Python sandbox (without it the Python tool answers 503 and research
  uses its fixed plan).

The response says only "ok", "failed" or "timeout" per check. The reason is
written to the log, never returned, because driver errors can contain host
names and user names.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal

import httpx
import psycopg
import redis.asyncio as redis

from core.config import Settings
from core.logging import get_logger

logger = get_logger(__name__)

CheckResult = Literal["ok", "failed", "timeout"]
ReadinessStatus = Literal["ready", "degraded", "not_ready"]
Probe = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    probe: Probe
    required: bool = True


@dataclass(frozen=True)
class ReadinessReport:
    status: ReadinessStatus
    checks: dict[str, CheckResult]

    @property
    def is_ready(self) -> bool:
        return self.status != "not_ready"


async def _run_one(check: ReadinessCheck, timeout_seconds: float) -> CheckResult:
    try:
        async with asyncio.timeout(timeout_seconds):
            await check.probe()
    except TimeoutError:
        logger.warning("readiness_check_timeout", check=check.name, timeout_s=timeout_seconds)
        return "timeout"
    except Exception as exc:
        # Only the error type is logged. The message may contain connection details.
        logger.warning("readiness_check_failed", check=check.name, error_type=type(exc).__name__)
        return "failed"
    return "ok"


async def run_checks(checks: Sequence[ReadinessCheck], timeout_seconds: float) -> ReadinessReport:
    results = await asyncio.gather(*(_run_one(check, timeout_seconds) for check in checks))
    by_name = {check.name: result for check, result in zip(checks, results, strict=True)}

    required_failed = any(
        result != "ok" for check, result in zip(checks, results, strict=True) if check.required
    )
    if required_failed:
        status: ReadinessStatus = "not_ready"
    elif all(result == "ok" for result in results):
        status = "ready"
    else:
        status = "degraded"
    return ReadinessReport(status=status, checks=by_name)


# ---------------------------------------------------------------
# Probes for the services in Docker Compose
# ---------------------------------------------------------------


def postgres_probe(url: str) -> Probe:
    async def probe() -> None:
        async with await psycopg.AsyncConnection.connect(url) as conn:
            await conn.execute("SELECT 1")

    return probe


def redis_probe(url: str) -> Probe:
    async def probe() -> None:
        client = redis.Redis.from_url(url)
        try:
            if not await client.ping():
                raise ConnectionError("Redis did not answer PING")
        finally:
            await client.aclose()

    return probe


def http_probe(url: str) -> Probe:
    async def probe() -> None:
        async with httpx.AsyncClient() as http:
            response = await http.get(url)
            response.raise_for_status()

    return probe


def build_checks(settings: Settings) -> list[ReadinessCheck]:
    """Return the checks for every service that has a connection string set."""
    checks: list[ReadinessCheck] = []
    if settings.postgres_url and settings.postgres_url.get_secret_value():
        checks.append(
            ReadinessCheck("postgres", postgres_probe(settings.postgres_url.get_secret_value()))
        )
    if settings.redis_url and settings.redis_url.get_secret_value():
        checks.append(ReadinessCheck("redis", redis_probe(settings.redis_url.get_secret_value())))
    if settings.sandbox_url:
        sandbox_health = settings.sandbox_url.rstrip("/") + "/healthz"
        checks.append(ReadinessCheck("sandbox", http_probe(sandbox_health), required=False))
    return checks
