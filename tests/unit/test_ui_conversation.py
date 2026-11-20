"""How the UI builds one answer and its activity from the streamed events."""

from typing import Any

from ui.conversation import Conversation, Turn, TurnStatus


def _turn(*events: dict[str, Any]) -> Turn:
    turn = Turn(question="Why is the pool exhausted?")
    for event in events:
        turn.apply(event)
    return turn


def _token(text: str) -> dict[str, Any]:
    return {"type": "token", "text": text}


def _end(text: str = "Raise the pool size [1].") -> dict[str, Any]:
    return {"type": "response_end", "kind": "answered", "text": text, "duration_ms": 900}


def test_tokens_build_the_preview_until_the_final_answer_replaces_it() -> None:
    turn = _turn({"type": "response_start", "attempt": 1}, _token("Raise "), _token("the pool"))
    assert (turn.text, turn.status, turn.tokens) == ("Raise the pool", TurnStatus.STREAMING, 2)

    turn.apply(_end("Raise the pool size to 200 [1]."))
    assert turn.status is TurnStatus.DONE
    assert turn.text == "Raise the pool size to 200 [1]."


def test_a_new_response_start_clears_the_preview_and_says_why() -> None:
    turn = _turn(
        {"type": "response_start", "attempt": 1, "reason": "first"},
        _token("The limit is 200 [9]."),
        {"type": "response_start", "attempt": 2, "reason": "rewrite"},
        _token("The limit"),
    )
    assert turn.preview == "The limit"
    assert turn.restarts == ["rewrite"]


def test_an_error_event_fails_the_turn_with_its_safe_message() -> None:
    turn = _turn(_token("Half an"), {"type": "error", "code": "timeout", "message": "Too slow."})
    assert (turn.status, turn.error) == (TurnStatus.FAILED, "Too slow.")
    assert turn.text == "Half an"  # kept, and shown as unfinished


def test_a_stream_that_ends_without_an_answer_was_cut_short() -> None:
    turn = _turn(_token("Half"))
    turn.end_of_stream()
    assert turn.status is TurnStatus.FAILED
    assert turn.error is not None
    assert "cut short" in turn.error

    finished = _turn(_end())
    finished.end_of_stream()
    assert (finished.status, finished.error) == (TurnStatus.DONE, None)


def test_stop_marks_the_turn_and_its_running_steps_as_stopped() -> None:
    turn = _turn({"type": "node_start", "node": "retrieval"})
    turn.stop()
    assert turn.status is TurnStatus.STOPPED
    assert turn.steps[0].status == "stopped"

    done = _turn(_end())
    done.stop()
    assert done.status is TurnStatus.DONE


def test_node_end_finishes_the_latest_run_of_that_node() -> None:
    turn = _turn(
        {"type": "node_start", "node": "supervisor"},
        {"type": "node_end", "node": "supervisor", "duration_ms": 800, "summary": ["Planned"]},
        {"type": "node_start", "node": "retrieval"},
        {"type": "node_start", "node": "supervisor"},
    )
    assert turn.active_node == "supervisor"

    turn.apply({"type": "node_end", "node": "supervisor", "duration_ms": 5, "status": "error"})
    first, search, second = turn.steps
    assert (first.status, first.duration_ms, first.summary) == ("ok", 800, ("Planned",))
    assert (second.status, second.duration_ms) == ("error", 5)
    assert search.status == "running"
    assert turn.active_node == "retrieval"


def test_a_node_end_without_its_start_still_shows_up() -> None:
    turn = _turn({"type": "node_end", "node": "validator", "duration_ms": 3})
    assert [(s.node, s.status) for s in turn.steps] == [("validator", "ok")]


def test_the_panel_details_are_collected_by_kind() -> None:
    turn = _turn(
        {"type": "agent_state", "status": "accepted", "run_id": "run-1", "traced": True},
        {"type": "agent_state", "status": "planned", "run_id": "run-1", "intent": "research"},
        {"type": "retrieval", "query": "pool", "hits": 3},
        {"type": "validation", "stage": "input", "passed": True},
        {"type": "degraded", "dependency": "language_model", "message": "Backup model used."},
        _token("x"),
    )
    assert (turn.run_id, turn.traced) == ("run-1", True)
    assert turn.plan is not None
    assert turn.plan["intent"] == "research"
    assert [s["query"] for s in turn.searches] == ["pool"]
    assert [c["stage"] for c in turn.checks] == ["input"]
    assert turn.warnings == ["Backup model used."]
    # Tokens are counted, not kept: the raw list holds everything else.
    assert len(turn.events) == 5
    assert turn.tokens == 1


def test_events_from_a_newer_api_are_kept_and_change_nothing_else() -> None:
    turn = _turn({"type": "tool_call", "tool": "incident_records"})
    assert turn.other == [{"type": "tool_call", "tool": "incident_records"}]
    assert turn.status is TurnStatus.STREAMING


def test_a_conversation_is_titled_by_its_first_question() -> None:
    conversation = Conversation(role="viewer")
    assert conversation.title == "New conversation"

    conversation.turns.append(Turn(question="What are the\npayment cut-off times?"))
    assert conversation.title == "What are the payment cut-off times?"

    conversation.turns[0] = Turn(question="word " * 20)
    assert len(conversation.title) == 40
    assert conversation.title.endswith("…")


def test_a_conversation_is_busy_while_an_answer_streams() -> None:
    conversation = Conversation(role="viewer", turns=[Turn(question="q")])
    assert conversation.busy
    conversation.turns[0].apply(_end())
    assert not conversation.busy
