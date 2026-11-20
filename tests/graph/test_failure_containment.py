"""One failing agent never breaks the run (the "butterfly effect" bonus).

Each test breaks one part on purpose (the model, the search, or a node
itself) and checks that the user still gets a sensible answer that says
what was missing.
"""

import pytest

from agents.nodes.retrieval import RetrievalAgent
from agents.nodes.supervisor import Supervisor
from agents.state import AgentName, AnswerKind, ErrorKind
from agents.telemetry import AgentMetrics
from core.config import LogFormat
from core.errors import LLMUnavailableError, RetrievalError
from core.logging import configure_logging
from rag.retrieval import RerankStatus
from rag.schema import Collection
from security.roles import Role
from tests.helpers.agents import (
    FakeSearcher,
    agent_service,
    decision,
    hit,
    result_with,
    search_plan,
)
from tests.helpers.llm import ScriptedChatModel

DOWN = LLMUnavailableError()


def _errors(metrics: AgentMetrics, node: str, kind: str) -> float:
    return metrics.node_errors.labels(node=node, kind=kind)._value.get()  # type: ignore[no-any-return]


async def test_model_down_while_planning_uses_the_default_plan_and_still_answers() -> None:
    model = ScriptedChatModel(
        structured={"SupervisorDecision": [DOWN], "SearchPlan": [search_plan("pool")]},
        texts=["Restart it [1]."],
    )
    searcher = FakeSearcher([result_with(hit("RB-001"))])

    run = await agent_service(model, searcher).ask("pool?", role=Role.VIEWER)

    assert run.answer.kind is AnswerKind.ANSWERED
    assert "default plan" in run.answer.steps[0]
    assert "The language model could not plan this question." in run.answer.limits


async def test_model_down_while_planning_the_search_searches_everything_as_written() -> None:
    model = ScriptedChatModel(
        structured={"SupervisorDecision": [decision("document_question")], "SearchPlan": [DOWN]},
        texts=["Restart it [1]."],
    )
    searcher = FakeSearcher([result_with(hit("RB-001"))])

    run = await agent_service(model, searcher).ask("Why is PayHub slow?", role=Role.VIEWER)

    request = searcher.requests[0][0]
    assert request.query == "Why is PayHub slow?"
    assert request.collections == tuple(Collection)
    assert run.answer.kind is AnswerKind.ANSWERED


async def test_search_failing_once_is_retried_and_the_answer_is_normal() -> None:
    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("document_question")],
            "SearchPlan": [search_plan("pool")],
        },
        texts=["Restart it [1]."],
    )
    searcher = FakeSearcher([RetrievalError(), result_with(hit("RB-001"))])

    run = await agent_service(model, searcher).ask("pool?", role=Role.VIEWER)

    assert len(searcher.requests) == 2
    assert run.nodes.count("retrieval") == 2
    assert run.answer.kind is AnswerKind.ANSWERED
    # The search plan is reused on the retry: one model call, not two.
    assert [c.purpose for c in run.llm_calls].count("search_plan") == 1
    # The error was cleared by the successful retry, so it is not a limit.
    assert run.errors == ()


async def test_search_down_gives_a_clear_unavailable_answer_after_one_retry() -> None:
    metrics = AgentMetrics()
    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("document_question")],
            "SearchPlan": [search_plan("pool")],
        }
    )
    searcher = FakeSearcher([RetrievalError()])

    run = await agent_service(model, searcher, metrics=metrics).ask("pool?", role=Role.VIEWER)

    assert len(searcher.requests) == 2
    assert run.answer.kind is AnswerKind.UNAVAILABLE
    assert "Document search is unavailable" in run.answer.text
    assert _errors(metrics, "retrieval", "unavailable") == 2
    # No answer model call: there is nothing to write from.
    assert "answer" not in [c.purpose for c in run.llm_calls]


async def test_collections_that_did_not_answer_are_named_in_the_limits() -> None:
    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("document_question")],
            "SearchPlan": [search_plan("outage", ["incidents", "runbooks"])],
        },
        texts=["Restart it [1]."],
    )
    partial = result_with(hit("RB-001"), failed=(Collection.INCIDENTS,), rerank=RerankStatus.FAILED)

    run = await agent_service(model, FakeSearcher([partial])).ask("outage?", role=Role.VIEWER)

    assert run.answer.kind is AnswerKind.ANSWERED
    assert any("incidents" in limit for limit in run.answer.limits)
    assert any("re-ranker" in limit for limit in run.answer.limits)


