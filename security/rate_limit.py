"""Rate limiting: questions per minute, and a daily token budget per user.

Two limits are checked before a question runs (`RateLimiter.admit`):

1. **Questions per minute**, as token buckets. Each user has a bucket sized
   by their role, and all users of one role share a second bucket. A
   bucket holds up to `burst` questions and refills at the role's rate per
   minute. A question takes one from each bucket, and only if both have
   one; otherwise the answer is 429 with `Retry-After` set to the time
   until the emptiest bucket has one again.
2. **A daily budget of language model tokens** per user, by role. The
   question reserves a typical cost before it runs. When the answer ends,
   the reservation is corrected to what the models really used
   (`settle`). A question the user abandons keeps its reservation, so
   leaving early never dodges the budget. The budget resets at 00:00 UTC.

The counts live in Redis (`RedisStore`), so every API worker sees the same
numbers. The bucket update is one Lua script, which Redis runs atomically,
so two requests at the same moment can never both take the last question.

If Redis stops answering, each worker carries on with its own counts in
memory (`MemoryStore`), with every limit multiplied by
`rate_limit_fallback_factor` (0.5), because the workers can no longer see
each other's counts. A warning is logged and the `rate_limit_degraded`
metric is set. When Redis answers again, it takes over.

Admins can change a role's limits while the API runs. The change is kept
in Redis, so every worker picks it up within a few seconds.

Users are stored by their pseudonym (security/auth.py), never their user ID.
"""

import json
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, TypeVar

import redis.asyncio as redis
from prometheus_client import CollectorRegistry, Counter, Gauge

from core.config import Settings
from core.errors import RateLimitError, ServiceUnavailableError
from core.logging import SECURITY_LOGGER, get_logger
from security.roles import Role

security_log = get_logger(SECURITY_LOGGER)
log = get_logger(__name__)

T = TypeVar("T")

# How long the admins' limit changes are cached by each worker.
OVERRIDES_CACHE_SECONDS = 5.0
# At most one "Redis is down" warning per this many seconds.
FALLBACK_WARNING_SECONDS = 60.0
# Errors that mean Redis cannot be used right now.
STORE_ERRORS = (redis.RedisError, OSError, TimeoutError)

TOO_MANY_QUESTIONS = (
    "You are asking questions faster than allowed. Please wait {wait} and try again."
)
BUDGET_USED = (
    "You have used today's question budget. It resets at 00:00 UTC, in {wait}. "
    "An administrator can raise it if needed."
)


# ---------------------------------------------------------------
# Limits
# ---------------------------------------------------------------


@dataclass(frozen=True)
class RoleLimits:
    """The limits for each user with one role."""

    per_minute: int
    daily_tokens: int

    def scaled(self, factor: float) -> "RoleLimits":
        return RoleLimits(
            per_minute=max(1, math.floor(self.per_minute * factor)),
            daily_tokens=max(1, math.floor(self.daily_tokens * factor)),
        )


def default_limits(settings: Settings) -> dict[Role, RoleLimits]:
    return {
        Role.VIEWER: RoleLimits(
            settings.rate_limit_viewer_per_minute, settings.rate_limit_viewer_daily_tokens
        ),
        Role.ANALYST: RoleLimits(
            settings.rate_limit_analyst_per_minute, settings.rate_limit_analyst_daily_tokens
        ),
        Role.ADMIN: RoleLimits(
            settings.rate_limit_admin_per_minute, settings.rate_limit_admin_daily_tokens
        ),
    }


@dataclass(frozen=True)
class Bucket:
    key: str
    capacity: float
    per_second: float


@dataclass(frozen=True)
class TakeResult:
    allowed: bool
    # Seconds until the request would be allowed. 0 when allowed.
    retry_after: float
    # Questions left in the user's own bucket after this one.
    remaining: float


def refill(tokens: float, last: float, now: float, capacity: float, per_second: float) -> float:
    """A bucket's content at `now`: what it had, plus what dripped in since, up to full."""
    return min(capacity, tokens + max(0.0, now - last) * per_second)


# ---------------------------------------------------------------
# Where the counts are kept
# ---------------------------------------------------------------


class Store(Protocol):
    name: str

    async def take(self, buckets: Sequence[Bucket], now: float) -> TakeResult:
        """Take one question from every bucket, or from none of them."""
        ...

    async def add_usage(self, key: str, amount: int, ttl_seconds: int) -> int:
        """Add to a usage counter (may be negative) and return the new total."""
        ...

    async def usage(self, key: str) -> int: ...

    async def overrides(self) -> dict[Role, RoleLimits]: ...

    async def set_override(self, role: Role, limits: RoleLimits | None) -> None: ...

    async def aclose(self) -> None: ...


