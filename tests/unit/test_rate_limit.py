"""Rate limiting maths and rules (security/rate_limit.py), with a fake clock.

The same rules run on Redis in tests/integration/test_rate_limit_redis.py.
"""

from datetime import UTC, datetime

import pytest

from core.config import Settings
from core.errors import RateLimitError
from security.rate_limit import (
    Bucket,
    MemoryStore,
    RateLimiter,
    RoleLimits,
    describe_wait,
    refill,
)
from security.roles import Role

# 2026-11-21 23:59:00 UTC: one minute before the daily budget resets.
ONE_MINUTE_TO_MIDNIGHT = datetime(2026, 11, 21, 23, 59, tzinfo=UTC).timestamp()
NOON = datetime(2026, 11, 21, 12, 0, tzinfo=UTC).timestamp()


class Clock:
    def __init__(self, now: float = NOON) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "rate_limit_burst": 3,
        "rate_limit_viewer_per_minute": 6,  # one question every 10 seconds
        "rate_limit_role_per_minute": 100,
        "rate_limit_viewer_daily_tokens": 100_000,
        "rate_limit_tokens_per_question": 4_000,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def _limiter(clock: Clock, **overrides: object) -> RateLimiter:
    return RateLimiter(_settings(**overrides), MemoryStore(clock=clock), clock=clock)


# ---------------------------------------------------------------
# The bucket
# ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("tokens", "elapsed", "expected"),
    [
        (0.0, 0, 0.0),
        (0.0, 10, 1.0),  # 0.1 per second
        (0.0, 15, 1.5),
        (2.5, 100, 3.0),  # never above capacity
        (1.0, -5, 1.0),  # a clock going backwards adds nothing
    ],
)
def test_a_bucket_refills_steadily_up_to_its_capacity(
    tokens: float, elapsed: float, expected: float
) -> None:
    assert refill(tokens, 100.0, 100.0 + elapsed, 3, 0.1) == pytest.approx(expected)


async def test_a_bucket_allows_the_burst_then_one_per_refill() -> None:
    store = MemoryStore()
    bucket = [Bucket("b", capacity=3, per_second=0.1)]

    results = [await store.take(bucket, now=0) for _ in range(4)]

    assert [r.allowed for r in results] == [True, True, True, False]
    assert results[3].retry_after == pytest.approx(10)
    assert (await store.take(bucket, now=9.9)).allowed is False
    assert (await store.take(bucket, now=10)).allowed is True


async def test_a_question_takes_from_every_bucket_or_from_none() -> None:
    store = MemoryStore()
    roomy = Bucket("user", capacity=5, per_second=1)
    empty = Bucket("role", capacity=1, per_second=0.01)
    await store.take([empty], now=0)  # the shared bucket is now empty

    refused = await store.take([roomy, empty], now=0)

    assert not refused.allowed
    assert refused.retry_after == pytest.approx(100)
    # The user's bucket was not charged for the refused question.
    assert (await store.take([roomy], now=0)).remaining == pytest.approx(4)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (1, "1 second"),
        (45, "45 seconds"),
        (60, "1 minute"),
        (61, "2 minutes"),
        (3600, "1 hour"),
        (5 * 3600 + 12 * 60, "5 hours 12 minutes"),
    ],
)
def test_waits_are_described_in_plain_words(seconds: int, text: str) -> None:
    assert describe_wait(seconds) == text


# ---------------------------------------------------------------
# Questions per minute
# ---------------------------------------------------------------


async def test_a_user_gets_the_burst_then_a_429_with_retry_after() -> None:
    clock = Clock()
    limiter = _limiter(clock)
    for _ in range(3):
        await limiter.admit("u_1", Role.VIEWER)

    with pytest.raises(RateLimitError) as caught:
        await limiter.admit("u_1", Role.VIEWER)

    error = caught.value
    assert error.retry_after == 10
    assert error.headers == {"Retry-After": "10"}
    assert error.details == {"retry_after_seconds": 10, "limit": "requests"}
    assert "Please wait 10 seconds" in error.message


async def test_the_bucket_refills_at_the_roles_rate() -> None:
    clock = Clock()
    limiter = _limiter(clock)
    for _ in range(3):
        await limiter.admit("u_1", Role.VIEWER)

    clock.advance(10)
    await limiter.admit("u_1", Role.VIEWER)
    with pytest.raises(RateLimitError):
        await limiter.admit("u_1", Role.VIEWER)


async def test_users_have_their_own_buckets() -> None:
    limiter = _limiter(Clock())
    for _ in range(3):
        await limiter.admit("u_1", Role.VIEWER)

    await limiter.admit("u_2", Role.VIEWER)  # not affected by u_1


