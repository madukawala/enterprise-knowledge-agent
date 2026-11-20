"""What the UI remembers: conversations, and for each question what happened.

A `Turn` is one question and its answer. It is built from the streamed
events with `turn.apply(event)`, following the rules in
docs/architecture/09-streaming-events.md:

- `token` events are a preview of the answer;
- `response_start` clears the preview (the validator asked for a rewrite,
  or one model failed half-way and the next started again);
- `response_end` is the final answer, and replaces the preview;
- the stream ends with `response_end` or `error`. Anything else means the
  connection dropped, or the user pressed Stop.

Event types this version does not know (tool calls and memory updates come
with later branches) are kept and listed in the panel, so a newer API never
breaks an older UI.

Everything here is plain data, kept in Streamlit's session state, so the
rules can be tested without Streamlit.
"""

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

Event = dict[str, Any]


class TurnStatus(StrEnum):
    STREAMING = "streaming"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass
class Step:
    """One run of a graph node, as shown in the panel's timeline."""

    node: str
    status: str = "running"  # running, ok, error
    duration_ms: int | None = None
    summary: tuple[str, ...] = ()


@dataclass
class Turn:
    question: str
    status: TurnStatus = TurnStatus.STREAMING
    # The answer text streamed so far, since the last response_start.
    preview: str = ""
    # The response_end event: final text, sources, steps, limits, models.
    final: Event | None = None
    error: str | None = None
    run_id: str | None = None
    request_id: str | None = None
    traced: bool = False
    # The supervisor's plan (the "planned" agent_state event).
    plan: Event | None = None
    steps: list[Step] = field(default_factory=list)
    searches: list[Event] = field(default_factory=list)
    checks: list[Event] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    other: list[Event] = field(default_factory=list)
    # How many times the answer started again, and why (rewrite, model_switch).
    restarts: list[str] = field(default_factory=list)
    tokens: int = 0
    # Every event except tokens, for the "Raw events" view.
    events: list[Event] = field(default_factory=list)

    @property
    def text(self) -> str:
        """What the chat window shows as the answer right now."""
        if self.final is not None:
            return str(self.final.get("text", ""))
        return self.preview

    @property
    def active_node(self) -> str | None:
        running = [step.node for step in self.steps if step.status == "running"]
        return running[-1] if running else None

    def apply(self, event: Event) -> None:
        kind = event.get("type")
        if kind == "token":
            self.preview += str(event.get("text", ""))
            self.tokens += 1
            return

        self.events.append(event)
        match kind:
            case "agent_state":
                self.run_id = event.get("run_id") or self.run_id
                self.traced = bool(event.get("traced", self.traced))
                if event.get("status") == "planned":
                    self.plan = event
            case "node_start":
                self.steps.append(Step(node=str(event.get("node"))))
            case "node_end":
                self._finish_step(event)
            case "retrieval":
                self.searches.append(event)
            case "validation":
                self.checks.append(event)
            case "degraded":
                self.warnings.append(str(event.get("message", "")))
            case "response_start":
                self.preview = ""
                reason = str(event.get("reason", "first"))
                if reason != "first":
                    self.restarts.append(reason)
            case "response_end":
                self.final = event
                self.status = TurnStatus.DONE
            case "error":
                self.error = str(event.get("message") or "Something went wrong.")
                self.status = TurnStatus.FAILED
            case _:
                self.other.append(event)

    def fail(self, message: str) -> None:
        self.error = message
        self.status = TurnStatus.FAILED

    def end_of_stream(self) -> None:
        """The stream closed. Without response_end or error, it was cut short."""
        if self.status is TurnStatus.STREAMING:
            self.fail("The answer was cut short because the connection closed.")

    def stop(self) -> None:
        """The user pressed Stop (or left) while the answer was streaming."""
        if self.status is TurnStatus.STREAMING:
            self.status = TurnStatus.STOPPED
            for step in self.steps:
                if step.status == "running":
                    step.status = "stopped"

    def _finish_step(self, event: Event) -> None:
        node = str(event.get("node"))
        # The latest unfinished run of this node. The supervisor and the
        # response agent can run more than once in one turn.
        for step in reversed(self.steps):
            if step.node == node and step.status == "running":
                break
        else:
            step = Step(node=node)
            self.steps.append(step)
        step.status = str(event.get("status", "ok"))
        step.duration_ms = event.get("duration_ms")
        step.summary = tuple(event.get("summary", ()))


@dataclass
class Conversation:
    role: str
    # Set from the API's X-Thread-ID after the first answer starts, and sent
    # back with every follow-up question.
    thread_id: str | None = None
    turns: list[Turn] = field(default_factory=list)
    key: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def title(self) -> str:
        if not self.turns:
            return "New conversation"
        first = " ".join(self.turns[0].question.split())
        return first if len(first) <= 40 else first[:39].rstrip() + "…"

    @property
    def busy(self) -> bool:
        return any(turn.status is TurnStatus.STREAMING for turn in self.turns)
