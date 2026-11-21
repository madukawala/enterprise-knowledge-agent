"""The chat stream when things go wrong: the client leaves, the run is too
slow, or something fails that nobody planned for."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastapi import FastAPI

from agents.events import AgentEvent, AgentStateEvent
from core.config import Environment, LogFormat, Settings
from core.logging import configure_logging
from security.roles import Role
from tests.helpers.agents import FakeSearcher, gateway, hit, result_with
from tests.helpers.chat import answering_model, chat_app
from tests.helpers.sse import post_stream
from tests.helpers.tokens import TEST_USERS

LONG_ANSWER = " ".join(["The pool was exhausted [1]."] * 20)


def _settings(**values: Any) -> Settings:
    return Settings(_env_file=None, app_env=Environment.TEST, **values)


def _metric(app: FastAPI, outcome: str) -> float:
    return app.state.chat_metrics.streams.labels(outcome=outcome)._value.get()  # type: ignore[no-any-return]


async def _eventually(check: Any, seconds: float = 2.0) -> None:
    """Wait for background clean-up (the run's task) to finish."""
    async with asyncio.timeout(seconds):
        # Polling is fine here: the state is set by a task the test does not own.
        while not check():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


async def test_the_run_stops_when_the_client_disconnects() -> None:
    """Leaving mid-answer cancels the model call; the backup model is never asked."""
    slow = answering_model(LONG_ANSWER, token_delay=0.02)
    backup = answering_model("Backup answer [1].")
    app = chat_app(_settings(), gateway(slow, backup), FakeSearcher([result_with(hit("RB-1"))]))

    reply = await post_stream(
        app,
        "/chat",
        {"message": "pool?", "thread_id": "thread-left-early"},
        disconnect_when=lambda events: any(e["type"] == "token" for e in events),
    )

    assert "token" in reply.types
    assert "response_end" not in reply.types
    await _eventually(lambda: _metric(app, "cancelled") == 1)
    assert backup.calls == []
    assert app.state.chat_metrics.active._value.get() == 0
    # The thread is free again, so the user can ask again straight away.
    app.state.threads.start("thread-left-early", owner=TEST_USERS[Role.VIEWER].user_id)


async def test_a_run_over_the_time_limit_ends_with_a_timeout_event() -> None:
    model = answering_model("Too late [1].", delay=1.0)
    app = chat_app(_settings(chat_timeout_seconds=0.2), model)

    reply = await post_stream(app, "/chat", {"message": "pool?"})

    assert reply.status == 200
    error = reply.events[-1]
    assert (error["type"], error["code"]) == ("error", "timeout")
    assert error["request_id"] == reply.headers["x-request-id"]
    assert _metric(app, "timeout") == 1


async def test_keep_alive_lines_are_sent_while_the_agents_are_quiet() -> None:
    model = answering_model("Slow but fine [1].", delay=0.15)
    app = chat_app(
        _settings(chat_keepalive_seconds=0.05), model, FakeSearcher([result_with(hit("RB-1"))])
    )

    reply = await post_stream(app, "/chat", {"message": "pool?"})

    assert reply.keepalives >= 1
    assert reply.types[-1] == "response_end"


class _BrokenAgents:
    """A service whose run fails half-way with an unexpected error."""

    traced = False

    def stream(self, question: str, **ids: Any) -> AsyncGenerator[AgentEvent]:
        async def events() -> AsyncGenerator[AgentEvent]:
            yield AgentStateEvent(status="accepted", run_id="r", thread_id=ids["thread_id"])
            raise RuntimeError("database password is hunter2")

        return events()


async def test_an_unexpected_failure_ends_with_a_safe_error_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO", LogFormat.JSON)
    app = chat_app(_settings(), answering_model())
    app.state.agents = _BrokenAgents()

    reply = await post_stream(app, "/chat", {"message": "pool?"})

    assert reply.types == ["agent_state", "error"]
    error = reply.events[-1]
    assert error["code"] == "internal_error"
    assert "hunter2" not in reply.raw
    # The details are in the log, with the request ID, for whoever investigates.
    log = capsys.readouterr().out
    assert "chat_stream_failed" in log
    assert reply.headers["x-request-id"] in log
    assert _metric(app, "error") == 1