@dataclass
class MemoryStore:
    """Counts in this process only. Used when there is no Redis, or it is down."""

    name: str = "memory"
    clock: Callable[[], float] = time.time
    _buckets: dict[str, tuple[float, float]] = field(default_factory=dict)
    _usage: dict[str, tuple[int, float]] = field(default_factory=dict)
    _overrides: dict[Role, RoleLimits] = field(default_factory=dict)

    async def take(self, buckets: Sequence[Bucket], now: float) -> TakeResult:
        levels = []
        wait = 0.0
        for bucket in buckets:
            tokens, last = self._buckets.get(bucket.key, (bucket.capacity, now))
            level = refill(tokens, last, now, bucket.capacity, bucket.per_second)
            levels.append(level)
            if level < 1:
                wait = max(wait, (1 - level) / bucket.per_second)
        if wait > 0:
            return TakeResult(allowed=False, retry_after=wait, remaining=levels[0])
        for bucket, level in zip(buckets, levels, strict=True):
            self._buckets[bucket.key] = (level - 1, now)
        return TakeResult(allowed=True, retry_after=0.0, remaining=levels[0] - 1)

    async def add_usage(self, key: str, amount: int, ttl_seconds: int) -> int:
        current = await self.usage(key)
        total = max(0, current + amount)
        expires = self._usage.get(key, (0, self.clock() + ttl_seconds))[1]
        self._usage[key] = (total, expires)
        return total

    async def usage(self, key: str) -> int:
        value, expires = self._usage.get(key, (0, math.inf))
        if self.clock() >= expires:
            del self._usage[key]
            return 0
        return value

    async def overrides(self) -> dict[Role, RoleLimits]:
        return dict(self._overrides)

    async def set_override(self, role: Role, limits: RoleLimits | None) -> None:
        if limits is None:
            self._overrides.pop(role, None)
        else:
            self._overrides[role] = limits

    async def aclose(self) -> None:
        return None


# KEYS: the bucket keys. ARGV: now (seconds), then capacity and refill per
# second for each key. Takes one from every bucket, or from none.
# Returns {allowed (0 or 1), retry_after in ms, remaining in the first
# bucket x 1000}. Numbers are passed back as integers, because Redis turns
# Lua floats into integers on the way out.
_TAKE_SCRIPT = """
local now = tonumber(ARGV[1])
local levels = {}
local wait = 0
for i = 1, #KEYS do
  local capacity = tonumber(ARGV[2 * i])
  local rate = tonumber(ARGV[2 * i + 1])
  local saved = redis.call('HMGET', KEYS[i], 'tokens', 'at')
  local tokens = tonumber(saved[1]) or capacity
  local at = tonumber(saved[2]) or now
  local level = math.min(capacity, tokens + math.max(0, now - at) * rate)
  levels[i] = level
  if level < 1 then
    wait = math.max(wait, (1 - level) / rate)
  end
end
if wait > 0 then
  return {0, math.ceil(wait * 1000), math.floor(levels[1] * 1000)}
end
for i = 1, #KEYS do
  local capacity = tonumber(ARGV[2 * i])
  local rate = tonumber(ARGV[2 * i + 1])
  redis.call('HSET', KEYS[i], 'tokens', tostring(levels[i] - 1), 'at', tostring(now))
  -- A bucket that has refilled completely is the same as no bucket.
  redis.call('EXPIRE', KEYS[i], math.ceil(capacity / rate) + 1)
end
return {1, 0, math.floor((levels[1] - 1) * 1000)}
"""

_OVERRIDES_KEY = "ratelimit:limits"


class RedisStore:
    """Counts shared by every API worker."""

    name = "redis"

    def __init__(self, client: "redis.Redis") -> None:
        self.client = client
        self._take = client.register_script(_TAKE_SCRIPT)

    @classmethod
    def from_url(cls, url: str, *, timeout_seconds: float) -> "RedisStore":
        return cls(
            redis.Redis.from_url(
                url, socket_timeout=timeout_seconds, socket_connect_timeout=timeout_seconds
            )
        )

    async def take(self, buckets: Sequence[Bucket], now: float) -> TakeResult:
        args: list[Any] = [repr(now)]
        for bucket in buckets:
            args += [repr(bucket.capacity), repr(bucket.per_second)]
        allowed, wait_ms, remaining = await self._take(keys=[b.key for b in buckets], args=args)
        return TakeResult(
            allowed=bool(allowed), retry_after=int(wait_ms) / 1000, remaining=int(remaining) / 1000
        )

    async def add_usage(self, key: str, amount: int, ttl_seconds: int) -> int:
        async with self.client.pipeline(transaction=True) as pipe:
            pipe.incrby(key, amount)
            pipe.expire(key, ttl_seconds, nx=True)
            total, _ = await pipe.execute()
        if int(total) < 0:  # a correction larger than what was counted
            await self.client.set(key, 0, keepttl=True)
            return 0
        return int(total)

    async def usage(self, key: str) -> int:
        value = await self.client.get(key)
        return int(value) if value is not None else 0

    async def overrides(self) -> dict[Role, RoleLimits]:
        raw: Mapping[bytes | str, bytes | str] = await self.client.hgetall(_OVERRIDES_KEY)
        result: dict[Role, RoleLimits] = {}
        for key, value in raw.items():
            name = key.decode() if isinstance(key, bytes) else key
            try:
                role = Role(name)
                data = json.loads(value)
                result[role] = RoleLimits(int(data["per_minute"]), int(data["daily_tokens"]))
            except (ValueError, KeyError, TypeError):
                log.warning("rate_limit_override_ignored", role=name[:20])
        return result

    async def set_override(self, role: Role, limits: RoleLimits | None) -> None:
        if limits is None:
            await self.client.hdel(_OVERRIDES_KEY, role.value)
        else:
            value = json.dumps(
                {"per_minute": limits.per_minute, "daily_tokens": limits.daily_tokens}
            )
            await self.client.hset(_OVERRIDES_KEY, role.value, value)

    async def aclose(self) -> None:
        await self.client.aclose()


