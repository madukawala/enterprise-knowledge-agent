"""The Streamlit page, run headless with Streamlit's AppTest.

The API is played back from real recorded replies (tests/helpers/ui.py),
so the page is tested against exactly what the API sends.
"""

import asyncio
import json
from collections.abc import Iterator
from dataclasses import replace
from functools import cache
from typing import Any

import httpx
import pytest
import respx
import streamlit as st
from streamlit.testing.v1 import AppTest

from api.main import create_app
from core.config import Environment, Settings
from security.roles import Role
from tests.helpers.agents import FakeSearcher, hit, result_with
from tests.helpers.chat import answering_model, chat_app
from tests.helpers.sse import post_stream
from tests.helpers.tokens import TEST_USERS, TOKENS
from tests.helpers.ui import (
    API_URL,
    page_for,
    record,
    record_me,
    replay,
)
from ui.auth import BAD_CREDENTIALS, SESSION_ENDED
from ui.conversation import Turn


@cache
def _me_reply() -> httpx.Response:
    settings = Settings(_env_file=None, app_env=Environment.TEST)
    return record_me(create_app(settings, signing_keys=TOKENS.signing_keys), Role.VIEWER)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    for name in ("API_URL", "KEYCLOAK_URL", "KEYCLOAK_REALM", "KEYCLOAK_CLIENT_ID"):
        monkeypatch.delenv(name, raising=False)
    st.cache_data.clear()  # the API status check is cached between runs
    with respx.mock(base_url=API_URL, assert_all_called=False) as router:
        router.get("/healthz").respond(200, json={"status": "ok"})
        # Read again after each answer, to show today's token usage.
        router.get("/me").mock(return_value=_me_reply())
        # Saved conversations, read on sign-in and after each answer.
        router.get("/threads").respond(200, json=[])
        yield router


def _replies(settings: Settings, *answers: str) -> list[httpx.Response]:
    """Real API replies to one conversation, one answer per question."""
    app = chat_app(settings, answering_model(*answers), FakeSearcher([result_with(hit("RB-001"))]))
    first = record(app, "first question")
    thread_id = first.headers["x-thread-id"]
    later = [record(app, f"question {n}", thread_id=thread_id) for n in range(2, len(answers) + 1)]
    return [first, *later]


KEYCLOAK = "http://localhost:8080/realms/helix/protocol/openid-connect"


def _page(role: Role | None = Role.VIEWER) -> AppTest:
    return page_for(role)


def _ask(page: AppTest, question: str) -> AppTest:
    return page.chat_input[0].set_value(question).run()


def _texts(page: AppTest) -> str:
    return "\n".join([m.value for m in page.markdown] + [c.value for c in page.caption])


def _bodies(route: respx.Route) -> list[dict[str, str]]:
    return [json.loads(call.request.content) for call in route.calls]


def test_the_first_page_welcomes_and_offers_example_questions(api: respx.MockRouter) -> None:
    page = _page()

    assert not page.exception
    assert page.title[0].value == "Helix Knowledge Assistant"
    labels = [button.label for button in page.button]
    assert "The PayHub database connection pool is exhausted. What should I do?" in labels
    assert "API connected: http://localhost:8000" in _texts(page)
    assert "Role: **viewer**" in _texts(page)


def test_an_api_that_is_down_is_shown_in_the_sidebar(api: respx.MockRouter) -> None:
    api.get("/healthz").mock(side_effect=httpx.ConnectError("refused"))
    assert "API not reachable: http://localhost:8000" in _texts(_page())


def test_an_answer_streams_into_the_chat_and_the_panel(
    api: respx.MockRouter, settings: Settings
) -> None:
    chat = api.post("/chat").mock(side_effect=_replies(settings, "Raise the pool size [1]."))

    page = _ask(_page(), "Why is the pool exhausted?")

    assert not page.exception
    text = _texts(page)
    # The chat window: the question, the answer and its sources.
    assert "Why is the pool exhausted?" in text
    assert "Raise the pool size [1]." in text
    assert "[1] **RB-001**" in text
    # The activity panel: plan, steps, search, checks and the response.
    assert "Intent: document question. Agents: Retrieval agent." in text
    for step in ("Input guard", "Supervisor", "Retrieval agent", "Response agent", "Validator"):
        assert step in text
    assert "Query: _pool exhausted_" in text
    assert "Input check: passed" in text
    assert "Citation check: passed" in text
    assert "written by gemini / gemini-test" in text
    assert "3 model calls" in text
    assert _bodies(chat) == [{"message": "Why is the pool exhausted?"}]
    token = page.session_state.session.access_token
    assert chat.calls[0].request.headers["authorization"] == f"Bearer {token}"


