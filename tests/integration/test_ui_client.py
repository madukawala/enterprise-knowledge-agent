"""The UI's API client, fed with what the real API sends.

Each reply here is recorded from the real FastAPI app with scripted agents
(tests/helpers/ui.py), so these tests check that the UI and the API agree
on the stream format, the headers and the error shape.
"""

import json
from collections.abc import Iterator
from contextlib import closing

import httpx
import pytest

from core.config import Settings
from tests.helpers.agents import FakeSearcher, hit, result_with
from tests.helpers.chat import answering_model, chat_app
from tests.helpers.ui import Playback, record
from ui.client import ApiError, ChatClient, StreamOpened
from ui.config import UiSettings
from ui.conversation import Turn, TurnStatus
from ui.sse import KeepAlive


def _searcher() -> FakeSearcher:
    return FakeSearcher([result_with(hit("RB-001"), hit("RB-002"))])


def _client(transport: httpx.BaseTransport) -> ChatClient:
    return ChatClient(UiSettings(), transport=transport)


def _turn_from(client: ChatClient, question: str, **options: str) -> tuple[Turn, StreamOpened]:
    turn = Turn(question=question)
    opened: StreamOpened | None = None
    with closing(client.ask(question, role=options.pop("role", "viewer"), **options)) as stream:
        for item in stream:
            if isinstance(item, StreamOpened):
                opened = item
            elif not isinstance(item, KeepAlive):
                turn.apply(item)
    turn.end_of_stream()
    assert opened is not None
    return turn, opened


# ---------------------------------------------------------------
# A real stream, read by the UI
# ---------------------------------------------------------------


def test_the_ui_builds_the_same_answer_the_api_sent(settings: Settings) -> None:
    app = chat_app(settings, answering_model("Raise the pool size [1]."), _searcher())
    reply = record(app, "Why is the pool exhausted?")
    api_final = json.loads(reply.text.strip().split("\n")[-1].removeprefix("data: "))

    turn, opened = _turn_from(_client(Playback(reply)), "Why is the pool exhausted?")

    assert opened.thread_id == reply.headers["x-thread-id"]
    assert opened.run_id == reply.headers["x-run-id"] == turn.run_id
    assert opened.request_id == reply.headers["x-request-id"]
    assert turn.status is TurnStatus.DONE
    assert turn.final == api_final
    assert turn.text == "Raise the pool size [1]."
    # The streamed tokens spelled out the same answer before it was final.
    assert turn.preview == "Raise the pool size [1]."
    assert [step.node for step in turn.steps] == [
        "input_guard",
        "supervisor",
        "retrieval",
        "supervisor",
        "response",
        "validator",
    ]
    assert all(step.status == "ok" for step in turn.steps)
    assert [check["stage"] for check in turn.checks] == ["input", "output"]
    assert turn.searches[0]["collections"] == ["runbooks"]


def test_a_rewrite_shows_up_as_a_restart_and_the_final_text_wins(settings: Settings) -> None:
    model = answering_model("The limit is 200 [9].", "The limit is 200 [1].")
    app = chat_app(settings, model, _searcher())

    turn, _ = _turn_from(_client(Playback(record(app, "limit?"))), "limit?")

    assert turn.restarts == ["rewrite"]
    assert turn.text == "The limit is 200 [1]."
    assert [check["passed"] for check in turn.checks if check["stage"] == "output"] == [
        False,
        True,
    ]


def test_the_request_carries_the_question_thread_and_role_and_nothing_else(
    settings: Settings,
) -> None:
    app = chat_app(settings, answering_model("A [1].", "B [1]."), _searcher())
    first = record(app, "pool?", headers={"X-Dev-Role": "analyst"})
    thread_id = first.headers["x-thread-id"]
    second = record(app, "and then?", thread_id=thread_id, headers={"X-Dev-Role": "analyst"})
    playback = Playback(first, second)
    client = _client(playback)

    _turn_from(client, "pool?", role="analyst")
    turn, opened = _turn_from(client, "and then?", role="analyst", thread_id=thread_id)

    assert opened.thread_id == thread_id
    assert turn.text == "B [1]."
    bodies = [json.loads(request.content) for request in playback.requests]
    assert bodies == [{"message": "pool?"}, {"message": "and then?", "thread_id": thread_id}]
    assert {request.headers["x-dev-role"] for request in playback.requests} == {"analyst"}
    assert {request.url.path for request in playback.requests} == {"/chat"}


# ---------------------------------------------------------------
# The API refuses the question
# ---------------------------------------------------------------


def _refusal(reply: httpx.Response) -> ApiError:
    client = _client(Playback(reply))
    with pytest.raises(ApiError) as caught, closing(client.ask("pool?", role="viewer")) as stream:
        list(stream)
    return caught.value


