"""Saved conversations and memory through the API: /threads, /me/memory, clean-up."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI
from langgraph.store.memory import InMemoryStore

from agents.checkpoint import memory_checkpointer
from agents.memory import LongTermMemory
from api.main import create_app
from api.threads import NAMESPACE, ThreadRegistry
from core.config import Settings
from security.roles import Role
from tests.helpers.agents import FakeSearcher, agent_service, hit, result_with
from tests.helpers.chat import answering_model
from tests.helpers.sse import post_stream
from tests.helpers.tokens import TOKENS, TestUser, bearer, bearer_for


def _app(settings: Settings, answers: int = 3) -> FastAPI:
    """An app wired like Docker Compose, with in-memory stores in place of Postgres."""
    store = InMemoryStore()
    app = create_app(settings, signing_keys=TOKENS.signing_keys)
    app.state.agents = agent_service(
        answering_model(*["Raise the pool size [1]."] * answers),
        FakeSearcher([result_with(hit("RB-001"))]),
        checkpointer=memory_checkpointer(),
        memory=LongTermMemory(store),
    )
    app.state.threads.index = store
    return app


async def _call(
    app: FastAPI, method: str, path: str, headers: dict[str, str] | None = None
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return await http.request(method, path, headers=headers or bearer(Role.VIEWER))


async def _ask(app: FastAPI, message: str, **body: Any) -> str:
    reply = await post_stream(app, "/chat", {"message": message, **body})
    assert reply.status == 200
    return reply.headers["x-thread-id"]


async def test_the_callers_threads_are_listed_with_titles(settings: Settings) -> None:
    app = _app(settings)
    first = await _ask(app, "Why is the PayHub pool exhausted?")
    second = await _ask(app, "What are the cut-off times?")

    threads = (await _call(app, "GET", "/threads")).json()

    assert [t["thread_id"] for t in threads] == [second, first]  # most recent first
    assert threads[1]["title"] == "Why is the PayHub pool exhausted?"


async def test_a_thread_shows_its_questions_and_answers(settings: Settings) -> None:
    app = _app(settings)
    thread_id = await _ask(app, "Why is the pool exhausted?")
    await _ask(app, "And then?", thread_id=thread_id)

    detail = (await _call(app, "GET", f"/threads/{thread_id}")).json()

    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "user", "assistant"]
    assert detail["messages"][0]["text"] == "Why is the pool exhausted?"
    assert detail["messages"][1]["text"] == "Raise the pool size [1]."


async def test_other_users_cannot_see_list_or_delete_a_thread(settings: Settings) -> None:
    app = _app(settings)
    thread_id = await _ask(app, "My private question")
    other = bearer_for(TestUser("someone.else", Role.ADMIN))

    assert (await _call(app, "GET", f"/threads/{thread_id}", other)).status_code == 404
    assert (await _call(app, "DELETE", f"/threads/{thread_id}", other)).status_code == 404
    assert (await _call(app, "GET", "/threads", other)).json() == []


async def test_deleting_a_thread_removes_its_saved_steps(settings: Settings) -> None:
    app = _app(settings)
    thread_id = await _ask(app, "pool?")

    assert (await _call(app, "DELETE", f"/threads/{thread_id}")).status_code == 204

    assert (await _call(app, "GET", f"/threads/{thread_id}")).status_code == 404
    assert await app.state.agents.history(thread_id) == []


async def test_thread_owners_survive_a_restart(settings: Settings) -> None:
    """A new registry on the same store (as after a restart) still knows the owner."""
    app = _app(settings)
    thread_id = await _ask(app, "my question")
    app.state.threads = ThreadRegistry(busy_for_at_most=10, index=app.state.threads.index)

    other = bearer_for(TestUser("thief", Role.VIEWER))
    stolen = await post_stream(
        app, "/chat", {"message": "and?", "thread_id": thread_id}, role=None, headers=other
    )

    assert stolen.status == 404
    assert (await _ask(app, "and then?", thread_id=thread_id)) == thread_id


async def test_my_memory_can_be_read_and_cleared(settings: Settings) -> None:
    app = _app(settings)
    await _ask(app, "Why is the PayHub pool exhausted?")

    memory = (await _call(app, "GET", "/me/memory")).json()
    assert [item["text"] for item in memory["interactions"]] == [
        "Why is the PayHub pool exhausted?"
    ]
    assert memory["interactions"][0]["documents"] == ["RB-001"]

    assert (await _call(app, "DELETE", "/me/memory")).json() == {"deleted": 1}
    assert (await _call(app, "GET", "/me/memory")).json()["interactions"] == []


async def test_the_admin_clean_up_removes_old_threads_and_memory(settings: Settings) -> None:
    app = _app(settings)
    recent = await _ask(app, "recent question")
    old = await _ask(app, "old question")
    store = app.state.threads.index
    item = await store.aget(NAMESPACE, old)
    stale = (datetime.now(UTC) - timedelta(days=settings.memory_retention_days + 1)).isoformat()
    await store.aput(NAMESPACE, old, {**item.value, "updated": stale})

    response = await _call(app, "POST", "/admin/memory/cleanup", bearer(Role.ADMIN))

    assert response.json() == {
        "days": settings.memory_retention_days,
        "threads_deleted": 1,
        "memories_deleted": 0,
    }
    listed = [t["thread_id"] for t in (await _call(app, "GET", "/threads")).json()]
    assert listed == [recent]
    assert await app.state.agents.history(old) == []


async def test_without_agents_threads_and_memory_say_unavailable(settings: Settings) -> None:
    app = create_app(settings, signing_keys=TOKENS.signing_keys)
    assert (await _call(app, "GET", "/threads")).json() == []
    assert (await _call(app, "GET", "/me/memory")).status_code == 503