def test_a_follow_up_question_continues_the_same_thread(
    api: respx.MockRouter, settings: Settings
) -> None:
    replies = _replies(settings, "The limit is 200 [1].", "Restart the workers [1].")
    chat = api.post("/chat").mock(side_effect=[replay(reply) for reply in replies])

    page = _ask(_ask(_page(), "What is the limit?"), "And then?")

    thread_id = replies[0].headers["x-thread-id"]
    assert _bodies(chat) == [
        {"message": "What is the limit?"},
        {"message": "And then?", "thread_id": thread_id},
    ]
    text = _texts(page)
    assert "The limit is 200 [1]." in text
    assert "Restart the workers [1]." in text
    assert f"Thread `{thread_id}`" in text
    # The panel can show either question; the latest one by default.
    picker = page.selectbox(key=f"panel-{page.session_state.current}")
    assert picker.options == ["1. What is the limit?", "2. And then?"]
    assert picker.index == 1


def test_an_example_question_is_asked_when_clicked(
    api: respx.MockRouter, settings: Settings
) -> None:
    chat = api.post("/chat").mock(side_effect=_replies(settings, "Raise the pool size [1]."))
    page = _page()

    example = page.button(key="example-0")
    page = example.click().run()

    assert _bodies(chat) == [{"message": example.label}]
    assert "Raise the pool size [1]." in _texts(page)


def test_a_refused_question_shows_the_apis_message_and_a_reference(
    api: respx.MockRouter, settings: Settings
) -> None:
    app = chat_app(settings, answering_model("A [1]."), FakeSearcher())
    app.state.threads.start("thread-0001", owner=TEST_USERS[Role.VIEWER].user_id)
    busy = record(app, "pool?", thread_id="thread-0001")
    api.post("/chat").mock(return_value=busy)

    page = _ask(_page(), "pool?")

    assert not page.exception
    (error,) = page.error
    assert error.value.startswith("This conversation is still answering")
    assert json.loads(busy.content)["error"]["request_id"] in error.value


def test_an_unreachable_api_fails_the_question_with_a_clear_message(
    api: respx.MockRouter,
) -> None:
    api.post("/chat").mock(side_effect=httpx.ConnectError("refused"))
    page = _ask(_page(), "pool?")
    assert page.error[0].value.startswith("Cannot reach the assistant at http://localhost:8000")


def test_new_conversation_starts_empty(api: respx.MockRouter, settings: Settings) -> None:
    api.post("/chat").mock(side_effect=_replies(settings, "A [1]."))
    page = _ask(_page(), "pool?")

    new = page.button(key="new-conversation")
    assert new.label == "New conversation"
    page = new.click().run()

    assert len(page.session_state.conversations) == 2
    assert "pool?" not in [m.value for m in page.markdown]
    assert page.button(key="example-0")


def test_an_answer_cut_off_by_stop_is_marked_as_stopped(api: respx.MockRouter) -> None:
    page = _page()
    # What a Stop click leaves behind: a turn still marked as streaming.
    turn = Turn(question="pool?", preview="Raise the")
    turn.apply({"type": "node_start", "node": "response"})
    page.session_state.conversations[0].turns.append(turn)

    page = page.run()

    assert "Stopped before the answer was finished." in _texts(page)
    assert "Raise the" in _texts(page)


