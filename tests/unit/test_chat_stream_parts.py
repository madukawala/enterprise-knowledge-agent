"""The small parts of the chat stream: SSE encoding, the token hold-back and
the thread registry."""

import json

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from agents.events import TokenEvent
from agents.streaming import RunRecorder
from api.sse import encode
from api.threads import ThreadRegistry
from core.errors import ConflictError, NotFoundError

RESPONSE = {"langgraph_node": "response"}


def _recorder() -> RunRecorder:
    recorder = RunRecorder(run_id="run-1", thread_id="thread-1")
    recorder.translate("tasks", {"id": "t1", "name": "response", "input": {}})
    return recorder


def _tokens(recorder: RunRecorder, *parts: str, call: str = "call-1") -> str:
    """The streamed text, including the end that is held back until the draft is done."""
    text = ""
    for part in parts:
        for event in recorder.translate("messages", (AIMessageChunk(part, id=call), RESPONSE)):
            assert isinstance(event, TokenEvent)
            text += event.text
    finished = recorder.translate("tasks", {"id": "t1", "name": "response", "result": {}})
    return text + "".join(e.text for e in finished if isinstance(e, TokenEvent))


def test_an_event_is_one_sse_block_with_one_line_of_json() -> None:
    block = encode(TokenEvent(text="line one\nline two ✓"), seq=3, request_id="req-1")

    lines = block.split("\n")
    assert lines[:2] == ["id: 3", "event: token"]
    assert lines[2].startswith("data: ")
    assert block.endswith("\n\n")
    assert json.loads(lines[2][6:]) == {
        "seq": 3,
        "request_id": "req-1",
        "type": "token",
        "text": "line one\nline two ✓",
    }


def test_an_answer_starting_like_not_found_is_released_once_it_differs() -> None:
    assert _tokens(_recorder(), "No", " incidents", " were found [1].") == (
        "No incidents were found [1]."
    )


def test_not_found_with_more_text_after_it_is_shown() -> None:
    assert _tokens(_recorder(), "NOT_", "FOUND", " in runbooks") == "NOT_FOUND in runbooks"


def test_only_the_response_agents_tokens_are_passed_on() -> None:
    recorder = _recorder()
    planner = recorder.translate(
        "messages", (AIMessageChunk("{...}", id="c"), {"langgraph_node": "supervisor"})
    )
    whole = recorder.translate("messages", (AIMessage("done", id="m"), RESPONSE))
    assert planner == whole == []


def test_tasks_of_unknown_nodes_and_unknown_modes_are_ignored() -> None:
    recorder = _recorder()
    assert recorder.translate("tasks", {"id": "x", "name": "__start__", "input": {}}) == []
    assert recorder.translate("custom", {"anything": 1}) == []


def test_a_thread_belongs_to_whoever_started_it() -> None:
    threads = ThreadRegistry(busy_for_at_most=60)
    threads.start("thread-1", owner="role:analyst")
    threads.finish("thread-1")

    with pytest.raises(NotFoundError):
        threads.start("thread-1", owner="role:viewer")
    threads.start("thread-1", owner="role:analyst")


def test_a_busy_thread_is_refused_until_it_finishes() -> None:
    threads = ThreadRegistry(busy_for_at_most=60)
    threads.start("thread-1", owner="me")
    with pytest.raises(ConflictError):
        threads.start("thread-1", owner="me")
    threads.finish("thread-1")
    threads.start("thread-1", owner="me")


def test_a_thread_left_busy_by_a_stream_that_never_started_frees_itself() -> None:
    threads = ThreadRegistry(busy_for_at_most=0)
    threads.start("thread-1", owner="me")
    threads.start("thread-1", owner="me")  # no finish() in between
