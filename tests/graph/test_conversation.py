"""Several questions in one thread: follow-ups, a clean slate per question,
and the checkpoint that carries the conversation."""

import logging

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agents import graph as graph_module
from agents.checkpoint import ALLOWED_TYPES, memory_checkpointer
from agents.state import AnswerKind
from core.errors import RetrievalError
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


def _two_turn_model() -> ScriptedChatModel:
    return ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("document_question")] * 2,
            "SearchPlan": [search_plan("payments pool"), search_plan("cards pool")],
        },
        texts=["Payments: restart it [1].", "Cards: same steps [1]."],
    )


async def test_a_follow_up_question_sees_the_earlier_conversation() -> None:
    model = _two_turn_model()
    agents = agent_service(
        model, FakeSearcher([result_with(hit("RB-001"))]), checkpointer=memory_checkpointer()
    )

    first = await agents.ask("How do I fix the payments pool?", role=Role.VIEWER)
    second = await agents.ask("and for cards?", role=Role.VIEWER, thread_id=first.thread_id)

    assert second.answer.text == "Cards: same steps [1]."
    follow_up_plan = model.prompts_for("SearchPlan")[1]
    assert "User: How do I fix the payments pool?" in follow_up_plan
    assert "Assistant: Payments: restart it [1]." in follow_up_plan
    assert follow_up_plan.endswith("<question>\nand for cards?\n</question>")


async def test_each_question_starts_with_a_clean_slate() -> None:
    """Errors, steps and sources from the first question do not leak into the second."""
    model = _two_turn_model()
    searcher = FakeSearcher([RetrievalError(), RetrievalError(), result_with(hit("RB-002"))])
    agents = agent_service(model, searcher, checkpointer=memory_checkpointer())

    first = await agents.ask("payments pool?", role=Role.VIEWER)
    model.texts = ["Cards: same steps [1]."]
    second = await agents.ask("and cards?", role=Role.VIEWER, thread_id=first.thread_id)

    assert first.answer.kind is AnswerKind.UNAVAILABLE
    assert second.answer.kind is AnswerKind.ANSWERED
    assert second.errors == ()
    assert len(second.answer.steps) == 4
    assert [c.citation.doc_id for c in second.answer.citations] == ["RB-002"]


async def test_the_role_is_taken_from_each_call_not_from_the_thread() -> None:
    """A thread started by an admin and continued as a viewer searches as a viewer."""
    searcher = FakeSearcher([result_with(hit("RB-001"))])
    agents = agent_service(_two_turn_model(), searcher, checkpointer=memory_checkpointer())

    first = await agents.ask("payments pool?", role=Role.ADMIN)
    await agents.ask("and cards?", role=Role.VIEWER, thread_id=first.thread_id)

    assert [role for _, role in searcher.requests] == [Role.ADMIN, Role.VIEWER]


async def test_the_saved_conversation_holds_question_and_answer_text_only() -> None:
    agents = agent_service(
        _two_turn_model(),
        FakeSearcher([result_with(hit("RB-001"))]),
        checkpointer=memory_checkpointer(),
    )
    run = await agents.ask("payments pool?", role=Role.VIEWER)

    state = await agents.graph.aget_state({"configurable": {"thread_id": run.thread_id}})
    messages = state.values["messages"]
    assert [type(m) for m in messages] == [HumanMessage, AIMessage]
    assert messages[1].text == "Payments: restart it [1]."


async def test_checkpoints_rebuild_only_allowed_types(caplog: pytest.LogCaptureFixture) -> None:
    """Every type in the state is on the allow-list, so nothing is blocked or warned about."""
    caplog.set_level(logging.WARNING)
    agents = agent_service(
        _two_turn_model(),
        FakeSearcher([result_with(hit("RB-001"))]),
        checkpointer=memory_checkpointer(),
    )
    first = await agents.ask("payments pool?", role=Role.VIEWER)
    await agents.ask("and cards?", role=Role.VIEWER, thread_id=first.thread_id)

    assert not [r for r in caplog.records if "msgpack" in r.getMessage().lower()]
    assert all(isinstance(kind, type) for kind in ALLOWED_TYPES)


async def test_a_routing_loop_is_stopped_and_still_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a routing bug made the run loop, the recursion limit ends it safely."""
    monkeypatch.setattr(graph_module, "after_supervisor", lambda state: "retrieval")
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
