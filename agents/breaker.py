"""A circuit breaker per language model provider (ADR-0002, failure handling diagram).

A provider that keeps failing is skipped for a while, so every question
does not first wait for it to time out:

    closed  --(5 failures within 60 s, or one rate-limit reply)-->  open
    open    --(30 s later, one call is let through as a probe)-->   half-open
    half-open --(probe works)-->  closed
    half-open --(probe fails)-->  open again

While a provider's breaker is open, the gateway goes straight to the next
provider. If every breaker is open, the question gets the usual "the
language model is unavailable" answer at once, instead of after a timeout.

A rate-limit reply (HTTP 429) opens the breaker straight away: free tiers
refuse every request for a while once their per-minute limit is reached,
so trying again within seconds only adds waiting.
"""

import time
from collections import deque
from collections.abc import Callable
from enum import StrEnum

from core.logging import get_logger

log = get_logger(__name__)


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        failures: int = 5,
        window_seconds: float = 60.0,
        open_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        on_change: Callable[[str, BreakerState], None] | None = None,
    ) -> None:
        self.name = name
        self.failures = failures
        self.window_seconds = window_seconds
        self.open_seconds = open_seconds
        self.clock = clock
        self.on_change = on_change
        self.state = BreakerState.CLOSED
        self._recent: deque[float] = deque()
        self._opened_at = 0.0
        self._probing = False

    def allow(self) -> bool:
        """May a call go to this provider now?"""
        if self.state is BreakerState.CLOSED:
            return True
        if self.state is BreakerState.OPEN:
            if self.clock() - self._opened_at < self.open_seconds:
                return False
            self._move(BreakerState.HALF_OPEN)
        # Half-open: exactly one probe at a time.
        if self._probing:
            return False
        self._probing = True
        return True

    def success(self) -> None:
        self._probing = False
        self._recent.clear()
        if self.state is not BreakerState.CLOSED:
            self._move(BreakerState.CLOSED)

    def failure(self, *, rate_limited: bool = False) -> None:
        self._probing = False
        now = self.clock()
        if self.state is BreakerState.HALF_OPEN or rate_limited:
            self._open(now)
            return
        self._recent.append(now)
        while self._recent and now - self._recent[0] > self.window_seconds:
            self._recent.popleft()
        if len(self._recent) >= self.failures:
            self._open(now)

    def _open(self, now: float) -> None:
        self._opened_at = now
        self._recent.clear()
        self._move(BreakerState.OPEN)

    def _move(self, state: BreakerState) -> None:
        if state is self.state:
            return
        log.warning("llm_breaker_changed", provider=self.name, state=state.value)
        self.state = state
        if self.on_change is not None:
            self.on_change(self.name, state)


def is_rate_limit(error: BaseException) -> bool:
    """Whether a provider error means "too many requests" (HTTP 429).

    Provider SDKs use different error classes, so the check looks at the
    status code if there is one, and at the class name otherwise.
    """
    for attribute in ("status_code", "code", "status"):
        if getattr(error, attribute, None) == 429:
            return True
    response = getattr(error, "response", None)
    if getattr(response, "status_code", None) == 429:
        return True
    return "ratelimit" in type(error).__name__.lower().replace("_", "")
