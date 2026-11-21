"""Rate limits through the API: 429s, Retry-After, the budget, and admin changes."""

import json
from typing import Any

import fakeredis
import httpx
import pytest
from fastapi import FastAPI

from agents.checkpoint import memory_checkpointer
from api.main import create_app
from core.config import Settings
from security.rate_limit import RedisStore
from security.roles import Role
from tests.helpers.agents import FakeSearcher, agent_service, hit, result_with
from tests.helpers.chat import answering_model
from tests.helpers.llm import ScriptedChatModel
from tests.helpers.sse import post_stream
from tests.helpers.tokens import TOKENS, TestUser, bearer, bearer_for

# Each scripted model call reports 120 tokens; a normal question makes 3.
TOKENS_PER_ANSWER = 360


def _app(settings: Settings, model: ScriptedChatModel, **limits: Any) -> FastAPI:
    settings = settings.model_copy(update=limits)
    app = create_app(
        settings,
        signing_keys=TOKENS.signing_keys,
        rate_limit_store=RedisStore(fakeredis.FakeAsyncRedis()),
    )
    app.state.agents = agent_service(
        model, FakeSearcher([result_with(hit("RB-001"))]), checkpointer=memory_checkpointer()
    )
    return app


async def _get(app: FastAPI, path: str, role: Role) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return await http.get(path, headers=bearer(role))