async def test_nothing_found_with_filters_searches_again_without_them() -> None:
    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("document_question")],
            "SearchPlan": [search_plan("pool", departments=["cards"], date_from="2026-10-01")],
        },
        texts=["Restart it [1]."],
    )
    searcher = FakeSearcher([result_with(), result_with(hit("RB-001"))])

    run = await agent_service(model, searcher).ask("pool?", role=Role.VIEWER)

    first, second = (request for request, _ in searcher.requests)
    assert first.filters.departments
    assert first.filters.date_from
    assert not second.filters.departments
    assert second.filters.date_from is None
    assert run.answer.kind is AnswerKind.ANSWERED
    assert any("without them" in step for step in run.answer.steps)


async def test_model_down_while_answering_lists_the_sources_instead() -> None:
    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("document_question")],
            "SearchPlan": [search_plan("pool")],
        },
        texts=[DOWN],
    )
    searcher = FakeSearcher([result_with(hit("RB-001"), hit("RB-001"), hit("RB-002"))])

    run = await agent_service(model, searcher).ask("pool?", role=Role.VIEWER)

    assert run.answer.kind is AnswerKind.FALLBACK
    # One line per document, even when a document has several chunks.
    assert [c.citation.doc_id for c in run.answer.citations] == ["RB-001", "RB-002"]
    assert "The language model could not write the answer." in run.answer.limits


async def test_every_model_down_still_returns_an_answer() -> None:
    model = ScriptedChatModel(
        structured={"SupervisorDecision": [DOWN], "SearchPlan": [DOWN]}, texts=[DOWN]
    )
    run = await agent_service(model, FakeSearcher([result_with(hit("RB-001"))])).ask(
        "pool?", role=Role.VIEWER
    )
    assert run.answer.kind is AnswerKind.FALLBACK
    assert run.answer.citations


@pytest.mark.parametrize("node_class", [Supervisor, RetrievalAgent])
async def test_a_bug_inside_an_agent_is_contained_and_logged(
    node_class: type[Supervisor | RetrievalAgent],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unexpected exception in one node becomes an error slot, not a crash."""

    async def broken(self: object, state: object) -> dict[str, object]:
        raise ValueError("a bug")

    configure_logging("INFO", LogFormat.JSON)
    monkeypatch.setattr(node_class, "run", broken)
    metrics = AgentMetrics()
    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("document_question")],
            "SearchPlan": [search_plan("pool")],
        },
        texts=["Restart it [1]."],
    )
    searcher = FakeSearcher([result_with(hit("RB-001"))])

    run = await agent_service(model, searcher, metrics=metrics).ask("pool?", role=Role.VIEWER)

    name = node_class.name.value
    assert run.answer is not None
    assert any(e.agent is node_class.name and e.kind is ErrorKind.INTERNAL for e in run.errors)
    assert _errors(metrics, name, "internal") >= 1
    assert "agent_node_failed" in capsys.readouterr().out


async def test_a_bug_in_the_response_agent_still_gives_the_source_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.nodes.response import ResponseAgent

    async def broken(self: object, state: object) -> dict[str, object]:
        raise KeyError("bug")

    monkeypatch.setattr(ResponseAgent, "run", broken)
    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("document_question")],
            "SearchPlan": [search_plan("pool")],
        }
    )
    run = await agent_service(model, FakeSearcher([result_with(hit("RB-001"))])).ask(
        "pool?", role=Role.VIEWER
    )
    assert run.answer.kind is AnswerKind.FALLBACK
    assert [c.citation.doc_id for c in run.answer.citations] == ["RB-001"]


async def test_a_bug_in_the_input_guard_refuses_rather_than_lets_the_question_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.nodes.input_guard import InputGuard

    async def broken(self: object, state: object) -> dict[str, object]:
        raise RuntimeError("bug")

    monkeypatch.setattr(InputGuard, "run", broken)
    model = ScriptedChatModel()
    run = await agent_service(model, FakeSearcher()).ask("pool?", role=Role.VIEWER)
    assert run.answer.kind is AnswerKind.REFUSED
    assert model.calls == []


async def test_a_bug_in_the_validator_gives_the_source_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.nodes.validator import Validator

    async def broken(self: object, state: object) -> dict[str, object]:
        raise RuntimeError("bug")

    monkeypatch.setattr(Validator, "run", broken)
    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("document_question")],
            "SearchPlan": [search_plan("pool")],
        },
        texts=["Unchecked answer [1]."],
    )
    run = await agent_service(model, FakeSearcher([result_with(hit("RB-001"))])).ask(
        "pool?", role=Role.VIEWER
    )
    # An answer that was never checked is not shown.
    assert run.answer.kind is AnswerKind.FALLBACK
    assert AgentName.VALIDATOR in {error.agent for error in run.errors}
