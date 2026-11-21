"""The tools agent in the graph: live enterprise data for analysts and admins."""

from typing import Any

from agents.events import ToolCallEvent
from agents.state import AnswerKind
from agents.tools.enterprise import EnterpriseTools
from mcp_server.server import build_server
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


def _model(calls: list[dict[str, Any]], answer: str) -> ScriptedChatModel:
    return ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("enterprise_data")],
            "ToolPlan": [{"calls": calls}],
            "SearchPlan": [search_plan("PayHub owner", ["architecture"])],
        },
        texts=[answer],
    )


def _tools() -> EnterpriseTools:
    return EnterpriseTools(build_server())


async def test_an_analyst_gets_live_data_in_the_answer() -> None:
    model = _model(
        [{"tool": "service_owner", "arguments": {"service": "PayHub"}}],
        "According to the live service catalog, Grace Okafor owns PayHub.",
    )
    agents = agent_service(model, FakeSearcher([result_with(hit("ARC-001"))]), enterprise=_tools())

    events = [e async for e in agents.stream("Who owns PayHub?", role=Role.ANALYST)]

    (call,) = [e for e in events if isinstance(e, ToolCallEvent)]
    assert (call.tool, call.arguments, call.ok, call.items) == (
        "service_owner",
        {"service": "PayHub"},
        True,
        1,
    )
    answer_prompt = model.prompts_for("text")[0]
    assert "<tool_results>\n" in answer_prompt
    assert "Grace Okafor" in answer_prompt
    final = events[-1]
    # Facts from live data need no source number.
    assert final.kind is AnswerKind.ANSWERED  # type: ignore[union-attr]


async def test_the_model_sees_only_the_tools_and_the_tools_prompt_lists_them() -> None:
    model = _model([], "From the documents [1].")
    await agent_service(
        model, FakeSearcher([result_with(hit("ARC-001"))]), enterprise=_tools()
    ).ask("Who owns PayHub?", role=Role.ANALYST)
    tools_prompt = model.prompts_for("ToolPlan")[0]
    for tool in ("find_person(name)", "service_owner(service)", "list_incidents("):
        assert tool in tools_prompt


async def test_bad_arguments_from_the_model_are_refused_and_reported() -> None:
    model = _model(
        [{"tool": "list_incidents", "arguments": {"status": "deleted", "limit": 1000}}],
        "From the documents [1].",
    )
    run = await agent_service(
        model, FakeSearcher([result_with(hit("ARC-001"))]), enterprise=_tools()
    ).ask("List deleted incidents", role=Role.ANALYST)

    assert any("list_incidents: bad arguments" in limit for limit in run.answer.limits)
    assert "<tool_results>\n" not in model.prompts_for("text")[0]


async def test_a_server_that_is_down_means_documents_only_and_a_limit() -> None:
    model = _model(
        [{"tool": "service_owner", "arguments": {"service": "PayHub"}}],
        "The Payments Platform team owns PayHub [1].",
    )
    down = EnterpriseTools("http://127.0.0.1:9/mcp", timeout_seconds=2)

    run = await agent_service(
        model, FakeSearcher([result_with(hit("ARC-001"))]), enterprise=down
    ).ask("Who owns PayHub?", role=Role.ANALYST)

    assert run.answer.kind is AnswerKind.ANSWERED
    assert "retrieval" in run.nodes
    assert any("did not respond" in limit for limit in run.answer.limits)


async def test_a_viewer_never_reaches_the_tools() -> None:
    model = _model(
        [{"tool": "find_person", "arguments": {"name": "Grace"}}], "From the documents [1]."
    )
    run = await agent_service(
        model, FakeSearcher([result_with(hit("ARC-001"))]), enterprise=_tools()
    ).ask("Who owns PayHub?", role=Role.VIEWER)

    assert "tools" not in run.nodes
    assert model.prompts_for("ToolPlan") == []
    assert any("cannot use enterprise data tools" in limit for limit in run.answer.limits)


async def test_the_tools_agent_checks_the_role_itself() -> None:
    """Defence in depth: even if routing sent a viewer here, nothing is called."""
    from agents.nodes.tools import ToolsAgent
    from tests.helpers.agents import node_context

    model = _model([{"tool": "find_person", "arguments": {"name": "Grace"}}], "x")
    agent = ToolsAgent(node_context(model, enterprise=_tools()))

    update = await agent.run({"role": Role.VIEWER, "question": "Who is Grace?", "messages": []})

    assert "tool_results" not in update
    assert model.calls == []