def test_the_panel_shows_failures_fallbacks_and_rewrites(api: respx.MockRouter) -> None:
    page = _page()
    turn = Turn(question="Why did card payments fail?")
    for event in (
        {"type": "node_start", "node": "retrieval"},
        {
            "type": "retrieval",
            "query": "card failures",
            "collections": ["incidents", "runbooks"],
            "filters": {"department": "payments"},
            "failed_collections": ["runbooks"],
            "candidates": 12,
            "hits": 4,
            "rerank": "failed",
            "took_ms": 8000,
            "relaxed": True,
        },
        {"type": "node_end", "node": "retrieval", "duration_ms": 8000, "status": "error"},
        {"type": "degraded", "dependency": "language_model", "message": "Backup model used."},
        {"type": "response_start", "attempt": 2, "reason": "rewrite"},
        {"type": "memory", "saved": ["Noted interest in: department = payments"]},
        {
            "type": "response_end",
            "kind": "not_found",
            "text": "I couldn't find this in the documents you can read.",
            "limits": ["The runbooks did not answer."],
            "duration_ms": 9100,
        },
    ):
        turn.apply(event)
    page.session_state.conversations[0].turns.append(turn)

    page = page.run()

    text = _texts(page)
    assert "failed, the run carried on without it" in text
    assert "runbooks, incidents" not in text  # order is the API's
    assert "incidents, runbooks: 12 matches, 4 kept (re-ranking failed, search order kept)" in text
    assert "Filters: department = payments" in text
    assert "Second try, with the optional filters dropped." in text
    assert "department = payments" in text  # the memory event, in its own section
    assert "Limits: The runbooks did not answer." in text
    assert [w.value for w in page.warning] == ["No answer from: runbooks", "Backup model used."]
    assert [i.value for i in page.info] == [
        "The validator rejected the draft, so the answer was written again."
    ]
    assert "Not found in the documents you can read" in text
    assert "No model calls" in text


# ---------------------------------------------------------------
# Signing in and out
# ---------------------------------------------------------------


def _sign_in(page: AppTest, username: str, password: str) -> AppTest:
    page.text_input[0].input(username)
    page.text_input[1].input(password)
    return page.button[0].click().run()


def test_signing_in_shows_the_users_name_role_and_tools(
    api: respx.MockRouter, settings: Settings
) -> None:
    token = TOKENS.token(Role.ANALYST)
    keycloak = api.post(f"{KEYCLOAK}/token").respond(
        200,
        json={
            "access_token": token,
            "expires_in": 300,
            "refresh_token": "refresh",
            "refresh_expires_in": 1800,
        },
    )
    me = api.get("/me").mock(
        return_value=record_me(chat_app(settings, answering_model()), Role.ANALYST)
    )
    page = _page(role=None)
    assert page.subheader[0].value == "Sign in"
    assert not page.chat_input

    page = _sign_in(page, "adam.analyst", "analyst-demo-pass")

    assert not page.exception
    form = dict(item.split("=") for item in keycloak.calls[0].request.content.decode().split("&"))
    assert form["grant_type"] == "password"
    assert form["client_id"] == "enterprise-knowledge-ui"
    assert form["username"] == "adam.analyst"
    assert me.calls[0].request.headers["authorization"] == f"Bearer {token}"
    text = _texts(page)
    assert "**Adam Analyst**" in text
    assert "Role: **analyst**" in text
    assert "Python analysis tool" in text
    assert "Admin: audit log" not in text
    assert "Documents you can read: public, internal, confidential" in text


def test_a_wrong_password_keeps_the_user_at_the_sign_in_form(api: respx.MockRouter) -> None:
    api.post(f"{KEYCLOAK}/token").respond(401, json={"error": "invalid_grant"})

    page = _sign_in(_page(role=None), "vera.viewer", "wrong")

    assert [e.value for e in page.error] == [BAD_CREDENTIALS]
    assert "profile" not in page.session_state
    assert not page.chat_input


def test_a_viewer_is_not_shown_the_tools_their_role_cannot_use(api: respx.MockRouter) -> None:
    text = _texts(_page(Role.VIEWER))

    assert ":material/check: Knowledge search" in text
    for hidden in ("Python analysis tool", "Deep research", "Admin:"):
        assert hidden not in text
    assert "Documents you can read: public, internal" in text


def test_an_admin_is_shown_the_admin_tools(api: respx.MockRouter) -> None:
    text = _texts(_page(Role.ADMIN))
    assert "Admin: audit log" in text
    assert "Documents you can read: public, internal, confidential, restricted" in text


