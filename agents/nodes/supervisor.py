"""Supervisor: understands the question, plans the work and routes it.

The supervisor runs more than once per question:

- **First visit: plan.** One model call reads the question (and the recent
  conversation, for follow-ups) and returns the intent and a short plan.
  Code, not the model, then turns the intent into the list of agents to
  run (`AGENTS_FOR_INTENT`). So the model can describe the work, but it can
  never send the run to an agent that does not exist or is not allowed.
- **Every later visit: review.** After an agent returns, plain code checks
  its result and decides what happens next: run the next agent, retry once,
  or go on to the answer with what we have. No model call, so these
  decisions are free, fast and easy to test.

Failure containment: if the model cannot be reached, the supervisor uses a
safe default plan (search every collection, then answer), and the answer
says so. If an agent failed, the supervisor retries it once when that can
help, and otherwise moves on; the response agent then explains what was
missing.
"""

from types import MappingProxyType

from pydantic import BaseModel, Field

from agents.context import prompt
from agents.nodes.base import AgentNode, StateUpdate
from agents.state import AgentError, AgentName, AgentState, ErrorKind, Intent, Plan
from core.errors import LLMUnavailableError
from core.logging import get_logger

log = get_logger(__name__)

# Which agents each intent needs before the answer is written. The research
# agent (feature/rlm-research) and the tool agent (feature/python-sandbox,
# feature/mcp-server) are added here as they are built. Until then, research
# questions use a wider search and enterprise-data questions search the
# documents, and the answer says so.
AGENTS_FOR_INTENT = MappingProxyType(
    {
        Intent.DOCUMENT_QUESTION: (AgentName.RETRIEVAL,),
        Intent.RESEARCH: (AgentName.RETRIEVAL,),
        Intent.ENTERPRISE_DATA: (AgentName.RETRIEVAL,),
        Intent.CONVERSATION: (),
        Intent.OUT_OF_SCOPE: (),
    }
)

LIMIT_FOR_INTENT = MappingProxyType(
    {
        Intent.RESEARCH: (
            "Deep research across many documents is not available yet, so this answer "
            "comes from one wider search."
        ),
        Intent.ENTERPRISE_DATA: (
            "The enterprise data tools (directory, service catalog, incident records) are "
            "not connected yet, so this answer comes from the documents only."
        ),
    }
)

# One retry for an agent that failed in a way a retry can fix.
MAX_RETRIEVAL_ATTEMPTS = 2


class SupervisorDecision(BaseModel):
    """What the model returns on the first visit."""

    intent: Intent = Field(description="The kind of request.")
    plan: list[str] = Field(
        description="One to four short steps in plain words.", min_length=1, max_length=4
    )
    reason: str = Field(description="One short sentence on why this intent.")


def default_plan() -> Plan:
    """Used when the model cannot be reached: search everything, then answer."""
    return Plan(
        intent=Intent.DOCUMENT_QUESTION,
        steps=("Search all document collections", "Answer with citations"),
        agents=AGENTS_FOR_INTENT[Intent.DOCUMENT_QUESTION],
        reason="default plan",
    )


class Supervisor(AgentNode):
    name = AgentName.SUPERVISOR

    async def run(self, state: AgentState) -> StateUpdate:
        if state.get("plan") is None:
            return await self._plan(state)
        return self._review(state)

    async def _plan(self, state: AgentState) -> StateUpdate:
        context = self.context
        try:
            decision, call = await context.gateway.structured(
                SupervisorDecision,
                prompt(
                    "supervisor",
                    state["question"],
                    state.get("messages", []),
                    today=context.today(),
                    history=context.history_messages,
                ),
                purpose="supervisor",
            )
        except LLMUnavailableError:
            plan = default_plan()
            return {
                "plan": plan,
                "next_agent": plan.agents[0],
                "errors": {
                    self.name.value: self.failure(
                        ErrorKind.UNAVAILABLE, "The language model could not plan this question."
                    )
                },
                "steps": [
                    self.step(
                        "Could not reach the language model to plan, so used the default "
                        "plan: search all collections, then answer."
                    )
                ],
            }

        plan = Plan(
            intent=decision.intent,
            steps=tuple(step.strip() for step in decision.plan if step.strip())[:4],
            agents=AGENTS_FOR_INTENT[decision.intent],
            reason=decision.reason.strip()[:200],
        )
        update: StateUpdate = {
            "plan": plan,
            "next_agent": plan.agents[0] if plan.agents else AgentName.RESPONSE,
            "llm_calls": [call],
            "steps": [
                self.step(
                    f"Understood the request as {plan.intent.value.replace('_', ' ')}. "
                    f"Plan: {'; '.join(plan.steps) or 'answer directly'}."
                )
            ],
        }
        if limit := LIMIT_FOR_INTENT.get(plan.intent):
            update["limits"] = [limit]
        log.info("supervisor_planned", intent=plan.intent.value, agents=len(plan.agents))
        return update

    def _review(self, state: AgentState) -> StateUpdate:
        """Decide what runs next, after an agent has returned."""
        plan = state.get("plan")
        if plan is None:
            return {"next_agent": AgentName.RESPONSE}
        errors = state.get("errors", {})
        attempts = state.get("retrieval_attempts", 0)
        retrieval_error = errors.get(AgentName.RETRIEVAL.value)
        search = state.get("search")

        if attempts < MAX_RETRIEVAL_ATTEMPTS:
            if retrieval_error is not None and retrieval_error.retryable:
                return {
                    "next_agent": AgentName.RETRIEVAL,
                    "steps": [self.step("Document search failed, so tried it once more.")],
                }
            if search is not None and search.hits == 0 and _has_optional_filters(state):
                return {
                    "next_agent": AgentName.RETRIEVAL,
                    "relax_filters": True,
                    "steps": [
                        self.step("Nothing matched the filters, so searched again without them.")
                    ],
                }

        done = set(state.get("completed", []))
        pending = [agent for agent in plan.agents if agent not in done]
        return {"next_agent": pending[0] if pending else AgentName.RESPONSE}

    def on_error(self, state: AgentState, error: AgentError) -> StateUpdate:
        # A bug while planning: use the default plan. A bug while reviewing:
        # go straight to the answer with what we have.
        if state.get("plan") is None:
            plan = default_plan()
            return {"plan": plan, "next_agent": plan.agents[0]}
        return {"next_agent": AgentName.RESPONSE}


def _has_optional_filters(state: AgentState) -> bool:
    search = state.get("search")
    if search is None or state.get("relax_filters"):
        return False
    filters = search.filters
    return bool(filters.departments or filters.date_from or filters.date_to)