def test_a_busy_conversation_gives_the_apis_own_message(settings: Settings) -> None:
    app = chat_app(settings, answering_model("A [1]."), _searcher())
    app.state.threads.start("thread-0001", owner="role:viewer")

    error = _refusal(record(app, "pool?", thread_id="thread-0001"))

    assert (error.status, error.code) == (409, "conflict")
    assert error.message
    assert error.request_id


def test_someone_elses_conversation_is_not_found(settings: Settings) -> None:
    app = chat_app(settings, answering_model("A [1]."), _searcher())
    thread_id = record(app, "pool?").headers["x-thread-id"]

    error = _refusal(record(app, "pool?", thread_id=thread_id, headers={"X-Dev-Role": "admin"}))

    assert (error.status, error.code) == (404, "not_found")


def test_a_request_the_api_rejects_explains_why(settings: Settings) -> None:
    app = chat_app(settings, answering_model("A [1]."), _searcher())
    error = _refusal(record(app, "pool?", thread_id="bad"))
    assert error.status == 422
    assert error.message


def test_agents_that_are_not_running_give_503(settings: Settings) -> None:
    from api.main import create_app

    error = _refusal(record(create_app(settings), "pool?"))
    assert (error.status, error.message) == (
        503,
        "The assistant is not available right now. Please try again later.",
    )


def test_an_error_page_from_a_proxy_gets_a_plain_message() -> None:
    reply = httpx.Response(502, text="<html>Bad gateway</html>")
    error = _refusal(reply)
    assert error.status == 502
    assert error.message == "The assistant could not take the question (HTTP 502)."
    assert "<html>" not in error.message


def test_a_known_status_without_a_body_still_gets_a_helpful_message() -> None:
    error = _refusal(httpx.Response(409, json={"detail": "busy"}))
    assert error.message.startswith("This conversation is still answering")


# ---------------------------------------------------------------
# The connection fails
# ---------------------------------------------------------------


class _BreaksHalfWay(httpx.SyncByteStream):
    """A stream that sends some events, then fails with `error`."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        yield b'event: token\ndata: {"type": "token", "text": "Half"}\n\n'
        yield b": keep-alive\n\n"
        raise self.error

    def close(self) -> None:
        self.closed = True


def _streaming(body: httpx.SyncByteStream) -> httpx.MockTransport:
    headers = {"content-type": "text/event-stream", "x-thread-id": "thread-0001"}
    return httpx.MockTransport(lambda _: httpx.Response(200, headers=headers, stream=body))


def test_an_api_that_is_down_is_reported_without_internal_details() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 61] Connection refused at 10.0.0.5", request=request)

    with pytest.raises(ApiError) as caught:
        list(_client(httpx.MockTransport(refuse)).ask("pool?", role="viewer"))
    assert caught.value.message == (
        "Cannot reach the assistant at http://localhost:8000. Is the API running?"
    )
    assert caught.value.status is None


def test_a_connection_lost_half_way_keeps_what_arrived() -> None:
    body = _BreaksHalfWay(httpx.ReadError("reset"))
    items: list[object] = []
    with pytest.raises(ApiError, match="connection to the assistant was lost"):
        items.extend(_client(_streaming(body)).ask("pool?", role="viewer"))
    assert isinstance(items[0], StreamOpened)
    assert items[1] == {"type": "token", "text": "Half"}
    assert isinstance(items[2], KeepAlive)
    assert body.closed


def test_a_silent_api_times_out_with_a_clear_message() -> None:
    body = _BreaksHalfWay(httpx.ReadTimeout("no data"))
    with pytest.raises(ApiError, match="took too long"):
        list(_client(_streaming(body)).ask("pool?", role="viewer"))

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    with pytest.raises(ApiError, match="did not answer in time"):
        list(_client(httpx.MockTransport(slow)).ask("pool?", role="viewer"))


def test_an_event_the_ui_cannot_read_is_an_error_not_a_crash() -> None:
    class Garbled(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"event: token\ndata: not json\n\n"

    with pytest.raises(ApiError, match="could not read"):
        list(_client(_streaming(Garbled())).ask("pool?", role="viewer"))


def test_stopping_early_closes_the_connection() -> None:
    body = _BreaksHalfWay(AssertionError("read past the stop"))
    with closing(_client(_streaming(body)).ask("pool?", role="viewer")) as stream:
        next(stream)  # StreamOpened
        assert next(stream) == {"type": "token", "text": "Half"}
    assert body.closed


def test_the_liveness_check() -> None:
    up = httpx.MockTransport(lambda _: httpx.Response(200, json={"status": "ok"}))
    assert _client(up).is_up()

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert not _client(httpx.MockTransport(down)).is_up()
