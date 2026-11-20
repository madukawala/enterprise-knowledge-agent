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
from html import escape

from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, SystemMessage

from agents.prompts import load
from rag.retrieval import SearchHit


def system_message(task: str, *, today: date) -> SystemMessage:
    """The shared rules (system.md) followed by the task of one node."""
    rules = load("system").substitute(today=today.isoformat())
    return SystemMessage(rules + "\n\n" + load(task).substitute(today=today.isoformat()))


def history_block(messages: Sequence[AnyMessage], *, limit: int) -> str:
    """The last `limit` messages before the current question, as plain text."""
    earlier = list(messages[:-1])[-limit:] if limit else []
    lines: list[str] = []
    for message in earlier:
        if isinstance(message, HumanMessage):
            lines.append(f"User: {message.text}")
        elif isinstance(message, AIMessage):
            lines.append(f"Assistant: {message.text}")
    if not lines:
        return ""
    return "<conversation>\n" + "\n".join(lines) + "\n</conversation>\n\n"


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
) -> HumanMessage:
    """One user message: earlier conversation, sources, notes, then the question."""
    text = history_block(messages, limit=history)
    if sources:
        text += sources_block(sources)
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
) -> list[BaseMessage]:
    return [
        system_message(task, today=today),
        user_message(question, messages, history=history, sources=sources, notes=notes),
    ]
