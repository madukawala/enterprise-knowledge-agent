"""Human approval before deep research: interrupt, then resume (approved or not)."""

from agents.checkpoint import memory_checkpointer
from agents.events import ApprovalRequiredEvent, ResponseEndEvent
from agents.service import AgentService
from agents.state import AnswerKind
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

QUESTION = "Summarize payment-failure outages over the last year"


def _agents(searcher: FakeSearcher | None = None) -> tuple[AgentService, ScriptedChatModel]:
    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("research")],
            "BatchFindings": [{"findings": []}] * 4,
            "SearchPlan": [search_plan("payment outages", ["incidents"])],
        },
        texts=["Two outages [1]."],
    )
    agents = agent_service(
        model,
        searcher or FakeSearcher([result_with(hit("INC-1"))]),
        checkpointer=memory_checkpointer(),
        approval_for_research=True,
    )
    return agents, model


async def _stream(agents: AgentService, thread: str, role: Role = Role.ANALYST) -> list[object]:
    return [e async for e in agents.stream(QUESTION, role=role, thread_id=thread)]


async def test_research_stops_and_asks_before_anything_runs() -> None:
    searcher = FakeSearcher([result_with(hit("INC-1"))])
    agents, model = _agents(searcher)

    events = await _stream(agents, "thread-approve-1")

    last = events[-1]
    assert isinstance(last, ApprovalRequiredEvent)
    assert last.approval_id == "thread-approve-1"
    assert last.action == "research"
    assert not any(isinstance(e, ResponseEndEvent) for e in events)
    assert searcher.requests == []  # nothing searched yet
    assert model.prompts_for("BatchFindings") == []
    waiting = await agents.pending_approval("thread-approve-1")
    assert waiting is not None
    assert waiting["action"] == "research"


async def test_approved_research_runs_from_where_it_stopped() -> None:
    searcher = FakeSearcher([result_with(hit("INC-1"))])
    agents, _ = _agents(searcher)
    await _stream(agents, "thread-approve-2")

    events = [e async for e in agents.resume("thread-approve-2", approved=True, role=Role.ANALYST)]

    final = events[-1]
    assert isinstance(final, ResponseEndEvent)
    assert final.kind is AnswerKind.ANSWERED
    assert len(searcher.requests) == 4  # the research batches
    assert "You approved the deep research." in final.steps
    assert await agents.pending_approval("thread-approve-2") is None


async def test_rejected_research_becomes_one_normal_search() -> None:
    searcher = FakeSearcher([result_with(hit("INC-1"))])
    agents, model = _agents(searcher)
    await _stream(agents, "thread-approve-3")

    events = [e async for e in agents.resume("thread-approve-3", approved=False, role=Role.ANALYST)]

    final = events[-1]
    assert isinstance(final, ResponseEndEvent)
    assert len(searcher.requests) == 1  # the retrieval agent's one search
    assert model.prompts_for("BatchFindings") == []
    assert any("not approved" in limit for limit in final.limits)


async def test_ask_resumes_with_its_approve_argument() -> None:
    rejected, _ = _agents()
    run = await rejected.ask(QUESTION, role=Role.ANALYST, thread_id="thread-ask-1")
    assert any("not approved" in limit for limit in run.answer.limits)
    assert run.nodes[:3] == ("input_guard", "supervisor", "approval")

    approved, _ = _agents()
    run = await approved.ask(QUESTION, role=Role.ANALYST, thread_id="thread-ask-2", approve=True)
    assert "research" in run.nodes


async def test_a_viewer_is_never_asked_because_research_is_not_theirs() -> None:
    agents, _ = _agents()
    events = await _stream(agents, "thread-viewer", role=Role.VIEWER)
    assert isinstance(events[-1], ResponseEndEvent)
    assert not any(isinstance(e, ApprovalRequiredEvent) for e in events)


async def test_nothing_is_waiting_on_an_ordinary_thread() -> None:
    agents, _ = _agents()
    assert await agents.pending_approval("never-used") is None


async def test_a_resumed_run_uses_the_role_of_the_person_resuming_it() -> None:
    """The role comes from the current login, not from the saved state."""
    searcher = FakeSearcher([result_with(hit("INC-1"))])
    agents, _ = _agents(searcher)
    await _stream(agents, "thread-role")
    # Simulate a checkpoint that kept the role as plain text.
    await agents.graph.aupdate_state(
        {"configurable": {"thread_id": "thread-role"}}, {"role": "analyst"}
    )

    events = [e async for e in agents.resume("thread-role", approved=False, role=Role.ANALYST)]

    assert isinstance(events[-1], ResponseEndEvent)
    assert {role for _, role in searcher.requests} == {Role.ANALYST}
    assert all(isinstance(role, Role) for _, role in searcher.requests)
