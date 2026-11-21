"""Approvals and feedback through the API."""

from typing import Any

import httpx
from fastapi import FastAPI
from langgraph.store.memory import InMemoryStore

from agents.checkpoint import memory_checkpointer
from agents.memory import LongTermMemory
from api.main import create_app
from core.config import Settings
from security.roles import Role
from tests.helpers.agents import FakeSearcher, agent_service, decision, hit, result_with
from tests.helpers.llm import ScriptedChatModel
from tests.helpers.sse import post_stream
from tests.helpers.tokens import TOKENS, TestUser, bearer, bearer_for

QUESTION = "Summarize payment-failure outages over the last year"


class FakeTracing:
    enabled = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, float, str | None]] = []

    def send_feedback(self, run_id: str, *, score: float, comment: str | None) -> bool:
        self.sent.append((run_id, score, comment))
        return True

    def callbacks(self, *, tags: Any) -> list[Any]:
        return []

    def flush(self) -> None:
        return None


def _app(settings: Settings) -> FastAPI:
    store = InMemoryStore()
    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("research")],
            "BatchFindings": [{"findings": []}] * 4,
        },
        texts=["Two outages [1]."],
    )
    app = create_app(settings, signing_keys=TOKENS.signing_keys)
    app.state.agents = agent_service(
        model,
        FakeSearcher([result_with(hit("INC-1"))]),
        checkpointer=memory_checkpointer(),
        memory=LongTermMemory(store),
        approval_for_research=True,
    )
    app.state.agents.tracing = FakeTracing()
    app.state.threads.index = store
    return app


async def _post(app: FastAPI, path: str, body: Any, headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return await http.post(path, json=body, headers=headers)


async def test_a_research_question_waits_and_the_approval_streams_the_answer(
    settings: Settings,
) -> None:
    app = _app(settings)
    asked = await post_stream(app, "/chat", {"message": QUESTION}, role=Role.ANALYST)
    assert asked.types[-1] == "approval_required"
    thread_id = asked.events[-1]["approval_id"]

    resumed = await post_stream(
        app, f"/approvals/{thread_id}", {"approve": True}, role=Role.ANALYST
    )

    assert resumed.status == 200
    assert resumed.headers["x-thread-id"] == thread_id
    assert resumed.types[-1] == "response_end"
    assert "research" in resumed.types
    again = await post_stream(app, f"/approvals/{thread_id}", {"approve": True}, role=Role.ANALYST)
    assert again.status == 409  # nothing waits any more


async def test_only_the_owner_can_answer_the_approval(settings: Settings) -> None:
    app = _app(settings)
    asked = await post_stream(app, "/chat", {"message": QUESTION}, role=Role.ANALYST)
    thread_id = asked.events[-1]["approval_id"]

    other = bearer_for(TestUser("other.analyst", Role.ANALYST))
    response = await _post(app, f"/approvals/{thread_id}", {"approve": True}, other)

    assert response.status_code == 404


async def test_feedback_is_saved_sent_to_langsmith_and_listed_for_admins(
    settings: Settings,
) -> None:
    app = _app(settings)
    asked = await post_stream(app, "/chat", {"message": QUESTION}, role=Role.ANALYST)
    thread_id = asked.headers["x-thread-id"]
    run_id = asked.headers["x-run-id"]

    response = await _post(
        app,
        "/feedback",
        {
            "run_id": run_id,
            "thread_id": thread_id,
            "score": "down",
            "comment": "Missed INC-2; mail jane@example.com",
        },
        bearer(Role.ANALYST),
    )

    assert response.status_code == 201
    assert response.json() == {"saved": True, "sent_to_langsmith": True}
    ((sent_run, score, comment),) = app.state.agents.tracing.sent
    assert (sent_run, score) == (run_id, 0.0)
    assert comment == "Missed INC-2; mail [EMAIL]"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        listed = (await http.get("/admin/feedback", headers=bearer(Role.ADMIN))).json()
    assert [(f["run_id"], f["score"], f["role"]) for f in listed] == [(run_id, "down", "analyst")]
    assert listed[0]["user"].startswith("u_")
    counter = app.state.agents.metrics.feedback.labels(score="down")
    assert counter._value.get() == 1


async def test_feedback_on_someone_elses_thread_is_refused(settings: Settings) -> None:
    app = _app(settings)
    asked = await post_stream(app, "/chat", {"message": QUESTION}, role=Role.ANALYST)
    body = {
        "run_id": asked.headers["x-run-id"],
        "thread_id": asked.headers["x-thread-id"],
        "score": "up",
    }
    other = bearer_for(TestUser("other.viewer", Role.VIEWER))
    assert (await _post(app, "/feedback", body, other)).status_code == 404


async def test_bad_feedback_is_refused(settings: Settings) -> None:
    app = _app(settings)
    bad = {"run_id": "not-a-uuid", "thread_id": "thread-0001", "score": "meh"}
    assert (await _post(app, "/feedback", bad, bearer(Role.VIEWER))).status_code == 422