def test_signing_out_ends_the_keycloak_session_and_forgets_the_conversations(
    api: respx.MockRouter, settings: Settings
) -> None:
    api.post("/chat").mock(side_effect=_replies(settings, "A [1]."))
    logout = api.post(f"{KEYCLOAK}/logout").respond(204)
    page = _ask(_page(), "pool?")

    page = page.button(key="sign-out").click().run()

    assert logout.called
    assert "refresh_token=test-refresh-token" in logout.calls[0].request.content.decode()
    assert page.subheader[0].value == "Sign in"
    for key in ("session", "profile", "conversations"):
        assert key not in page.session_state
    assert "pool?" not in _texts(page)


def test_a_token_the_api_no_longer_accepts_sends_the_user_back_to_sign_in(
    api: respx.MockRouter, settings: Settings
) -> None:
    expired = TOKENS.token(Role.VIEWER, expires_in=-120)
    refused = record(
        chat_app(settings, answering_model()),
        "pool?",
        role=None,
        headers={"Authorization": f"Bearer {expired}"},
    )
    assert refused.status_code == 401
    api.post("/chat").mock(return_value=refused)
    api.post(f"{KEYCLOAK}/logout").respond(204)

    page = _ask(_page(), "pool?")

    assert page.subheader[0].value == "Sign in"
    assert [i.value for i in page.info] == [SESSION_ENDED]
    assert "conversations" not in page.session_state


def test_a_session_that_cannot_be_refreshed_signs_the_user_out(api: respx.MockRouter) -> None:
    api.post(f"{KEYCLOAK}/token").respond(400, json={"error": "invalid_grant"})
    api.post(f"{KEYCLOAK}/logout").respond(204)
    chat = api.post("/chat")
    page = _page()
    session = page.session_state.session
    page.session_state.session = replace(session, expires_at=0)

    page = _ask(page, "pool?")

    assert not chat.called
    assert page.subheader[0].value == "Sign in"
    assert [i.value for i in page.info] == [SESSION_ENDED]


# ---------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------


def test_the_sidebar_shows_todays_token_usage(api: respx.MockRouter) -> None:
    page = _page()
    (bar,) = page.get("progress")
    assert "Today's usage: 0 of 100,000 tokens" in bar.proto.text


def test_a_question_over_the_limit_says_when_to_try_again(
    api: respx.MockRouter, settings: Settings
) -> None:
    app = chat_app(settings.model_copy(update={"rate_limit_burst": 1}), answering_model("A [1]."))
    record(app, "first")
    refused = record(app, "second")
    assert refused.status_code == 429
    api.post("/chat").mock(return_value=refused)

    page = _ask(_page(), "pool?")

    assert not page.exception
    (error,) = page.error
    assert "Please wait" in error.value
    assert "try again" in error.value


def test_the_panel_shows_the_research_plan_and_batches(api: respx.MockRouter) -> None:
    page = _page(Role.ANALYST)
    turn = Turn(question="Summarize outages")
    turn.apply(
        {
            "type": "research",
            "plan_source": "sandbox",
            "plan_code": "result = [search('outage', ['incidents'])]",
            "batches": [
                {"label": "2026 H1", "depth": 1, "hits": 8, "findings": 0, "split": True},
                {"label": "2026 Q1", "depth": 2, "hits": 3, "findings": 2},
                {"label": "2026 Q2", "depth": 2, "hits": 0, "findings": 0, "error": "timeout"},
            ],
            "max_depth": 2,
            "sources": 3,
            "tokens_used": 2400,
            "stopped_by_budget": True,
        }
    )
    page.session_state.conversations[0].turns.append(turn)

    page = page.run()

    text = _texts(page)
    assert "Plan written by the model and run in the sandbox. 3 batches, depth 2" in text
    assert "2,400 tokens. Stopped at the token budget." in text
    assert "2026 H1: 8 sources, split into smaller batches" in text
    assert "2026 Q1: 3 sources, 2 findings" in text
    assert "2026 Q2: failed (timeout)" in text
    assert page.get("code")[0].value == "result = [search('outage', ['incidents'])]"


# ---------------------------------------------------------------
# Saved conversations and memory
# ---------------------------------------------------------------


