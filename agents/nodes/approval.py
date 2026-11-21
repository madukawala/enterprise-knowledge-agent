"""Human approval: the graph stops and asks before deep research runs.

Deep research can cost several model calls and a minute of work, so the
user confirms it first. The supervisor puts this node in front of the
research agent when `APPROVAL_FOR_RESEARCH` is on.

How it works (LangGraph's `interrupt`):

1. This node calls `interrupt(...)` with what needs approving. The graph
   stops, its state is saved by the checkpointer, and the stream ends with
   an `approval_required` event.
2. The user answers with `POST /approvals/{thread_id}` (approve or reject).
3. The graph resumes from the saved state: this node runs again, and
   `interrupt` now returns the answer instead of stopping.
   - Approved: research runs as planned.
   - Rejected: research is skipped, the supervisor runs one normal search
     instead, and the answer says research was not approved.

The node does nothing else before `interrupt`, because it runs again from
the start when the graph resumes. If anything goes wrong here, research is
treated as rejected: the node fails closed.
"""

from typing import Any

from langgraph.types import interrupt

from agents.nodes.base import AgentNode, StateUpdate
from agents.state import AgentError, AgentName, AgentState

REJECTED_LIMIT = "Deep research was not approved, so this answer comes from one normal search."


class ApprovalStep(AgentNode):
    name = AgentName.APPROVAL

    async def run(self, state: AgentState) -> StateUpdate:
        decision: Any = interrupt(
            {
                "action": "research",
                "message": (
                    "This question needs deep research across many documents: a search plan, "
                    "several searches in parallel and up to about 8 model calls. Run it?"
                ),
                "question": state.get("question", ""),
            }
        )
        approved = isinstance(decision, dict) and decision.get("approved") is True
        if approved:
            return {
                "approval": "approved",
                "completed": [self.name],
                "steps": [self.step("You approved the deep research.")],
            }
        return self._rejected()

    def _rejected(self) -> StateUpdate:
        return {
            "approval": "rejected",
            # Research counts as done (skipped), so the supervisor moves on.
            "completed": [self.name, AgentName.RESEARCH],
            "limits": [REJECTED_LIMIT],
            "steps": [self.step("Deep research was not approved.")],
        }

    def on_error(self, state: AgentState, error: AgentError) -> StateUpdate:
        return self._rejected()
