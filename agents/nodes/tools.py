"""Tools agent: live enterprise data from the MCP server (analysts and admins).

For questions such as "who owns PayHub?" or "which incidents are open?":

1. **Check the role again, in code.** The supervisor only sends roles with
   the `mcp_tools` permission here; this node checks once more before
   anything else, so a routing mistake cannot open the tools to a viewer.
2. **Choose the calls (one model call).** The model sees only the tools
   the role may use, and returns up to three calls.
3. **Check every call in code.** Unknown tools and bad arguments are
   refused before anything is sent (agents/tools/enterprise.py).
4. **Call the server**, all calls at the same time. Results go to the
   answer as data, inside `<tool_results>` tags, and each call is shown in
   the activity panel.

Failure containment: if the server is down or a call fails, the question
is still answered from the documents (the retrieval agent runs next), and
the answer says which data was missing.
"""

import asyncio
from typing import Any, Literal

from pydantic import BaseModel, Field

from agents.context import prompt
from agents.nodes.base import AgentNode, StateUpdate
from agents.state import AgentName, AgentState, ToolRecord
from agents.tools.enterprise import DESCRIPTIONS
from core.errors import LLMUnavailableError
from core.logging import SECURITY_LOGGER, get_logger
from security.policies import Action, is_allowed

log = get_logger(__name__)
security_log = get_logger(SECURITY_LOGGER)

MAX_CALLS = 3
UNAVAILABLE = (
    "The enterprise data tools (directory, service catalog, incident records) did not "
    "answer, so this answer comes from the documents only."
)
NOT_CONFIGURED = (
    "The enterprise data tools are not connected, so this answer comes from the documents only."
)


class ToolCall(BaseModel):
    tool: Literal["find_person", "service_owner", "list_incidents"] = Field(
        description="Which tool to call."
    )
    arguments: dict[str, Any] = Field(default_factory=dict, description="The tool's arguments.")


class ToolPlan(BaseModel):
    """What the model returns: the calls to make."""

    calls: list[ToolCall] = Field(default_factory=list, max_length=MAX_CALLS)


class ToolsAgent(AgentNode):
    name = AgentName.TOOLS

    async def run(self, state: AgentState) -> StateUpdate:
        role = state["role"]
        update: StateUpdate = {"completed": [self.name]}
        if not is_allowed(role, Action.MCP_TOOLS):
            security_log.warning("tools_refused", role=role.value)
            update["limits"] = ["Your role cannot use the enterprise data tools."]
            return update

        tools = self.context.enterprise
        if tools is None:
            update["limits"] = [NOT_CONFIGURED]
            update["steps"] = [self.step("The enterprise data tools are not connected.")]
            return update

        try:
            plan, call = await self.context.gateway.structured(
                ToolPlan,
                prompt(
                    "tools",
                    state["question"],
                    state.get("messages", []),
                    today=self.context.today(),
                    history=self.context.history_messages,
                    summary=state.get("summary") or "",
                    values={"tools": "\n".join(f"- {d}" for d in DESCRIPTIONS.values())},
                ),
                purpose="tool_choice",
            )
        except LLMUnavailableError:
            update["steps"] = [self.step("Could not reach the model to choose a tool.")]
            update["limits"] = [UNAVAILABLE]
            return update
        update["llm_calls"] = [call]

        calls = plan.calls[:MAX_CALLS]
        if not calls:
            update["steps"] = [self.step("None of the enterprise data tools fits this question.")]
            return update

        results = await asyncio.gather(*(tools.call(c.tool, c.arguments) for c in calls))
        records = [
            ToolRecord(
                tool=r.tool,
                arguments=r.arguments,
                ok=r.ok,
                items=r.items,
                error=r.error,
                duration_ms=r.duration_ms,
                text=r.as_text() if r.ok else "",
            )
            for r in results
        ]
        for record in records:
            self.context.metrics.tool_runs.labels(
                tool=record.tool, status="ok" if record.ok else "error"
            ).inc()
        failed = [r for r in records if not r.ok]
        update["tool_results"] = records
        update["steps"] = [
            self.step(
                f"Called {len(records)} enterprise data tool{'s' if len(records) != 1 else ''}: "
                + "; ".join(_describe(r) for r in records)
                + "."
            )
        ]
        if failed:
            update["limits"] = [
                "Some enterprise data could not be fetched ("
                + ", ".join(f"{r.tool}: {r.error}" for r in failed)
                + "), so the answer may be incomplete."
            ]
        log.info("tools_called", calls=len(records), failed=len(failed))
        return update


def _describe(record: ToolRecord) -> str:
    args = ", ".join(f"{k}={v!r}" for k, v in record.arguments.items())
    outcome = f"{record.items} found" if record.ok else "failed"
    return f"{record.tool}({args}) {outcome}"
