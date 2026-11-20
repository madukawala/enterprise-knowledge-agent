"""Response agent: writes the answer, in the bank's voice, with citations.

Most answers are written by the model from the numbered sources, with a
marker such as [2] after each fact. Some situations get a fixed answer
written in code instead, because a model adds nothing there and could
only make it worse:

- The input guard blocked the question: its refusal message.
- Out of scope (e.g. investment advice): a standard polite refusal.
- Documents were needed but search failed: "document search is unavailable".
- Documents were needed but nothing matched: "I couldn't find this in the
  documents you can access".
- The model wrote NOT_FOUND: the same "couldn't find" message.
- The model could not be reached: the most relevant sources, listed, so
  the user still gets somewhere.

The "couldn't find" message never hints that documents above the user's
level exist.

On a second attempt (the validator rejected the first draft), the
validator's findings are passed to the model as notes.
"""

from collections.abc import Sequence

from agents.context import prompt
from agents.nodes.base import AgentNode, StateUpdate
from agents.state import (
    MODEL_WRITTEN,
    AgentError,
    AgentName,
    AgentState,
    Answer,
    AnswerKind,
    ErrorKind,
    Intent,
    NumberedCitation,
)
from core.errors import LLMUnavailableError
from rag.retrieval import SearchHit

# What the model replies when the sources do not answer the question (answer.md).
NOT_FOUND_REPLY = "NOT_FOUND"

OUT_OF_SCOPE_TEXT = (
    "I can only help with questions about Helix Bank's internal knowledge: policies, "
    "architecture, runbooks, incident reports, product specifications and meeting notes. "
    "I can't help with this request. For financial or investment questions, please speak "
    "to the relevant team."
)
NOT_FOUND_TEXT = (
    "I couldn't find anything in the documents you can access that answers this. "
    "Try rephrasing the question, or ask about a specific system, policy or incident."
)
UNAVAILABLE_TEXT = (
    "Document search is unavailable right now, so I can't answer from the bank's documents. "
    "Please try again in a few minutes."
)
FALLBACK_TEXT = (
    "I couldn't write a full answer just now. These documents look most relevant to your question:"
)
FALLBACK_NO_SOURCES_TEXT = "I couldn't write an answer just now. Please try again in a few minutes."

# How many sources the fallback answer lists.
FALLBACK_SOURCES = 5


def numbered_citations(hits: Sequence[SearchHit]) -> tuple[NumberedCitation, ...]:
    return tuple(
        NumberedCitation(number=number, citation=hit.citation)
        for number, hit in enumerate(hits, start=1)
    )


def fallback_answer(hits: Sequence[SearchHit]) -> Answer:
    """Used when no usable answer could be written: list the best sources instead."""
    if not hits:
        return Answer(kind=AnswerKind.FALLBACK, text=FALLBACK_NO_SOURCES_TEXT)
    citations = _one_per_document(numbered_citations(hits))[:FALLBACK_SOURCES]
    lines = "\n".join(f"- [{item.number}] {item.citation.label}" for item in citations)
    return Answer(kind=AnswerKind.FALLBACK, text=f"{FALLBACK_TEXT}\n\n{lines}", citations=citations)


def _one_per_document(citations: Sequence[NumberedCitation]) -> tuple[NumberedCitation, ...]:
    seen: set[str] = set()
    kept = []
    for item in citations:
        if item.citation.doc_id not in seen:
            seen.add(item.citation.doc_id)
            kept.append(item)
    return tuple(kept)


class ResponseAgent(AgentNode):
    name = AgentName.RESPONSE

    async def run(self, state: AgentState) -> StateUpdate:
        attempt = state.get("response_attempts", 0) + 1
        update: StateUpdate = {"response_attempts": attempt}

        fixed = self._fixed_answer(state)
        if fixed is not None:
            update["draft"] = fixed
            return update

        plan = state.get("plan")
        hits = state.get("hits", [])
        conversation = plan is not None and plan.intent is Intent.CONVERSATION
        notes = list(state.get("limits", []))
        validation = state.get("validation")
        if validation is not None and not validation.passed:
            notes.append("Your previous draft was rejected: " + " ".join(validation.issues))

        try:
            text, call = await self.context.gateway.text(
                prompt(
                    "conversation" if conversation else "answer",
                    state["question"],
                    state.get("messages", []),
                    today=self.context.today(),
                    history=self.context.history_messages,
                    sources=() if conversation else hits,
                    notes=notes,
                ),
                purpose="answer",
            )
        except LLMUnavailableError:
            update["errors"] = {
                self.name.value: self.failure(
                    ErrorKind.UNAVAILABLE, "The language model could not write the answer."
                )
            }
            update["draft"] = fallback_answer(hits)
            update["steps"] = [
                self.step(
                    "Could not reach the language model to write the "
                    "answer, so listed the most relevant sources."
                )
            ]
            return update

        written_by = f"{call.provider} / {call.model}"
        if text.strip().strip(".").upper() == NOT_FOUND_REPLY:
            draft = Answer(kind=AnswerKind.NOT_FOUND, text=NOT_FOUND_TEXT, written_by=written_by)
        elif conversation:
            draft = Answer(kind=AnswerKind.CONVERSATION, text=text, written_by=written_by)
        else:
            draft = Answer(
                kind=AnswerKind.ANSWERED,
                text=text,
                citations=numbered_citations(hits),
                written_by=written_by,
            )
        update["draft"] = draft
        update["llm_calls"] = [call]
        if draft.kind in MODEL_WRITTEN:
            sources = f" from {len(hits)} sources" if hits and not conversation else ""
            update["steps"] = [self.step(f"Wrote the answer{sources} with {written_by}.")]
        return update

    def _fixed_answer(self, state: AgentState) -> Answer | None:
        """The answer for situations that need no model (see the module docstring)."""
        if reason := state.get("blocked_reason"):
            return Answer(kind=AnswerKind.REFUSED, text=reason)
        plan = state.get("plan")
        if plan is not None and plan.intent is Intent.OUT_OF_SCOPE:
            return Answer(kind=AnswerKind.OUT_OF_SCOPE, text=OUT_OF_SCOPE_TEXT)
        needs_documents = plan is None or AgentName.RETRIEVAL in plan.agents
        if needs_documents and not state.get("hits"):
            if AgentName.RETRIEVAL.value in state.get("errors", {}):
                return Answer(kind=AnswerKind.UNAVAILABLE, text=UNAVAILABLE_TEXT)
            return Answer(kind=AnswerKind.NOT_FOUND, text=NOT_FOUND_TEXT)
        return None

    def on_error(self, state: AgentState, error: AgentError) -> StateUpdate:
        return {
            "response_attempts": state.get("response_attempts", 0) + 1,
            "draft": fallback_answer(state.get("hits", [])),
        }