# ---------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------


class RateLimitMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        registry = registry or CollectorRegistry()
        self.rejected = Counter(
            "rate_limit_rejections_total",
            "Questions refused with 429, by the limit that was hit and the role.",
            ["limit", "role"],
            registry=registry,
        )
        self.tokens = Counter(
            "rate_limit_tokens_charged_total",
            "Language model tokens charged to users' daily budgets, by role.",
            ["role"],
            registry=registry,
        )
        self.degraded = Gauge(
            "rate_limit_degraded",
            "1 while Redis is down and this worker keeps its own, stricter counts.",
            registry=registry,
        )


# ---------------------------------------------------------------
# The limiter
# ---------------------------------------------------------------


@dataclass(frozen=True)
class Admission:
    """A question that was let through, and what it reserved from the budget."""

    user: str
    role: Role
    budget_key: str
    reserved: int
    # The store the reservation was made in, so it is corrected in the same one.
    store: Store | None = None


@dataclass(frozen=True)
class Usage:
    used: int
    limit: int
    resets_in: int


class RateLimiter:
    def __init__(
        self,
        settings: Settings,
        store: Store | None = None,
        *,
        metrics: RateLimitMetrics | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.enabled = settings.rate_limit_enabled
        self.burst = settings.rate_limit_burst
        self.role_per_minute = settings.rate_limit_role_per_minute
        self.tokens_per_question = settings.rate_limit_tokens_per_question
        self.fallback_factor = settings.rate_limit_fallback_factor
        self.defaults = default_limits(settings)
        self.metrics = metrics or RateLimitMetrics()
        self.clock = clock
        # Without Redis (a local run with `make run-api`), memory is the
        # normal store and the full limits apply.
        self.store: Store = store or MemoryStore(clock=clock)
        self.fallback = MemoryStore(name="memory-fallback", clock=clock)
        self.degraded = False
        self._last_warning = -math.inf
        self._overrides: dict[Role, RoleLimits] = {}
        self._overrides_read_at = -math.inf

    # --- the store, with the in-memory fallback ---

    async def _use(self, operation: Callable[[Store], Awaitable[T]]) -> tuple[T, Store]:
        try:
            result = await operation(self.store)
        except STORE_ERRORS as error:
            self._fall_back(error)
            return await operation(self.fallback), self.fallback
        if self.degraded:
            self.degraded = False
            self.metrics.degraded.set(0)
            log.info("rate_limit_store_recovered", store=self.store.name)
        return result, self.store

    def _fall_back(self, error: BaseException) -> None:
        self.degraded = True
        self.metrics.degraded.set(1)
        now = self.clock()
        if now - self._last_warning >= FALLBACK_WARNING_SECONDS:
            self._last_warning = now
            log.warning(
                "rate_limit_store_unavailable",
                store=self.store.name,
                error=type(error).__name__,
                fallback_factor=self.fallback_factor,
            )

    # --- limits ---

    async def limits(self) -> dict[Role, RoleLimits]:
        """The limits in force for each role: the settings, with the admins' changes."""
        now = self.clock()
        if now - self._overrides_read_at >= OVERRIDES_CACHE_SECONDS:
            try:
                self._overrides = await self.store.overrides()
            except STORE_ERRORS as error:
                self._fall_back(error)  # keep the last values we read
            self._overrides_read_at = now
        return {role: self._overrides.get(role, self.defaults[role]) for role in Role}

    async def _limits_for(self, role: Role, store: Store) -> RoleLimits:
        limits = (await self.limits())[role]
        return limits.scaled(self.fallback_factor) if store is self.fallback else limits

    async def set_limits(self, role: Role, limits: RoleLimits | None) -> None:
        """Change a role's limits for every worker, or go back to the settings (None)."""
        try:
            await self.store.set_override(role, limits)
        except STORE_ERRORS as error:
            # Saving it in this worker only would make workers disagree.
            self._fall_back(error)
            raise ServiceUnavailableError(
                "The new limits could not be saved because Redis is unavailable."
            ) from error
        self._overrides_read_at = -math.inf  # this worker sees it at once

    # --- one question ---

    async def admit(self, user: str, role: Role) -> Admission:
        """Let one question through, or raise RateLimitError (429)."""
        if not self.enabled:
            return Admission(user=user, role=role, budget_key="", reserved=0)
        now = self.clock()

        async def take(store: Store) -> TakeResult:
            limits = await self._limits_for(role, store)
            factor = self.fallback_factor if store is self.fallback else 1.0
            role_per_minute = max(1, math.floor(self.role_per_minute * factor))
            buckets = [
                # The user's own bucket: `burst` questions back to back, then
                # the role's rate per minute.
                Bucket(
                    f"ratelimit:user:{user}",
                    capacity=min(self.burst, limits.per_minute),
                    per_second=limits.per_minute / 60,
                ),
                # Shared by every user of the role: up to a minute's worth at once.
                Bucket(
                    f"ratelimit:role:{role.value}",
                    capacity=role_per_minute,
                    per_second=role_per_minute / 60,
                ),
            ]
            return await store.take(buckets, now)

        taken, _ = await self._use(take)
        if not taken.allowed:
            raise self._refuse("requests", role, user, math.ceil(taken.retry_after))

        key = self._budget_key(user, now)
        reserved = self.tokens_per_question

        async def reserve(store: Store) -> tuple[int, int]:
            limit = (await self._limits_for(role, store)).daily_tokens
            total = await store.add_usage(key, reserved, self._seconds_to_midnight(now) + 60)
            if total > limit:
                await store.add_usage(key, -reserved, 60)  # give it back
            return total, limit

        (total, limit), store = await self._use(reserve)
        if total > limit:
            raise self._refuse("daily_tokens", role, user, self._seconds_to_midnight(now))
        return Admission(user=user, role=role, budget_key=key, reserved=reserved, store=store)

    async def settle(self, admission: Admission, tokens_used: int) -> None:
        """Replace the question's reservation with what the models really used."""
        if not admission.reserved:
            return
        self.metrics.tokens.labels(role=admission.role.value).inc(max(0, tokens_used))
        await self._correct(admission, tokens_used - admission.reserved)

    async def refund(self, admission: Admission) -> None:
        """Give the reservation back, for a question that never ran (404, 409)."""
        if admission.reserved:
            await self._correct(admission, -admission.reserved)

    async def _correct(self, admission: Admission, delta: int) -> None:
        if delta == 0:
            return
        store = admission.store or self.store
        try:
            await store.add_usage(admission.budget_key, delta, self._seconds_to_midnight() + 60)
        except STORE_ERRORS as error:
            # The reservation stays as it was; that only ever counts too much.
            self._fall_back(error)

    async def usage_today(self, user: str, role: Role) -> Usage:
        now = self.clock()
        key = self._budget_key(user, now)

        async def read(store: Store) -> tuple[int, int]:
            return await store.usage(key), (await self._limits_for(role, store)).daily_tokens

        (used, limit), _ = await self._use(read)
        return Usage(used=used, limit=limit, resets_in=self._seconds_to_midnight(now))

    # --- helpers ---

    def _refuse(self, limit: str, role: Role, user: str, retry_after: int) -> RateLimitError:
        self.metrics.rejected.labels(limit=limit, role=role.value).inc()
        security_log.warning(
            "rate_limited", limit=limit, role=role.value, user=user, retry_after=retry_after
        )
        template = TOO_MANY_QUESTIONS if limit == "requests" else BUDGET_USED
        return RateLimitError(
            template.format(wait=describe_wait(retry_after)), retry_after=retry_after, limit=limit
        )

    def _budget_key(self, user: str, now: float) -> str:
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        return f"ratelimit:tokens:{user}:{day}"

    def _seconds_to_midnight(self, now: float | None = None) -> int:
        moment = datetime.fromtimestamp(self.clock() if now is None else now, UTC)
        midnight = (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(1, math.ceil((midnight - moment).total_seconds()))

    async def aclose(self) -> None:
        await self.store.aclose()


def describe_wait(seconds: int) -> str:
    """'45 seconds', '3 minutes', '5 hours 12 minutes': for the user, not exact."""
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = math.ceil(seconds / 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, minutes = divmod(minutes, 60)
    text = f"{hours} hour{'s' if hours != 1 else ''}"
    return text + (f" {minutes} minute{'s' if minutes != 1 else ''}" if minutes else "")
