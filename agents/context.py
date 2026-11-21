"""Builds the messages sent to the language model.

The rules live in the system message. Everything that comes from users or
documents (the conversation, the question, the retrieved chunks) goes into
the user message, wrapped in tags, and the system message says that text
inside those tags is data, never instructions. This is the first layer of
the prompt-injection defence; `feature/guardrails` adds cleaning and
scanning of the same text.
"""

from collections.abc import Sequence
from datetime import date
from functools import cache
from html import escape

from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, SystemMessage

from agents.prompts import load
from rag.retrieval import SearchHit
from security.guardrails.output import PromptFingerprint


def system_message(task: str, *, today: date, **values: object) -> SystemMessage:
    """The shared rules (system.md) followed by the task of one node.

    `values` fill the task's own placeholders, such as `$max_batches`.
    """
    rules = load("system").substitute(today=today.isoformat())
    task_text = load(task).substitute(today=today.isoformat(), **values)
    return SystemMessage(rules + "\n\n" + task_text)


def history_block(messages: Sequence[AnyMessage], *, limit: int, summary: str = "") -> str:
    """The last `limit` messages before the current question, as plain text.

    For a long thread, `summary` stands in for the turns before those.
    """
    earlier = list(messages[:-1])[-limit:] if limit else []
    head = f"<summary>\n{escape(summary)}\n</summary>\n\n" if summary else ""
    lines: list[str] = []
    for message in earlier:
        if isinstance(message, HumanMessage):
            lines.append(f"User: {message.text}")
        elif isinstance(message, AIMessage):
            lines.append(f"Assistant: {message.text}")
    if not lines:
        return head
    return head + "<conversation>\n" + "\n".join(lines) + "\n</conversation>\n\n"


def _attribute(name: str, value: str) -> str:
    # Escaped, so a title such as `x" id="9` cannot add a fake attribute.
    return f'{name}="{escape(value, quote=True)}"'


def sources_block(hits: Sequence[SearchHit]) -> str:
    """Number each chunk; the answer cites these numbers, e.g. [2]."""
    parts = []
    for number, hit in enumerate(hits, start=1):
        chunk = hit.chunk
        attributes = " ".join(
            _attribute(name, value)
            for name, value in (
                ("id", str(number)),
                ("doc_id", chunk.doc_id),
                ("title", chunk.title),
                ("section", chunk.section),
                ("date", chunk.created_date.isoformat()),
            )
        )
        parts.append(f"<source {attributes}>\n{chunk.text.strip()}\n</source>")
    return "<sources>\n" + "\n".join(parts) + "\n</sources>\n\n"


def user_message(
    question: str,
    messages: Sequence[AnyMessage],
    *,
    history: int,
    sources: Sequence[SearchHit] = (),
    notes: Sequence[str] = (),
    findings: Sequence[str] = (),
    overview: str = "",
    summary: str = "",
    memory: Sequence[str] = (),
    tool_results: Sequence[str] = (),
) -> HumanMessage:
    """One user message: conversation, overview, sources, findings, notes, then the question.

    Findings are written by research sub-agents from document text, so they
    are wrapped in tags and treated as data, like the sources.
    """
    text = history_block(messages, limit=history, summary=summary)
    if memory:
        text += "<memory>\n" + "\n".join(f"- {escape(m)}" for m in memory) + "\n</memory>\n\n"
    if overview:
        text += f"<collections>\n{overview}\n</collections>\n\n"
    if sources:
        text += sources_block(sources)
    if tool_results:
        text += (
            "<tool_results>\n"
            + "\n".join(f"- {escape(r)}" for r in tool_results)
            + "\n</tool_results>\n\n"
        )
    if findings:
        text += "<findings>\n" + "\n".join(f"- {escape(f)}" for f in findings) + "\n</findings>\n\n"
    if notes:
        text += "Notes from the system:\n" + "\n".join(f"- {note}" for note in notes) + "\n\n"
    text += f"<question>\n{question}\n</question>"
    return HumanMessage(text)


def prompt(
    task: str,
    question: str,
    messages: Sequence[AnyMessage],
    *,
    today: date,
    history: int,
    sources: Sequence[SearchHit] = (),
    notes: Sequence[str] = (),
    findings: Sequence[str] = (),
    overview: str = "",
    values: dict[str, object] | None = None,
    summary: str = "",
    memory: Sequence[str] = (),
    tool_results: Sequence[str] = (),
) -> list[BaseMessage]:
    return [
        system_message(task, today=today, **(values or {})),
        user_message(
            question,
            messages,
            history=history,
            sources=sources,
            notes=notes,
            findings=findings,
            overview=overview,
            summary=summary,
            memory=memory,
            tool_results=tool_results,
        ),
    ]


# Every prompt the models are given. The validator rejects an answer that
# repeats eight words in a row from any of them (system prompt leakage).
PROMPT_NAMES = (
    "system",
    "supervisor",
    "search_plan",
    "answer",
    "conversation",
    "research_plan",
    "research_batch",
    "summary",
    "tools",
)


@cache
def prompt_fingerprint() -> PromptFingerprint:
    # The example code in research_plan.md is left out: an answer quoting a
    # search query is not a leak.
    return PromptFingerprint.of(load(name).template.split("```")[0] for name in PROMPT_NAMES)
