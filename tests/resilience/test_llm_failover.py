"""The circuit breaker per model provider, on its own and inside the gateway."""

import time

import httpx
import pytest
from langchain_core.messages import HumanMessage

from agents.breaker import BreakerState, CircuitBreaker, is_rate_limit
from agents.llm import ChatProvider, LLMGateway
from agents.telemetry import AgentMetrics
from core.errors import LLMUnavailableError
from tests.helpers.llm import ScriptedChatModel


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class RateLimitError(Exception):
    """Named like the provider SDKs' 429 errors."""


class StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


# ---------------------------------------------------------------
# The breaker
# ---------------------------------------------------------------


def _breaker(clock: Clock) -> CircuitBreaker:
    return CircuitBreaker("gemini", failures=3, window_seconds=60, open_seconds=30, clock=clock)


def test_it_opens_after_enough_failures_inside_the_window() -> None:
    clock = Clock()
    breaker = _breaker(clock)
    breaker.failure()
    breaker.failure()
    assert breaker.allow()
    breaker.failure()
    assert breaker.state is BreakerState.OPEN
    assert not breaker.allow()


def test_old_failures_do_not_count() -> None:
    clock = Clock()
    breaker = _breaker(clock)
    breaker.failure()
    breaker.failure()
    clock.now += 61
    breaker.failure()
    assert breaker.state is BreakerState.CLOSED


def test_a_success_resets_the_count() -> None:
    breaker = _breaker(Clock())
    breaker.failure()
    breaker.failure()
    breaker.success()
    breaker.failure()
    breaker.failure()
    assert breaker.state is BreakerState.CLOSED


def test_after_the_wait_one_probe_is_let_through() -> None:
    clock = Clock()
    breaker = _breaker(clock)
    breaker.failure(rate_limited=True)
    clock.now += 30

    assert breaker.allow()  # the probe
    assert breaker.state is BreakerState.HALF_OPEN
    assert not breaker.allow()  # only one at a time


def test_a_good_probe_closes_it_and_a_bad_one_opens_it_again() -> None:
    clock = Clock()
    breaker = _breaker(clock)
    breaker.failure(rate_limited=True)
    clock.now += 30
    breaker.allow()
    breaker.failure()
    assert breaker.state is BreakerState.OPEN

    clock.now += 30
    breaker.allow()
    breaker.success()
    assert breaker.state is BreakerState.CLOSED
    assert breaker.allow()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RateLimitError(), True),
        (StatusError(429), True),
        (StatusError(500), False),
        (
            httpx.HTTPStatusError(
                "x",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(429),
            ),
            True,
        ),
        (TimeoutError(), False),
    ],
)
def test_rate_limit_errors_are_recognised(error: Exception, expected: bool) -> None:
    assert is_rate_limit(error) is expected


# ---------------------------------------------------------------
# Inside the gateway
# ---------------------------------------------------------------


def _gateway(gemini: ScriptedChatModel, openrouter: ScriptedChatModel, clock: Clock) -> LLMGateway:
    metrics = AgentMetrics()
    gateway = LLMGateway(
        [
            ChatProvider("gemini", "gemini-test", gemini),
            ChatProvider("openrouter", "openrouter-test", openrouter),
        ],
        timeout_seconds=1,
        metrics=metrics,
    )
    gateway.breakers = {
        name: CircuitBreaker(
            name,
            failures=2,
            window_seconds=60,
            open_seconds=30,
            clock=clock,
            on_change=lambda provider, state: metrics.llm_breaker_state.labels(
                provider=provider
            ).set({"closed": 0, "half_open": 1, "open": 2}[state.value]),
        )
        for name in ("gemini", "openrouter")
    }
    return gateway


QUESTION = [HumanMessage("pool?")]


async def test_a_failing_provider_is_skipped_once_its_breaker_opens() -> None:
    clock = Clock()
    gemini = ScriptedChatModel(texts=[RuntimeError("down"), RuntimeError("down"), "never"])
    openrouter = ScriptedChatModel(texts=["backup 1", "backup 2", "backup 3"])
    gateway = _gateway(gemini, openrouter, clock)

    for n in (1, 2, 3):
        text, call = await gateway.text(QUESTION, purpose="answer")
        assert text == f"backup {n}"
        assert call.fallback

    assert len(gemini.calls) == 2  # the third question did not wait for Gemini
    assert gateway.breakers["gemini"].state is BreakerState.OPEN
    gauge = gateway.metrics.llm_breaker_state.labels(provider="gemini")  # type: ignore[union-attr]
    assert gauge._value.get() == 2


async def test_a_rate_limit_reply_skips_the_provider_at_once() -> None:
    clock = Clock()
    gemini = ScriptedChatModel(texts=[RateLimitError(), "never"])
    openrouter = ScriptedChatModel(texts=["backup 1", "backup 2"])
    gateway = _gateway(gemini, openrouter, clock)

    await gateway.text(QUESTION, purpose="answer")
    await gateway.text(QUESTION, purpose="answer")

    assert len(gemini.calls) == 1


async def test_the_provider_is_used_again_after_a_good_probe() -> None:
    clock = Clock()
    gemini = ScriptedChatModel(texts=[RateLimitError(), "gemini is back", "gemini again"])
    openrouter = ScriptedChatModel(texts=["backup"])
    gateway = _gateway(gemini, openrouter, clock)
    await gateway.text(QUESTION, purpose="answer")

    clock.now += 31
    text, call = await gateway.text(QUESTION, purpose="answer")
    assert (text, call.provider, call.fallback) == ("gemini is back", "gemini", False)
    text, _ = await gateway.text(QUESTION, purpose="answer")
    assert text == "gemini again"
    assert gateway.breakers["gemini"].state is BreakerState.CLOSED


async def test_when_every_breaker_is_open_the_answer_is_immediate() -> None:
    clock = Clock()
    gemini = ScriptedChatModel(texts=[RateLimitError()])
    openrouter = ScriptedChatModel(texts=[RateLimitError()])
    gateway = _gateway(gemini, openrouter, clock)
    with pytest.raises(LLMUnavailableError):
        await gateway.text(QUESTION, purpose="answer")

    started = time.perf_counter()
    with pytest.raises(LLMUnavailableError):
        await gateway.text(QUESTION, purpose="answer")
    assert time.perf_counter() - started < 0.05
    assert len(gemini.calls) == len(openrouter.calls) == 1


async def test_a_bad_answer_moves_on_but_does_not_trip_the_breaker() -> None:
    clock = Clock()
    gemini = ScriptedChatModel(texts=["", "", "", "fine"])
    openrouter = ScriptedChatModel(texts=["backup"] * 3)
    gateway = _gateway(gemini, openrouter, clock)

    for _ in range(3):
        _, call = await gateway.text(QUESTION, purpose="answer")
        assert call.provider == "openrouter"
    _, call = await gateway.text(QUESTION, purpose="answer")

    assert call.provider == "gemini"
    assert gateway.breakers["gemini"].state is BreakerState.CLOSED
