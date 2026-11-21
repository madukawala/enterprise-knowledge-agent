"""The events of a full graph run, in order, as the activity panel gets them.

These run the real graph with a scripted model that streams word by word,
so the token path is the same one the real models take.
"""

from collections.abc import Sequence
from contextlib import aclosing

from agents.events import (
    AgentEvent,
    AgentStateEvent,
    DegradedEvent,
    NodeEndEvent,
    NodeStartEvent,
    ResponseEndEvent,
    ResponseStartEvent,
    RetrievalEvent,
    TokenEvent,
    ValidationEvent,
)
from agents.service import AgentService
from agents.state import AgentName, AnswerKind, Intent
from core.errors import RetrievalError
from security.roles import Role
from tests.helpers.agents import (
    FakeSearcher,
    agent_service,
    decision,
    gateway,
    hit,
    result_with,
    search_plan,
)
from tests.helpers.llm import PartialFailure, ScriptedChatModel


def _model(*texts: object, intent: str = "document_question") -> ScriptedChatModel:
    return ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision(intent)],
            "SearchPlan": [search_plan("pool exhausted", ["runbooks"])],
        },
        texts=list(texts),  # type: ignore[arg-type]
    )


async def _events(agents: AgentService, question: str = "pool?") -> list[AgentEvent]:
    async with aclosing(agents.stream(question, role=Role.VIEWER)) as stream:
        return [event async for event in stream]


def _types(events: Sequence[AgentEvent]) -> list[str]:
    """Event types, with the node name for node events, and tokens collapsed."""
    names: list[str] = []
    for event in events:
        if isinstance(event, NodeStartEvent | NodeEndEvent):
            names.append(f"{event.type.value}:{event.node.value}")
        elif not (isinstance(event, TokenEvent) and names[-1] == "token"):
            names.append(event.type.value)
    return names


def _streamed_text(events: Sequence[AgentEvent]) -> str:
    return "".join(e.text for e in events if isinstance(e, TokenEvent))


async def test_a_document_question_streams_every_step_in_order() -> None:
    searcher = FakeSearcher([result_with(hit("RB-001"), hit("RB-002"))])
    events = await _events(agent_service(_model("Raise the pool size [1]."), searcher))

    assert _types(events) == [
        "agent_state",
        "node_start:input_guard",
        "validation",
        "node_end:input_guard",
        "node_start:supervisor",
        "agent_state",
        "node_end:supervisor",
        "node_start:retrieval",
        "retrieval",
        "node_end:retrieval",
        "node_start:supervisor",
        "node_end:supervisor",
        "node_start:response",
        "response_start",
        "token",
        "node_end:response",
        "node_start:validator",
        "validation",
        "node_end:validator",
        "node_start:memory",
        "node_end:memory",
        "response_end",
    ]


async def test_the_events_carry_what_the_panel_shows() -> None:
    searcher = FakeSearcher([result_with(hit("RB-001"), hit("RB-002"))])
    agents = agent_service(_model("Raise the pool size [1]."), searcher)
    async with aclosing(agents.stream("pool?", role=Role.VIEWER, thread_id="thread-1")) as s:
        events = [event async for event in s]

    accepted, planned = [e for e in events if isinstance(e, AgentStateEvent)]
    assert (accepted.status, accepted.thread_id) == ("accepted", "thread-1")
    assert planned.intent is Intent.DOCUMENT_QUESTION
    assert planned.agents == (AgentName.RETRIEVAL,)
    assert planned.run_id == accepted.run_id

    (search,) = [e for e in events if isinstance(e, RetrievalEvent)]
    assert (search.query, search.hits, search.collections) == ("pool exhausted", 2, ("runbooks",))

    input_check, output_check = [e for e in events if isinstance(e, ValidationEvent)]
    assert (input_check.stage, input_check.passed) == ("input", True)
    assert (output_check.stage, output_check.passed) == ("output", True)

    retrieval_end = next(
        e for e in events if isinstance(e, NodeEndEvent) and e.node is AgentName.RETRIEVAL
    )
    assert retrieval_end.status == "ok"
    assert retrieval_end.duration_ms >= 0
    assert any("Searched" in line or "search" in line for line in retrieval_end.summary)

    end = events[-1]
    assert isinstance(end, ResponseEndEvent)
    assert end.kind is AnswerKind.ANSWERED
    assert end.written_by == "anthropic / anthropic-test"
    assert [c.doc_id for c in end.citations] == ["RB-001"]
    assert end.markdown.startswith("Raise the pool size [1].")
    assert [call.purpose for call in end.llm_calls] == ["supervisor", "search_plan", "answer"]


async def test_the_tokens_add_up_to_the_answer() -> None:
    text = (
        "Raise the pool size to 200 [1], then restart the workers [1]. Watch the slow "
        "query dashboard for ten minutes afterwards, and open an incident if it does not "
        "recover [1]."
    )
    events = await _events(agent_service(_model(text), FakeSearcher([result_with(hit("RB-001"))])))
    tokens = [e for e in events if isinstance(e, TokenEvent)]
    # Really streamed, not sent in one piece. The last 48 characters wait
    # until the draft is finished, in case they hold personal data.
    assert len(tokens) > 5
    assert _streamed_text(events) == text
    final = events[-1]
    assert isinstance(final, ResponseEndEvent)
    assert final.text == text


