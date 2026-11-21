import asyncio
from typing import Any

import pytest
import respx
from pydantic import SecretStr

from api import readiness
from api.readiness import (
    ReadinessCheck,
    build_checks,
    postgres_probe,
    redis_probe,
    run_checks,
)
from core.config import Settings


async def _ok() -> None:
    return None


async def _fail() -> None:
    raise ConnectionError("down")


async def _hang() -> None:
    await asyncio.sleep(10)


async def test_all_checks_passing_means_ready() -> None:
    report = await run_checks([ReadinessCheck("a", _ok), ReadinessCheck("b", _ok)], 1.0)

    assert report.status == "ready"
    assert report.checks == {"a": "ok", "b": "ok"}
    assert report.is_ready


async def test_no_checks_means_ready() -> None:
    report = await run_checks([], 1.0)

    assert report.status == "ready"
    assert report.checks == {}


async def test_a_failed_required_check_means_not_ready() -> None:
    report = await run_checks([ReadinessCheck("db", _fail), ReadinessCheck("cache", _ok)], 1.0)

    assert report.status == "not_ready"
    assert report.checks == {"db": "failed", "cache": "ok"}
    assert not report.is_ready


async def test_a_failed_optional_check_means_degraded_but_ready() -> None:
    report = await run_checks(
        [ReadinessCheck("db", _ok), ReadinessCheck("llm", _fail, required=False)], 1.0
    )

    assert report.status == "degraded"
    assert report.checks == {"db": "ok", "llm": "failed"}
    assert report.is_ready


async def test_a_slow_check_times_out() -> None:
    report = await run_checks([ReadinessCheck("slow", _hang)], 0.05)

    assert report.checks == {"slow": "timeout"}
    assert report.status == "not_ready"


def test_checks_are_built_only_for_services_with_a_url() -> None:
    settings = Settings(
        _env_file=None,
        postgres_url=SecretStr("postgresql://u:p@postgres:5432/app"),
        redis_url=SecretStr(""),
    )

    assert [check.name for check in build_checks(settings)] == ["postgres"]
    assert build_checks(Settings(_env_file=None)) == []


def test_checks_are_built_for_postgres_and_redis() -> None:
    settings = Settings(
        _env_file=None,
        postgres_url=SecretStr("postgresql://u:p@postgres:5432/app"),
        redis_url=SecretStr("redis://:p@redis:6379/0"),
    )

    checks = build_checks(settings)

    assert [check.name for check in checks] == ["postgres", "redis"]
    assert all(check.required for check in checks)


# Port 1 on the local machine is closed, so these fail straight away.
async def test_postgres_probe_fails_when_postgres_is_unreachable() -> None:
    probe = postgres_probe("postgresql://user:pw@127.0.0.1:1/app?connect_timeout=1")

    with pytest.raises(Exception):  # noqa: B017, PT011 - any driver error is a failure
        await probe()


async def test_redis_probe_fails_when_redis_is_unreachable() -> None:
    probe = redis_probe("redis://127.0.0.1:1/0")

    with pytest.raises(Exception):  # noqa: B017, PT011 - any driver error is a failure
        await probe()


async def test_postgres_probe_runs_a_simple_query(monkeypatch: pytest.MonkeyPatch) -> None:
    executed: list[str] = []

    class FakeConnection:
        async def __aenter__(self) -> "FakeConnection":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def execute(self, query: str) -> None:
            executed.append(query)

    async def fake_connect(url: str) -> FakeConnection:
        return FakeConnection()

    monkeypatch.setattr(readiness.psycopg.AsyncConnection, "connect", fake_connect)

    await postgres_probe("postgresql://u:p@postgres:5432/app")()

    assert executed == ["SELECT 1"]


class _FakeRedis:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.closed = False

    async def ping(self) -> bool:
        return self.answer

    async def aclose(self) -> None:
        self.closed = True


async def test_redis_probe_passes_when_redis_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis(answer=True)
    monkeypatch.setattr(readiness.redis.Redis, "from_url", lambda _url: fake)

    await redis_probe("redis://redis:6379/0")()

    assert fake.closed


async def test_redis_probe_fails_on_a_bad_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis(answer=False)
    monkeypatch.setattr(readiness.redis.Redis, "from_url", lambda _url: fake)

    with pytest.raises(ConnectionError):
        await redis_probe("redis://redis:6379/0")()
    assert fake.closed


async def test_the_sandbox_is_an_optional_check() -> None:
    settings = Settings(_env_file=None, sandbox_url="http://sandbox:8100")
    (check,) = build_checks(settings)
    assert (check.name, check.required) == ("sandbox", False)

    with respx.mock() as router:
        router.get("http://sandbox:8100/healthz").respond(503)
        report = await run_checks([check], timeout_seconds=1)
    assert report.status == "degraded"
