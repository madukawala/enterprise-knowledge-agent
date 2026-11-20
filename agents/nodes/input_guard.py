"""Input guard: the first node of every run.

It does two jobs:

- **Starts a new question.** Clears everything collected for the previous
  question in this thread (plan, search results, errors, steps), so nothing
  from an earlier turn leaks into this one. The conversation itself is kept.
- **Checks the question.** It must be a non-empty text message within the
  length limit, and the role must be set by the caller. Control characters
  are removed. A question that fails is answered with a short refusal and
  no model is called.

The prompt-injection scan (LLM Guard and rules) is added here in
`feature/guardrails`. Until then, the rules in the system prompt and the
role checks in code are what stop injection attempts (ADR-0007).

If this node itself fails, the question is refused: it fails closed.
"""

import unicodedata

from langchain_core.messages import HumanMessage

from agents.nodes.base import AgentNode, StateUpdate
from agents.state import AgentError, AgentName, AgentState
from security.roles import Role

# Longer than this is a pasted document, not a question. It also bounds
# the prompt size (and so the cost) of one question.
MAX_QUESTION_CHARS = 2000

REFUSAL_EMPTY = "Please type a question."
REFUSAL_TOO_LONG = (
    f"Your question is too long. Please keep it under {MAX_QUESTION_CHARS} characters, "
    "or ask about one part at a time."
)
REFUSAL_INTERNAL = "I couldn't check your question just now. Please try again."


def clean_question(text: str) -> str:
    """Remove control and invisible formatting characters; keep new lines and tabs."""
    kept = (
        char
        for char in text
        if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Co"}
    )
    return "".join(kept).strip()


def _new_question() -> StateUpdate:
    """Every per-question field, reset. `None` clears a collected list."""
    return {
        "blocked_reason": None,
        "plan": None,
        "next_agent": None,
        "search_plan": None,
        "search": None,
        "hits": [],
        "relax_filters": False,
        "retrieval_attempts": 0,
        "draft": None,
        "response_attempts": 0,
        "validation": None,
        "answer": None,
        "errors": None,
        "steps": None,
        "limits": None,
        "completed": None,
        "llm_calls": None,
    }


class InputGuard(AgentNode):
    name = AgentName.INPUT_GUARD

    async def run(self, state: AgentState) -> StateUpdate:
        update = _new_question()
        messages = state.get("messages", [])
        last = messages[-1] if messages else None
        question = clean_question(last.text) if isinstance(last, HumanMessage) else ""

        if not isinstance(state.get("role"), Role):
            # A programming error in the caller, not something the user did.
            # Refuse rather than guess a role.
            reason = REFUSAL_INTERNAL
        elif not question:
            reason = REFUSAL_EMPTY
        elif len(question) > MAX_QUESTION_CHARS:
            reason = REFUSAL_TOO_LONG
        else:
            update["question"] = question
            return update

        update["question"] = question[:MAX_QUESTION_CHARS]
        update["blocked_reason"] = reason
        return update

    def on_error(self, state: AgentState, error: AgentError) -> StateUpdate:
        update = _new_question()
        update["question"] = ""
        update["blocked_reason"] = REFUSAL_INTERNAL
        return update
