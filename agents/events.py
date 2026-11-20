"""The live events of one agent run, as the activity panel sees them.

`AgentService.stream()` yields these while the graph runs, and the API
sends each one as a server-sent event (`POST /chat`). The design and the
full table are in docs/architecture/09-streaming-events.md.

One run always looks like this:

    agent_state (accepted)
    node_start input_guard ... node_end input_guard    with a validation event
    node_start supervisor  ... node_end supervisor     with agent_state (planned)
    node_start retrieval   ... node_end retrieval      with a retrieval event
    ...
    node_start response, response_start, token, token, ..., node_end response
    node_start validator   ... node_end validator      with a validation event
    response_end

Two rules keep the client simple:

- `token` events are a preview. A `response_start` means "clear what you
  have shown so far": the validator asked for a rewrite, or one model
  failed half-way and the next one started again.
- `response_end` carries the final, checked answer. The client shows that
  text, whatever the tokens said.

The models only hold what the panel needs. They never carry prompts,
stack traces or the text of documents the user did not get as a source.
"""

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agents.state import AgentName, AnswerKind, Intent, LLMCall
from rag.retrieval import Citation, RerankStatus
from rag.schema import Collection


class EventType(StrEnum):
    AGENT_STATE = "agent_state"
    NODE_START = "node_start"
    NODE_END = "node_end"
    RETRIEVAL = "retrieval"
    VALIDATION = "validation"
    DEGRADED = "degraded"
    RESPONSE_START = "response_start"
    # An event name, not a password; both security linters are told so.
    TOKEN = "token"  # noqa: S105 # nosec B105
    RESPONSE_END = "response_end"
    ERROR = "error"


class _Event(BaseModel):
    model_config = ConfigDict(frozen=True)


class AgentStateEvent(_Event):
    """The run was accepted, and later, the supervisor's plan."""

    type: Literal[EventType.AGENT_STATE] = EventType.AGENT_STATE
    status: Literal["accepted", "planned"]
    run_id: str
    thread_id: str
    # True when the run is sent to LangSmith; `run_id` is then the trace's ID.
    traced: bool = False
    intent: Intent | None = None
    plan: tuple[str, ...] = ()
    agents: tuple[AgentName, ...] = ()


class NodeStartEvent(_Event):
    type: Literal[EventType.NODE_START] = EventType.NODE_START
    node: AgentName


class NodeEndEvent(_Event):
    type: Literal[EventType.NODE_END] = EventType.NODE_END
    node: AgentName
    duration_ms: int
    # "error" when the node failed and its failure was contained (ADR-0006).
    status: Literal["ok", "error"] = "ok"
    # The node's lines for "How I got this answer".
    summary: tuple[str, ...] = ()


class RetrievalEvent(_Event):
    """What the retrieval agent searched and what came back."""

    type: Literal[EventType.RETRIEVAL] = EventType.RETRIEVAL
    query: str
    collections: tuple[Collection, ...]
    filters: dict[str, Any]
    failed_collections: tuple[Collection, ...] = ()
    candidates: int
    hits: int
    rerank: RerankStatus
    alpha: float
    took_ms: int
    relaxed: bool = False


class ValidationEvent(_Event):
    """The input guard's check (stage "input") or the validator's (stage "output")."""

    type: Literal[EventType.VALIDATION] = EventType.VALIDATION
    stage: Literal["input", "output"]
    passed: bool
    issues: tuple[str, ...] = ()
    attempt: int = 1


class DegradedEvent(_Event):
    """Something did not work, and the run carried on without it."""

    type: Literal[EventType.DEGRADED] = EventType.DEGRADED
    dependency: Literal["documents", "language_model"]
    message: str


class ResponseStartEvent(_Event):
    """The answer starts. The client clears any tokens shown before this."""

    type: Literal[EventType.RESPONSE_START] = EventType.RESPONSE_START
    attempt: int
    # first: the first draft. rewrite: the validator rejected the draft.
    # model_switch: a model failed while writing and the next one took over.
    reason: Literal["first", "rewrite", "model_switch"] = "first"


class TokenEvent(_Event):
    type: Literal[EventType.TOKEN] = EventType.TOKEN
    text: str


class SourceRef(_Event):
    number: int
    doc_id: str
    title: str
    section: str
    label: str

    @classmethod
    def of(cls, number: int, citation: Citation) -> "SourceRef":
        return cls(
            number=number,
            doc_id=citation.doc_id,
            title=citation.title,
            section=citation.section,
            label=citation.label,
        )


class ResponseEndEvent(_Event):
    """The final, validated answer and what it cost."""

    type: Literal[EventType.RESPONSE_END] = EventType.RESPONSE_END
    kind: AnswerKind
    text: str
    # The whole answer as the user sees it: text, sources, steps, limits.
    markdown: str
    citations: tuple[SourceRef, ...] = ()
    steps: tuple[str, ...] = ()
    limits: tuple[str, ...] = ()
    written_by: str | None = None
    llm_calls: tuple[LLMCall, ...] = ()
    duration_ms: int


class ErrorEvent(_Event):
    """The run stopped. `message` is safe to show; details are in the log."""

    type: Literal[EventType.ERROR] = EventType.ERROR
    code: str
    message: str


AgentEvent = Annotated[
    AgentStateEvent
    | NodeStartEvent
    | NodeEndEvent
    | RetrievalEvent
    | ValidationEvent
    | DegradedEvent
    | ResponseStartEvent
    | TokenEvent
    | ResponseEndEvent
    | ErrorEvent,
    Field(discriminator="type"),
]