def test_an_earlier_conversation_opens_with_its_history(api: respx.MockRouter) -> None:
    api.get("/threads").respond(
        200,
        json=[
            {
                "thread_id": "saved-thread-1",
                "title": "Why is the pool exhausted?",
                "created": "2026-11-20T10:00:00Z",
                "updated": "2026-11-20T10:05:00Z",
            }
        ],
    )
    api.get("/threads/saved-thread-1").respond(
        200,
        json={
            "thread_id": "saved-thread-1",
            "title": "Why is the pool exhausted?",
            "created": "2026-11-20T10:00:00Z",
            "updated": "2026-11-20T10:05:00Z",
            "messages": [
                {"role": "user", "text": "Why is the pool exhausted?"},
                {"role": "assistant", "text": "A slow query held connections [1]."},
            ],
        },
    )
    page = _page()
    assert "Earlier conversations" in _texts(page)

    page = page.button(key="saved-saved-thread-1").click().run()

    assert not page.exception
    text = _texts(page)
    assert "Why is the pool exhausted?" in text
    assert "A slow query held connections [1]." in text
    assert page.session_state.conversations[-1].thread_id == "saved-thread-1"
    # Now open in the tab, so it is no longer listed as "earlier".
    assert "Earlier conversations" not in _texts(page)


def test_memory_can_be_shown_and_cleared(api: respx.MockRouter) -> None:
    api.get("/me/memory").respond(
        200,
        json={
            "departments": {"payments": 2},
            "interactions": [
                {
                    "text": "Why is the pool exhausted?",
                    "documents": [],
                    "at": "2026-11-20T10:00:00Z",
                }
            ],
        },
    )
    forget = api.delete("/me/memory").respond(200, json={"deleted": 1})
    page = _page()

    page = page.button(key="show-memory").click().run()
    assert "- Why is the pool exhausted?" in _texts(page)

    page = page.button(key="forget-memory").click().run()
    assert forget.called
    assert "Nothing yet." in _texts(page)


# ---------------------------------------------------------------
# Approval and feedback
# ---------------------------------------------------------------


def _research_app(settings: Settings) -> Any:
    from agents.checkpoint import memory_checkpointer
    from tests.helpers.agents import agent_service, decision
    from tests.helpers.llm import ScriptedChatModel

    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("research")],
            "BatchFindings": [{"findings": []}] * 4,
        },
        texts=["Two outages in the last year [1]."],
    )
    app = create_app(settings, signing_keys=TOKENS.signing_keys)
    app.state.agents = agent_service(
        model,
        FakeSearcher([result_with(hit("INC-1"))]),
        checkpointer=memory_checkpointer(),
        approval_for_research=True,
    )
    return app


def test_a_run_that_needs_approval_shows_the_question_and_continues_when_approved(
    api: respx.MockRouter, settings: Settings
) -> None:
    app = _research_app(settings)
    waiting = record(app, "Summarize outages over the last year", role=Role.ANALYST)
    thread_id = waiting.headers["x-thread-id"]
    resumed = asyncio.run(
        post_stream(app, f"/approvals/{thread_id}", {"approve": True}, role=Role.ANALYST)
    )
    api.post("/chat").mock(return_value=waiting)
    approval = api.post(f"/approvals/{thread_id}").respond(
        200, headers=resumed.headers, content=resumed.raw.encode()
    )
    page = _ask(_page(Role.ANALYST), "Summarize outages over the last year")

    assert "needs deep research" in next(i.value for i in page.info)
    page = page.button(key=f"approve-{thread_id}").click().run()

    assert not page.exception
    assert json.loads(approval.calls[0].request.content) == {"approve": True}
    assert "Two outages in the last year [1]." in _texts(page)


def test_thumbs_down_sends_feedback_for_that_answer(
    api: respx.MockRouter, settings: Settings
) -> None:
    replies = _replies(settings, "Raise the pool size [1].")
    api.post("/chat").mock(side_effect=replies)
    sent = api.post("/feedback").respond(201, json={"saved": True, "sent_to_langsmith": False})
    page = _ask(_page(), "pool?")
    run_id = replies[0].headers["x-run-id"]

    page = page.button(key=f"down-{run_id}").click().run()

    body = json.loads(sent.calls[0].request.content)
    assert (body["run_id"], body["score"]) == (run_id, "down")
    assert body["thread_id"] == replies[0].headers["x-thread-id"]
    assert "Thanks for the feedback." in _texts(page)
