"""Memory writer: the last node of every run (ADR-0008).

After the answer is final, it decides what to remember:

- **Long-term memory** (when the user is known and memory is on): an
  answered question is saved as one masked line with the documents it
  cited, and the departments it searched are counted into the user's
  profile. Refusals, small talk and failed answers are not saved.
- **A running summary** of a long thread: when the thread has more than
  `summary_after_messages` messages, and at least four new ones since the
  last summary, one model call summarises the older turns. Later prompts
  show the summary instead of those turns, so they stay small.

What was saved is listed in `memory_saved`, which the stream sends as a
`memory` event, so memory updates are visible in the activity panel.

Memory never fails a run: any error here is logged and the answer stands.
"""

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from agents.context import prompt
from agents.nodes.base import AgentNode, StateUpdate
from agents.state import AgentError, AgentName, AgentState, AnswerKind
from core.errors import LLMUnavailableError
from core.logging import get_logger
from core.pii import mask_text

log = get_logger(__name__)

# The newest messages always stay in full; the summary covers the rest.
KEEP_RECENT = 6
# Summarise again only after this many new messages.
RESUMMARISE_EVERY = 4
MAX_SUMMARY_CHARS = 1_200


def _transcript(messages: list[AnyMessage]) -> str:
    lines = []
    for message in messages:
        if isinstance(message, HumanMessage):
            lines.append(f"User: {message.text}")
        elif isinstance(message, AIMessage):
            lines.append(f"Assistant: {message.text}")
    return "\n".join(lines)


class MemoryWriter(AgentNode):
    name = AgentName.MEMORY

    async def run(self, state: AgentState) -> StateUpdate:
        saved: list[str] = []
        update: StateUpdate = {}

        answer = state.get("answer")
        user = state.get("user")
        memory = self.context.memory
        answered = answer is not None and answer.kind is AnswerKind.ANSWERED
        if memory is not None and user and answer is not None and answered:
            documents = list(dict.fromkeys(c.citation.doc_id for c in answer.citations))
            search = state.get("search")
            departments = [d.value for d in search.filters.departments] if search else []
            saved += await memory.remember(user, state["question"], documents, departments)

        summary = await self._summarise(state)
        if summary is not None:
            update.update(summary)
            saved.append("Summarised the earlier part of this conversation.")

        if saved:
            update["memory_saved"] = saved
            update["steps"] = [self.step("Memory: " + " ".join(saved))]
        return update

    async def _summarise(self, state: AgentState) -> StateUpdate | None:
        messages = state.get("messages", [])
        limit = self.context.summary_after_messages
        covers = state.get("summary_covers", 0)
        older = len(messages) - KEEP_RECENT
        if len(messages) <= limit or older - covers < RESUMMARISE_EVERY:
            return None
        earlier = _transcript(list(messages[:older]))
        previous = state.get("summary") or ""
        question = (
            (f"Earlier summary: {previous}\n\n" if previous else "")
            + "Conversation to summarise:\n"
            + earlier
        )
        try:
            text, call = await self.context.gateway.text(
                prompt("summary", question, [], today=self.context.today(), history=0),
                purpose="summary",
            )
        except LLMUnavailableError:
            return None
        return {
            "summary": mask_text(text.strip())[:MAX_SUMMARY_CHARS],
            "summary_covers": older,
            "llm_calls": [call],
        }

    def on_error(self, state: AgentState, error: AgentError) -> StateUpdate:
        # The answer is already final; a memory problem must not change it.
        log.warning("memory_write_failed", kind=error.kind.value)
        return {}
