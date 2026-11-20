"""Validator: checks the draft answer, then finishes it.

Checks on answers written by the model (fixed answers written in code are
trusted):

- the answer is not empty and not too long;
- every citation marker, such as [3], points to a source retrieved in this
  run. A marker for a source that was never retrieved is a hallucinated
  citation (threat model, LLM09);
- an answer built from sources cites at least one of them;
- a small-talk reply cites nothing, since it had no sources.

If the draft fails, the response agent writes it again once, with the
problems as notes. If the second draft also fails, the user gets the
fallback answer that lists the most relevant sources. So a bad answer is
never shown, and the cost of a retry is bounded (ASSUMPTIONS, trade-offs).

Finishing the answer: only the sources the text actually cites are kept,
and the steps and limits collected during the run are added, which gives
the "How I got this answer" section. The answer text is then added to the
conversation, so follow-up questions can refer to it.

Output guardrails (PII masking, restricted labels, system-prompt leakage,
brand rules) are added here in `feature/guardrails`.
"""

from collections.abc import Iterable

from langchain_core.messages import AIMessage

from agents.nodes.base import AgentNode, StateUpdate
from agents.nodes.response import fallback_answer
from agents.state import (
    CITATION_MARKER,
    MODEL_WRITTEN,
    AgentError,
    AgentName,
    AgentState,
    Answer,
    AnswerKind,
    Validation,
)
from core.logging import get_logger

log = get_logger(__name__)

MAX_ANSWER_CHARS = 6000
# The first draft plus one rewrite.
MAX_RESPONSE_ATTEMPTS = 2


def check(answer: Answer, *, sources: int) -> list[str]:
    """The problems with a model-written answer. Empty means it passed."""
    if answer.kind not in MODEL_WRITTEN:
        return []
    issues = []
    text = answer.text.strip()
    if not text:
        issues.append("The answer is empty.")
    if len(text) > MAX_ANSWER_CHARS:
        issues.append(f"The answer is longer than {MAX_ANSWER_CHARS} characters.")

    cited = {int(number) for number in CITATION_MARKER.findall(text)}
    if answer.kind is AnswerKind.CONVERSATION:
        if cited:
            issues.append("A reply without sources must not use citation numbers.")
        return issues

    unknown = sorted(number for number in cited if not 1 <= number <= sources)
    if unknown:
        listed = ", ".join(f"[{number}]" for number in unknown)
        issues.append(f"It cites {listed}, but only sources [1] to [{sources}] were given.")
    if not cited:
        issues.append("It does not cite any source. Add the source number after each fact.")
    return issues


def finish(answer: Answer, state: AgentState) -> Answer:
    """Keep only the cited sources and add the steps and limits of this run."""
    cited = {int(number) for number in CITATION_MARKER.findall(answer.text)}
    citations = (
        tuple(item for item in answer.citations if item.number in cited)
        if answer.kind is AnswerKind.ANSWERED
        else answer.citations
    )
    errors = state.get("errors", {}).values()
    limits = list(dict.fromkeys([*state.get("limits", []), *_error_limits(errors, answer)]))
    return answer.model_copy(
        update={
            "citations": citations,
            "steps": tuple(step.summary for step in state.get("steps", [])),
            "limits": tuple(limits),
        }
    )


def _error_limits(errors: Iterable[AgentError], answer: Answer) -> list[str]:
    """What failed, in plain words. A search failure is left out when the
    answer itself already says document search is unavailable."""
    return [
        error.message
        for error in errors
        if not (answer.kind is AnswerKind.UNAVAILABLE and error.agent is AgentName.RETRIEVAL)
    ]


class Validator(AgentNode):
    name = AgentName.VALIDATOR

    async def run(self, state: AgentState) -> StateUpdate:
        draft = state.get("draft")
        attempt = state.get("response_attempts", 1)
        if draft is None:
            # Should not happen; answer safely rather than show nothing.
            draft = fallback_answer(state.get("hits", []))

        issues = check(draft, sources=len(state.get("hits", [])))
        validation = Validation(passed=not issues, issues=tuple(issues), attempt=attempt)

        if issues and attempt < MAX_RESPONSE_ATTEMPTS:
            self.context.metrics.validations.labels(result="regenerate").inc()
            log.info("answer_rejected", attempt=attempt, issues=len(issues))
            return {
                "validation": validation,
                "next_agent": AgentName.RESPONSE,
                "steps": [
                    self.step("The first draft failed the answer checks, so it was written again.")
                ],
            }

        if issues:
            self.context.metrics.validations.labels(result="fallback").inc()
            log.warning("answer_replaced_by_fallback", attempt=attempt, issues=len(issues))
            draft = fallback_answer(state.get("hits", []))
            step = self.step(
                "The answer failed the checks twice, so the most relevant "
                "sources are listed instead."
            )
        else:
            self.context.metrics.validations.labels(result="passed").inc()
            step = self.step(_passed_summary(draft))

        state_with_step: AgentState = {**state, "steps": [*state.get("steps", []), step]}
        answer = finish(draft, state_with_step)
        return {
            "validation": validation,
            "next_agent": None,
            "answer": answer,
            "steps": [step],
            "messages": [AIMessage(answer.text, name="assistant")],
        }

    def on_error(self, state: AgentState, error: AgentError) -> StateUpdate:
        answer = fallback_answer(state.get("hits", []))
        return {
            "next_agent": None,
            "answer": answer,
            "messages": [AIMessage(answer.text, name="assistant")],
        }


def _passed_summary(answer: Answer) -> str:
    if answer.kind is AnswerKind.ANSWERED:
        cited = len(set(CITATION_MARKER.findall(answer.text)))
        sources = "source was" if cited == 1 else f"{cited} sources were"
        return f"Checked that the cited {sources} retrieved in this run: passed."
    return "Checked the answer: passed."