async def _send(
    app: FastAPI, method: str, path: str, role: Role, body: Any = None
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return await http.request(method, path, headers=bearer(role), json=body)


async def test_questions_over_the_limit_get_429_with_retry_after(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    model = answering_model(*["Answer [1]."] * 5)
    app = _app(settings, model, rate_limit_burst=2, rate_limit_viewer_per_minute=6)

    replies = [await post_stream(app, "/chat", {"message": f"q{n}"}) for n in range(3)]

    assert [r.status for r in replies] == [200, 200, 429]
    refused = replies[2]
    assert refused.headers["retry-after"] == "10"
    error = json.loads(refused.raw)["error"]
    assert error["code"] == "rate_limited"
    assert error["details"] == {"retry_after_seconds": 10, "limit": "requests"}
    assert "Please wait 10 seconds" in error["message"]
    # The refused question never reached the agents.
    assert len(model.prompts_for("text")) == 2
    log = capsys.readouterr().out
    assert '"rate_limited"' in log
    assert '"logger": "security"' in log


async def test_one_users_limit_does_not_affect_another(settings: Settings) -> None:
    app = _app(settings, answering_model(*["A [1]."] * 4), rate_limit_burst=1)
    alice, bob = TestUser("alice", Role.VIEWER), TestUser("bob", Role.VIEWER)

    first = await post_stream(app, "/chat", {"message": "q"}, role=None, headers=bearer_for(alice))
    again = await post_stream(app, "/chat", {"message": "q"}, role=None, headers=bearer_for(alice))
    other = await post_stream(app, "/chat", {"message": "q"}, role=None, headers=bearer_for(bob))

    assert (first.status, again.status, other.status) == (200, 429, 200)


async def test_the_budget_is_charged_with_the_tokens_the_models_used(settings: Settings) -> None:
    app = _app(settings, answering_model("Answer [1]."))

    await post_stream(app, "/chat", {"message": "pool?"})

    budget = (await _get(app, "/me", Role.VIEWER)).json()["budget"]
    assert budget["used"] == TOKENS_PER_ANSWER
    assert budget["limit"] == settings.rate_limit_viewer_daily_tokens
    assert 0 < budget["resets_in_seconds"] <= 86_400


async def test_leaving_early_keeps_the_reservation(settings: Settings) -> None:
    app = _app(settings, answering_model("A long answer that streams [1]."))

    await post_stream(
        app, "/chat", {"message": "pool?"}, disconnect_when=lambda events: len(events) >= 2
    )

    budget = (await _get(app, "/me", Role.VIEWER)).json()["budget"]
    assert budget["used"] == settings.rate_limit_tokens_per_question


async def test_a_used_up_budget_gets_429_until_midnight(settings: Settings) -> None:
    app = _app(
        settings,
        answering_model(*["A [1]."] * 3),
        rate_limit_viewer_daily_tokens=900,
        rate_limit_tokens_per_question=600,
    )

    first = await post_stream(app, "/chat", {"message": "q1"})  # settles to 360
    second = await post_stream(app, "/chat", {"message": "q2"})  # 360 + 600 > 900

    assert (first.status, second.status) == (200, 429)
    error = json.loads(second.raw)["error"]
    assert error["details"]["limit"] == "daily_tokens"
    assert "resets at 00:00 UTC" in error["message"]
    assert int(second.headers["retry-after"]) <= 86_400


async def test_a_question_refused_for_another_reason_gives_its_tokens_back(
    settings: Settings,
) -> None:
    app = _app(settings, answering_model("A [1]."))
    app.state.threads.start("thread-0001", owner="someone-else")

    refused = await post_stream(app, "/chat", {"message": "q", "thread_id": "thread-0001"})

    assert refused.status == 404
    assert (await _get(app, "/me", Role.VIEWER)).json()["budget"]["used"] == 0


async def test_an_admin_can_lower_a_limit_and_it_applies_at_once(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    app = _app(settings, answering_model(*["A [1]."] * 3), rate_limit_burst=5)

    changed = await _send(
        app,
        "PUT",
        "/admin/rate-limits/viewer",
        Role.ADMIN,
        {"per_minute": 1, "daily_tokens": 5_000},
    )
    assert changed.status_code == 200
    assert changed.json()["limits"]["viewer"] == {"per_minute": 1, "daily_tokens": 5_000}
    assert changed.json()["store"] == "redis"

    statuses = [(await post_stream(app, "/chat", {"message": f"q{n}"})).status for n in range(2)]
    assert statuses == [200, 429]
    assert '"rate_limits_changed"' in capsys.readouterr().out

    reset = await _send(app, "DELETE", "/admin/rate-limits/viewer", Role.ADMIN)
    assert reset.json()["limits"]["viewer"] == reset.json()["defaults"]["viewer"]


@pytest.mark.parametrize(
    "body",
    [
        {"per_minute": 0, "daily_tokens": 5_000},
        {"per_minute": 10, "daily_tokens": 5},
        {"per_minute": 10},
        {"per_minute": 10, "daily_tokens": 5_000, "burst": 1_000},
    ],
)
async def test_bad_limits_are_refused(settings: Settings, body: dict[str, int]) -> None:
    app = _app(settings, answering_model())
    response = await _send(app, "PUT", "/admin/rate-limits/viewer", Role.ADMIN, body)
    assert response.status_code == 422


async def test_an_unknown_role_is_refused(settings: Settings) -> None:
    app = _app(settings, answering_model())
    body = {"per_minute": 10, "daily_tokens": 5_000}
    response = await _send(app, "PUT", "/admin/rate-limits/root", Role.ADMIN, body)
    assert response.status_code == 422


async def test_analysts_cannot_change_limits(settings: Settings) -> None:
    app = _app(settings, answering_model())
    body = {"per_minute": 600, "daily_tokens": 100_000_000}
    response = await _send(app, "PUT", "/admin/rate-limits/analyst", Role.ANALYST, body)
    assert response.status_code == 403


async def test_the_429_count_is_on_metrics(settings: Settings) -> None:
    app = _app(settings, answering_model(*["A [1]."] * 2), rate_limit_burst=1)
    await post_stream(app, "/chat", {"message": "q1"})
    await post_stream(app, "/chat", {"message": "q2"})

    metrics = (await _get(app, "/metrics", Role.VIEWER)).text
    assert 'rate_limit_rejections_total{limit="requests",role="viewer"} 1.0' in metrics
    assert f'rate_limit_tokens_charged_total{{role="viewer"}} {TOKENS_PER_ANSWER}.0' in metrics