async def test_all_users_of_a_role_share_one_bucket_too() -> None:
    limiter = _limiter(Clock(), rate_limit_role_per_minute=4)
    for user in ("u_1", "u_2", "u_3", "u_4"):
        await limiter.admit(user, Role.VIEWER)

    with pytest.raises(RateLimitError):
        await limiter.admit("u_5", Role.VIEWER)
    # Another role has its own shared bucket.
    await limiter.admit("u_6", Role.ANALYST)


async def test_higher_roles_get_more_questions_per_minute() -> None:
    limiter = _limiter(Clock(), rate_limit_burst=100, rate_limit_analyst_per_minute=12)
    analyst = [await limiter.admit("a", Role.ANALYST) for _ in range(12)]
    assert len(analyst) == 12
    with pytest.raises(RateLimitError):
        await limiter.admit("a", Role.ANALYST)


async def test_switched_off_lets_everything_through() -> None:
    limiter = _limiter(Clock(), rate_limit_enabled=False)
    for _ in range(50):
        admission = await limiter.admit("u_1", Role.VIEWER)
    assert admission.reserved == 0


# ---------------------------------------------------------------
# The daily token budget
# ---------------------------------------------------------------


async def test_each_question_reserves_tokens_and_is_settled_to_the_real_count() -> None:
    clock = Clock()
    limiter = _limiter(clock, rate_limit_viewer_daily_tokens=10_000)

    admission = await limiter.admit("u_1", Role.VIEWER)
    assert (await limiter.usage_today("u_1", Role.VIEWER)).used == 4_000

    await limiter.settle(admission, 1_234)
    usage = await limiter.usage_today("u_1", Role.VIEWER)
    assert usage.used == 1_234
    assert usage.limit == 10_000


async def test_a_question_that_would_go_over_the_budget_is_refused_until_midnight() -> None:
    clock = Clock(ONE_MINUTE_TO_MIDNIGHT)
    limiter = _limiter(
        clock,
        rate_limit_burst=10,
        rate_limit_viewer_per_minute=60,
        rate_limit_viewer_daily_tokens=10_000,
    )
    await limiter.admit("u_1", Role.VIEWER)
    await limiter.admit("u_1", Role.VIEWER)  # 8,000 of 10,000 reserved

    with pytest.raises(RateLimitError) as caught:
        await limiter.admit("u_1", Role.VIEWER)

    assert caught.value.details == {"retry_after_seconds": 60, "limit": "daily_tokens"}
    assert "It resets at 00:00 UTC, in 1 minute" in caught.value.message
    # The refused question gave its reservation back.
    assert (await limiter.usage_today("u_1", Role.VIEWER)).used == 8_000

    clock.advance(61)  # a new day
    await limiter.admit("u_1", Role.VIEWER)
    assert (await limiter.usage_today("u_1", Role.VIEWER)).used == 4_000


async def test_an_abandoned_question_keeps_its_reservation() -> None:
    """Leaving before the answer ends must not dodge the budget."""
    limiter = _limiter(Clock())
    await limiter.admit("u_1", Role.VIEWER)  # never settled
    assert (await limiter.usage_today("u_1", Role.VIEWER)).used == 4_000


async def test_a_refund_gives_the_whole_reservation_back() -> None:
    limiter = _limiter(Clock())
    admission = await limiter.admit("u_1", Role.VIEWER)
    await limiter.refund(admission)
    assert (await limiter.usage_today("u_1", Role.VIEWER)).used == 0


async def test_usage_never_goes_below_zero() -> None:
    limiter = _limiter(Clock())
    admission = await limiter.admit("u_1", Role.VIEWER)
    await limiter.refund(admission)
    await limiter.refund(admission)
    assert (await limiter.usage_today("u_1", Role.VIEWER)).used == 0


# ---------------------------------------------------------------
# Limits changed by an admin
# ---------------------------------------------------------------


async def test_an_admin_change_applies_at_once_and_can_be_undone() -> None:
    limiter = _limiter(Clock(), rate_limit_burst=10)
    await limiter.set_limits(Role.VIEWER, RoleLimits(per_minute=1, daily_tokens=50_000))

    assert (await limiter.limits())[Role.VIEWER] == RoleLimits(1, 50_000)
    await limiter.admit("u_1", Role.VIEWER)
    with pytest.raises(RateLimitError):
        await limiter.admit("u_1", Role.VIEWER)

    await limiter.set_limits(Role.VIEWER, None)
    assert (await limiter.limits())[Role.VIEWER] == limiter.defaults[Role.VIEWER]


def test_scaled_limits_never_reach_zero() -> None:
    assert RoleLimits(1, 1_000).scaled(0.1) == RoleLimits(1, 100)
    assert RoleLimits(10, 1_000).scaled(0.5) == RoleLimits(5, 500)
