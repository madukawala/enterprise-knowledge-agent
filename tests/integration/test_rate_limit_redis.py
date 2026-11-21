"""The Redis store: the Lua token bucket, budgets, admin changes, and Redis failing.

fakeredis runs the real Lua script (through lupa), so the atomic bucket
update is tested as Redis would run it.
"""

import asyncio
import json
from typing import Any

import fakeredis
import pytest
import redis.asyncio as redis

from core.config import Settings
from core.errors import RateLimitError, ServiceUnavailableError
from security.rate_limit import Bucket, RateLimiter, RateLimitMetrics, RedisStore, RoleLimits
from security.roles import Role


@pytest.fixture
def client() -> fakeredis.FakeAsyncRedis:
    return fakeredis.FakeAsyncRedis()


@pytest.fixture
def store(client: fakeredis.FakeAsyncRedis) -> RedisStore:
    return RedisStore(client)


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "rate_limit_burst": 5,
        "rate_limit_viewer_per_minute": 6,
        "rate_limit_viewer_daily_tokens": 20_000,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


async def test_the_script_allows_the_burst_then_refills(store: RedisStore) -> None:
    bucket = [Bucket("ratelimit:user:u", capacity=3, per_second=0.1)]

    results = [await store.take(bucket, now=1_000.0) for _ in range(4)]

    assert [r.allowed for r in results] == [True, True, True, False]
    assert results[3].retry_after == pytest.approx(10, abs=0.01)
    assert results[2].remaining == pytest.approx(0)
    assert (await store.take(bucket, now=1_010.0)).allowed


async def test_the_script_takes_from_every_bucket_or_none(store: RedisStore) -> None:
    user = Bucket("ratelimit:user:u", capacity=5, per_second=1)
    role = Bucket("ratelimit:role:viewer", capacity=1, per_second=0.01)
    await store.take([role], now=0.0)

    refused = await store.take([user, role], now=0.0)

    assert not refused.allowed
    assert (await store.take([user], now=0.0)).remaining == pytest.approx(4)


async def test_buckets_expire_once_they_would_be_full_again(
    store: RedisStore, client: fakeredis.FakeAsyncRedis
) -> None:
    await store.take([Bucket("ratelimit:user:u", capacity=3, per_second=0.1)], now=0.0)
    ttl = await client.ttl("ratelimit:user:u")
    assert 0 < ttl <= 31


async def test_concurrent_questions_never_get_more_than_the_burst(store: RedisStore) -> None:
    """Twenty questions at the same moment: exactly five get through."""
    limiter = RateLimiter(_settings(rate_limit_viewer_per_minute=60), store)

    async def ask() -> bool:
        try:
            await limiter.admit("u_same", Role.VIEWER)
        except RateLimitError:
            return False
        return True

    results = await asyncio.gather(*(ask() for _ in range(20)))

    assert results.count(True) == 5


async def test_the_budget_is_shared_by_every_worker(store: RedisStore) -> None:
    """Two API workers, one Redis: the budget counts across both."""
    settings = _settings(rate_limit_burst=100, rate_limit_viewer_per_minute=600)
    first, second = RateLimiter(settings, store), RateLimiter(settings, store)

    for limiter in (first, second, first, second, first):
        await limiter.admit("u_1", Role.VIEWER)  # 5 x 4,000 = the whole 20,000

    with pytest.raises(RateLimitError) as caught:
        await second.admit("u_1", Role.VIEWER)
    assert caught.value.details["limit"] == "daily_tokens"


async def test_settling_and_the_budget_key_expiry(
    store: RedisStore, client: fakeredis.FakeAsyncRedis
) -> None:
    limiter = RateLimiter(_settings(), store)
    admission = await limiter.admit("u_1", Role.VIEWER)

    await limiter.settle(admission, 360)

    assert int(await client.get(admission.budget_key)) == 360
    assert 0 < await client.ttl(admission.budget_key) <= 86_400 + 60


async def test_an_admin_change_reaches_every_worker(
    store: RedisStore, client: fakeredis.FakeAsyncRedis
) -> None:
    first, second = RateLimiter(_settings(), store), RateLimiter(_settings(), store)

    await first.set_limits(Role.ANALYST, RoleLimits(per_minute=3, daily_tokens=9_000))

    assert (await second.limits())[Role.ANALYST] == RoleLimits(3, 9_000)
    saved = json.loads(await client.hget("ratelimit:limits", "analyst"))
    assert saved == {"per_minute": 3, "daily_tokens": 9_000}

    await first.set_limits(Role.ANALYST, None)
    assert await client.hget("ratelimit:limits", "analyst") is None


async def test_a_broken_override_in_redis_is_ignored(
    store: RedisStore, client: fakeredis.FakeAsyncRedis
) -> None:
    await client.hset("ratelimit:limits", "viewer", "not json")
    await client.hset("ratelimit:limits", "superuser", json.dumps({"per_minute": 1}))
    limiter = RateLimiter(_settings(), store)
    assert (await limiter.limits())[Role.VIEWER] == limiter.defaults[Role.VIEWER]


# ---------------------------------------------------------------
# Redis failing
# ---------------------------------------------------------------


class SwitchableRedis(RedisStore):
    """A Redis store that can be made to fail, like a Redis that went away."""

    def __init__(self) -> None:
        super().__init__(fakeredis.FakeAsyncRedis())
        self.down = False

    async def take(self, buckets: Any, now: float) -> Any:
        if self.down:
            raise redis.ConnectionError("Redis went away")
        return await super().take(buckets, now)

    async def add_usage(self, key: str, amount: int, ttl_seconds: int) -> int:
        if self.down:
            raise redis.ConnectionError("Redis went away")
        return await super().add_usage(key, amount, ttl_seconds)

    async def overrides(self) -> dict[Role, RoleLimits]:
        if self.down:
            raise TimeoutError()
        return await super().overrides()

    async def set_override(self, role: Role, limits: RoleLimits | None) -> None:
        if self.down:
            raise redis.ConnectionError("Redis went away")
        await super().set_override(role, limits)


async def test_redis_down_falls_back_to_stricter_counts_in_memory(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = SwitchableRedis()
    metrics = RateLimitMetrics()
    limiter = RateLimiter(
        _settings(rate_limit_burst=4, rate_limit_viewer_per_minute=4), store, metrics=metrics
    )
    store.down = True

    # Half the limit: 2 questions instead of 4, and no error for the user.
    await limiter.admit("u_1", Role.VIEWER)
    await limiter.admit("u_1", Role.VIEWER)
    with pytest.raises(RateLimitError):
        await limiter.admit("u_1", Role.VIEWER)

    assert limiter.degraded
    assert metrics.degraded._value.get() == 1
    output = capsys.readouterr().out
    assert output.count("rate_limit_store_unavailable") == 1  # one warning, not one per request


async def test_redis_coming_back_takes_over_again() -> None:
    store = SwitchableRedis()
    limiter = RateLimiter(_settings(), store)
    store.down = True
    await limiter.admit("u_1", Role.VIEWER)
    store.down = False

    await limiter.admit("u_1", Role.VIEWER)

    assert not limiter.degraded
    assert limiter.metrics.degraded._value.get() == 0


async def test_limits_cannot_be_changed_while_redis_is_down() -> None:
    store = SwitchableRedis()
    limiter = RateLimiter(_settings(), store)
    store.down = True
    with pytest.raises(ServiceUnavailableError):
        await limiter.set_limits(Role.VIEWER, RoleLimits(1, 1_000))
