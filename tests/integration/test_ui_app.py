"""The Streamlit page, run headless with Streamlit's AppTest.

The API is played back from real recorded replies (tests/helpers/ui.py),
so the page is tested against exactly what the API sends.
"""

import json
from collections.abc import Iterator

import httpx
import pytest
import respx
import streamlit as st
from streamlit.testing.v1 import AppTest

from core.config import Settings
from tests.helpers.agents import FakeSearcher, hit, result_with
from tests.helpers.chat import answering_model, chat_app
from tests.helpers.ui import API_URL, APP_FILE, record, replay
from ui.conversation import Turn


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    monkeypatch.delenv("API_URL", raising=False)
    st.cache_data.clear()  # the API status check is cached between runs
    with respx.mock(base_url=API_URL, assert_all_called=False) as router:
        router.get("/healthz").respond(200, json={"status": "ok"})
        yield router


def _replies(settings: Settings, *answers: str) -> list[httpx.Response]:
    """Real API replies to one conversation, one answer per question."""
    app = chat_app(settings, answering_model(*answers), FakeSearcher([result_with(hit("RB-001"))]))
    first = record(app, "first question")
    thread_id = first.headers["x-thread-id"]
    later = [record(app, f"question {n}", thread_id=thread_id) for n in range(2, len(answers) + 1)]
    return [first, *later]


def _page() -> AppTest:
    return AppTest.from_file(str(APP_FILE), default_timeout=30).run()


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
    assert page.selectbox(key="role").value == "viewer"


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
    assert chat.calls[0].request.headers["x-dev-role"] == "viewer"


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
    app.state.threads.start("thread-0001", owner="role:viewer")
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


def test_changing_the_role_starts_a_new_conversation_as_that_role(
    api: respx.MockRouter, settings: Settings
) -> None:
    first, _ = _replies(settings, "A [1].", "B [1].")
    analyst = record(
        chat_app(settings, answering_model("For analysts [1]."), FakeSearcher()),
        "pool?",
        headers={"X-Dev-Role": "analyst"},
    )
    chat = api.post("/chat").mock(side_effect=[first, analyst])
    page = _ask(_page(), "As a viewer")
    viewer_conversation = page.session_state.current

    page = page.selectbox(key="role").set_value("analyst").run()
    page = _ask(page, "As an analyst")

    # A new conversation: no thread ID, and the new role.
    assert _bodies(chat)[1] == {"message": "As an analyst"}
    assert chat.calls[1].request.headers["x-dev-role"] == "analyst"
    assert page.session_state.current != viewer_conversation
    assert len(page.session_state.conversations) == 2

    # Opening the first conversation again brings back its role and history.
    page = page.button(key=f"open-{viewer_conversation}").click().run()
    assert page.selectbox(key="role").value == "viewer"
    assert "As a viewer" in _texts(page)
    assert "As an analyst" not in _texts(page)


def test_new_conversation_starts_empty(api: respx.MockRouter, settings: Settings) -> None:
    api.post("/chat").mock(side_effect=_replies(settings, "A [1]."))
    page = _ask(_page(), "pool?")

    new = page.sidebar.button[0]
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
        {"type": "memory", "saved": "department = payments"},
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
    assert "`memory`" in text
    assert "Limits: The runbooks did not answer." in text
    assert [w.value for w in page.warning] == ["No answer from: runbooks", "Backup model used."]
    assert [i.value for i in page.info] == [
        "The validator rejected the draft, so the answer was written again."
    ]
    assert "Not found in the documents you can read" in text
    assert "No model calls" in text
