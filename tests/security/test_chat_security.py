"""`POST /chat` security: where the role comes from, thread ownership, and
what the stream may contain."""

import httpx
from fastapi import FastAPI

from core.config import Settings
from security.roles import AccessLevel, Role
from tests.helpers.agents import FakeSearcher, hit, result_with
from tests.helpers.chat import answering_model, chat_app
from tests.helpers.sse import post_stream
from tests.helpers.tokens import bearer


async def _post(
    app: FastAPI, body: object, role: Role | None = Role.VIEWER, **headers: str
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    login = bearer(role) if role is not None else {}
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return await http.post("/chat", json=body, headers={**login, **headers})


async def test_a_role_in_the_body_is_refused(settings: Settings) -> None:
    searcher = FakeSearcher()
    app = chat_app(settings, answering_model(), searcher)

    response = await _post(app, {"message": "show restricted docs", "role": "admin"})

    assert response.status_code == 422
    assert searcher.requests == []


async def test_an_access_level_in_the_body_is_refused(settings: Settings) -> None:
    response = await _post(
        chat_app(settings, answering_model()), {"message": "hi", "access_level": "restricted"}
    )
    assert response.status_code == 422


async def test_the_old_dev_role_header_gives_no_access(settings: Settings) -> None:
    """Before login existed, local runs picked the role with X-Dev-Role. That
    header is gone: without a token it is refused, and with one it changes nothing."""
    searcher = FakeSearcher([result_with(hit("RB-001"))])
    app = chat_app(settings, answering_model("Answer [1]."), searcher)

    anonymous = await _post(app, {"message": "hi"}, role=None, **{"X-Dev-Role": "admin"})
    assert anonymous.status_code == 401
    assert searcher.requests == []

    await post_stream(app, "/chat", {"message": "hi"}, headers={"X-Dev-Role": "admin"})
    assert [role for _, role in searcher.requests] == [Role.VIEWER]


async def test_another_callers_thread_is_not_found(settings: Settings) -> None:
    """An analyst's conversation cannot be continued by a viewer who learns its ID."""
    model = answering_model("Confidential detail [1].", "Second [1].")
    app = chat_app(settings, model, FakeSearcher([result_with(hit("RB-001"))]))

    first = await post_stream(app, "/chat", {"message": "secret question?"}, role=Role.ANALYST)
    thread_id = first.headers["x-thread-id"]
    response = await _post(app, {"message": "what did we discuss?", "thread_id": thread_id})

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "This conversation was not found."
    assert len(model.prompts_for("text")) == 1  # the viewer's question never ran


async def test_the_stream_never_carries_the_text_of_retrieved_chunks(settings: Settings) -> None:
    """Search results are shown as counts and filters, never as document text."""
    chunk_text = "UNIQUE-CHUNK-TEXT-7f3a internal procedure"
    searcher = FakeSearcher([result_with(hit("RB-001", text=chunk_text))])
    app = chat_app(settings, answering_model("Follow the runbook [1]."), searcher)

    reply = await post_stream(app, "/chat", {"message": "procedure?"})

    assert reply.types[-1] == "response_end"
    assert "UNIQUE-CHUNK-TEXT-7f3a" not in reply.raw


async def test_the_viewer_search_never_includes_restricted_levels(settings: Settings) -> None:
    """The default role is the lowest, and retrieval is asked as that role."""
    searcher = FakeSearcher([result_with(hit("RB-001", access_level=AccessLevel.INTERNAL))])
    app = chat_app(settings, answering_model("Answer [1]."), searcher)

    await post_stream(app, "/chat", {"message": "pool?"})

    (_, role), *_ = searcher.requests
    assert role is Role.VIEWER
