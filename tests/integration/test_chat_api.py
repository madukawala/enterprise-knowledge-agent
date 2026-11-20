"""`POST /chat`: the SSE stream, follow-up questions, request checks and startup."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

import api.main
from agents.service import AgentService
from api.main import create_app
from core.config import Settings
from security.roles import Role
from tests.helpers.agents import FakeSearcher, agent_service, hit, result_with
from tests.helpers.chat import answering_model, chat_app
from tests.helpers.sse import post_stream


async def _post_json(app: FastAPI, body: object, **headers: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return await http.post("/chat", json=body, headers=headers)


def _searcher() -> FakeSearcher:
    return FakeSearcher([result_with(hit("RB-001"), hit("RB-002"))])


async def test_the_answer_streams_as_server_sent_events(settings: Settings) -> None:
    app = chat_app(settings, answering_model("Raise the pool size [1]."), _searcher())

    reply = await post_stream(app, "/chat", {"message": "Why is the pool exhausted?"})

    assert reply.status == 200
    assert reply.headers["content-type"].startswith("text/event-stream")
    assert reply.headers["cache-control"] == "no-cache"
    assert reply.types[0] == "agent_state"
    assert reply.types[-1] == "response_end"
    assert {"node_start", "node_end", "retrieval", "validation", "token"} <= set(reply.types)
    # Numbered 1, 2, 3, ... and labelled with the request, run and thread.
    events = reply.events
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert {e["request_id"] for e in events} == {reply.headers["x-request-id"]}
    assert events[0]["run_id"] == reply.headers["x-run-id"]
    assert events[0]["thread_id"] == reply.headers["x-thread-id"]
    final = events[-1]
    assert final["kind"] == "answered"
    assert final["text"] == "Raise the pool size [1]."
    assert final["citations"][0]["doc_id"] == "RB-001"
    assert "**Sources**" in final["markdown"]


async def test_a_follow_up_question_continues_the_same_conversation(settings: Settings) -> None:
    model = answering_model("The pool limit is 200 [1].", "Restart the workers [1].")
    app = chat_app(settings, model, _searcher())

    first = await post_stream(app, "/chat", {"message": "What is the pool limit?"})
    thread_id = first.headers["x-thread-id"]
    second = await post_stream(
        app, "/chat", {"message": "And what do I do then?", "thread_id": thread_id}
    )

    assert second.status == 200
    assert second.headers["x-thread-id"] == thread_id
    assert second.headers["x-run-id"] != first.headers["x-run-id"]
    assert second.events[-1]["text"] == "Restart the workers [1]."
    # The second answer was written with the first exchange in view.
    follow_up_prompt = model.prompts_for("text")[1]
    assert "What is the pool limit?" in follow_up_prompt
    assert "The pool limit is 200" in follow_up_prompt


async def test_the_role_comes_from_the_dev_header_and_defaults_to_viewer(
    settings: Settings,
) -> None:
    searcher = _searcher()
    app = chat_app(settings, answering_model("A [1].", "B [1]."), searcher)

    await post_stream(app, "/chat", {"message": "pool?"})
    await post_stream(app, "/chat", {"message": "pool?"}, headers={"X-Dev-Role": "analyst"})

    assert [role for _, role in searcher.requests] == [Role.VIEWER, Role.ANALYST]


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"message": ""}, "body.message"),
        ({"message": "   \n "}, "body.message"),
        ({"message": "x" * 2001}, "body.message"),
        ({"message": "hi", "thread_id": "../../etc"}, "body.thread_id"),
        ({"message": "hi", "thread_id": "short"}, "body.thread_id"),
        ({}, "body.message"),
    ],
)
async def test_bad_requests_are_refused_before_streaming(
    settings: Settings, body: dict[str, object], field: str
) -> None:
    model = answering_model()
    response = await _post_json(chat_app(settings, model), body)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert field in [item["field"] for item in error["details"]["fields"]]
    assert model.calls == []


async def test_an_unknown_role_header_is_refused(settings: Settings) -> None:
    response = await _post_json(
        chat_app(settings, answering_model()), {"message": "hi"}, **{"X-Dev-Role": "root"}
    )
    assert response.status_code == 422
    assert "viewer, analyst, admin" in response.json()["error"]["message"]


async def test_without_a_model_the_chat_says_it_is_unavailable(settings: Settings) -> None:
    response = await _post_json(create_app(settings), {"message": "hi"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_unavailable"


async def test_a_second_question_while_the_first_is_answering_gets_409(
    settings: Settings,
) -> None:
    app = chat_app(settings, answering_model("A [1]."), _searcher())
    app.state.threads.start("thread-0001", owner="role:viewer")

    response = await _post_json(app, {"message": "hi", "thread_id": "thread-0001"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


async def test_stream_metrics_appear_on_the_metrics_endpoint(settings: Settings) -> None:
    app = chat_app(settings, answering_model("Raise the pool size [1]."), _searcher())
    await post_stream(app, "/chat", {"message": "pool?"})

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        metrics = (await http.get("/metrics")).text

    assert 'chat_streams_total{outcome="completed"} 1.0' in metrics
    assert 'chat_stream_events_total{type="response_end"} 1.0' in metrics
    assert "chat_first_token_seconds_count 1.0" in metrics
    assert "chat_streams_active 0.0" in metrics


# ---------------------------------------------------------------
# Startup: the agents are opened once, for the app's lifetime
# ---------------------------------------------------------------


async def test_startup_without_a_model_key_keeps_the_api_running(settings: Settings) -> None:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert app.state.agents is None


async def test_startup_opens_the_agents_with_the_apps_metrics(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[object] = []
    service = agent_service(answering_model(), FakeSearcher())

    @asynccontextmanager
    async def fake_open(
        settings: Settings, *, metrics: object, retrieval_metrics: object
    ) -> AsyncIterator[AgentService]:
        opened.append(metrics)
        yield service
        opened.append("closed")

    monkeypatch.setattr(api.main, "open_agent_service", fake_open)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert app.state.agents is service
    assert opened[-1] == "closed"