async def test_a_rewrite_starts_a_new_response_and_the_end_has_the_final_text() -> None:
    model = _model("The limit is 200 [9].", "The limit is 200 [1].")
    events = await _events(agent_service(model, FakeSearcher([result_with(hit("RB-001"))])))

    starts = [e for e in events if isinstance(e, ResponseStartEvent)]
    assert [(s.attempt, s.reason) for s in starts] == [(1, "first"), (2, "rewrite")]
    checks = [e for e in events if isinstance(e, ValidationEvent) and e.stage == "output"]
    assert [c.passed for c in checks] == [False, True]
    assert "It cites [9]" in " ".join(checks[0].issues)
    final = events[-1]
    assert isinstance(final, ResponseEndEvent)
    assert final.text == "The limit is 200 [1]."


async def test_a_model_failing_mid_answer_restarts_the_text_with_the_next_model() -> None:
    claude = _model(PartialFailure("The pool limit", RuntimeError("connection reset")))
    gemini = ScriptedChatModel(texts=["Restart the workers [1]."])
    agents = agent_service(gateway(claude, gemini), FakeSearcher([result_with(hit("RB-1"))]))

    events = await _events(agents)

    starts = [e for e in events if isinstance(e, ResponseStartEvent)]
    assert [s.reason for s in starts] == ["first", "model_switch"]
    after_switch = events[events.index(starts[1]) :]
    assert _streamed_text(after_switch) == "Restart the workers [1]."
    degraded = [e for e in events if isinstance(e, DegradedEvent)]
    assert [d.dependency for d in degraded] == ["language_model"]
    assert "gemini / gemini-test" in degraded[0].message
    final = events[-1]
    assert isinstance(final, ResponseEndEvent)
    assert final.written_by == "gemini / gemini-test"


async def test_not_found_never_flashes_up_before_the_friendly_message() -> None:
    events = await _events(
        agent_service(_model("NOT_FOUND."), FakeSearcher([result_with(hit("RB-001"))]))
    )
    assert _streamed_text(events) == ""
    final = events[-1]
    assert isinstance(final, ResponseEndEvent)
    assert final.kind is AnswerKind.NOT_FOUND
    assert "couldn't find" in final.text


async def test_search_down_is_reported_as_degraded_and_the_node_as_failed() -> None:
    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("document_question")],
            "SearchPlan": [search_plan("pool"), search_plan("pool")],
        },
    )
    events = await _events(agent_service(model, FakeSearcher([RetrievalError()])))

    degraded = [e for e in events if isinstance(e, DegradedEvent)]
    assert degraded
    assert {d.dependency for d in degraded} == {"documents"}
    retrieval_ends = [
        e for e in events if isinstance(e, NodeEndEvent) and e.node is AgentName.RETRIEVAL
    ]
    assert {e.status for e in retrieval_ends} == {"error"}
    final = events[-1]
    assert isinstance(final, ResponseEndEvent)
    assert final.kind is AnswerKind.UNAVAILABLE
    # A fixed answer: no tokens, the text comes with response_end.
    assert _streamed_text(events) == ""


async def test_a_blocked_question_reports_the_failed_input_check_and_ends() -> None:
    events = await _events(agent_service(ScriptedChatModel(), FakeSearcher()), question="   ")

    assert _types(events) == [
        "agent_state",
        "node_start:input_guard",
        "validation",
        "node_end:input_guard",
        "node_start:response",
        "response_start",
        "node_end:response",
        "node_start:validator",
        "validation",
        "node_end:validator",
        "node_start:memory",
        "node_end:memory",
        "response_end",
    ]
    input_check = next(e for e in events if isinstance(e, ValidationEvent))
    assert (input_check.stage, input_check.passed) == ("input", False)
    assert input_check.issues


async def test_small_talk_streams_without_a_search() -> None:
    model = ScriptedChatModel(
        structured={"SupervisorDecision": [decision("conversation", ["Reply"])]},
        texts=["Hello! Ask me about the bank's documents."],
    )
    events = await _events(agent_service(model, FakeSearcher()), question="hi")
    assert not any(isinstance(e, RetrievalEvent) for e in events)
    assert _streamed_text(events) == "Hello! Ask me about the bank's documents."


async def test_personal_data_never_appears_in_the_streamed_preview() -> None:
    text = (
        "Call the on-call lead on +44 20 7946 0958 or write to jane.doe@example.com, "
        "and quote card 4111 1111 1111 1111 only if asked [1]."
    )
    events = await _events(agent_service(_model(text), FakeSearcher([result_with(hit("RB-001"))])))

    streamed = _streamed_text(events)
    for private in ("7946 0958", "jane.doe@example.com", "4111 1111 1111 1111"):
        assert private not in streamed
        assert private not in "".join(e.model_dump_json() for e in events)
    assert "[PHONE]" in streamed
    assert "[EMAIL]" in streamed
    final = events[-1]
    assert isinstance(final, ResponseEndEvent)
    assert final.text == streamed
