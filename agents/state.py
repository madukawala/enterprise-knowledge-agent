"""The shared state every node reads and writes, and the models inside it.

LangGraph passes one `AgentState` from node to node. A node never changes
the state in place: it returns the fields it wants to update, and LangGraph
merges them. Most fields are simply replaced. The ones with a reducer
(`Annotated[..., reducer]`) are combined instead:

- `messages` keeps the whole conversation (LangGraph's `add_messages`).
- `errors`, `steps`, `limits`, `completed` and `llm_calls` collect entries
  during one question. The input guard clears them at the start of every
  new question by sending `None` (see `reset_or_extend`).

Failure containment (ADR-0006): a node that fails writes an `AgentError`
to `errors[<its name>]` instead of raising. The supervisor reads that slot
and decides to retry, skip or answer with what it has, and the answer says
what was missing.
"""

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field

from rag.filters import SearchFilters
from rag.retrieval import Citation, RerankStatus, SearchHit
from rag.schema import Collection
from security.roles import Role

# ---------------------------------------------------------------
# Names
# ---------------------------------------------------------------


class AgentName(StrEnum):
    """Node names. They appear in the graph diagram, logs, metrics and traces."""

    INPUT_GUARD = "input_guard"
    SUPERVISOR = "supervisor"
    RETRIEVAL = "retrieval"
    RESEARCH = "research"
    RESPONSE = "response"
    VALIDATOR = "validator"
    MEMORY = "memory"
    TOOLS = "tools"
    APPROVAL = "approval"


class Intent(StrEnum):
    """What the user wants, as decided by the supervisor."""

    DOCUMENT_QUESTION = "document_question"
    """A question the bank's documents should answer."""

    RESEARCH = "research"
    """A summary across many documents, e.g. "payment outages over the last year"."""

    ENTERPRISE_DATA = "enterprise_data"
    """Live enterprise data: people, service owners, incident records."""

    CONVERSATION = "conversation"
    """Greetings, thanks, or questions about the assistant itself."""

    OUT_OF_SCOPE = "out_of_scope"
    """Not about the bank's internal knowledge, e.g. investment advice."""


# ---------------------------------------------------------------
# Errors, steps and model calls
# ---------------------------------------------------------------


class ErrorKind(StrEnum):
    UNAVAILABLE = "unavailable"  # a service (model, Pinecone) did not answer
    BAD_OUTPUT = "bad_output"  # the model answered, but not in a usable form
    INTERNAL = "internal"  # a bug: logged with its traceback


class AgentError(BaseModel):
    """One node's failure, kept in `errors[<node>]`."""

    model_config = ConfigDict(frozen=True)

    agent: AgentName
    kind: ErrorKind
    # Safe to show to the user: no stack traces, keys or internal details.
    message: str
    retryable: bool = False


class Step(BaseModel):
    """One line of the "How I got this answer" section."""

    model_config = ConfigDict(frozen=True)

    agent: AgentName
    summary: str


class LLMCall(BaseModel):
    """One language model call: which model answered and what it cost."""

    model_config = ConfigDict(frozen=True)

    purpose: str  # supervisor, search_plan, answer
    provider: str  # gemini, openrouter
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    # True when an earlier provider failed and this one took over.
    fallback: bool = False


# ---------------------------------------------------------------
# Plan and search
# ---------------------------------------------------------------


class Plan(BaseModel):
    """The supervisor's decision for one question."""

    model_config = ConfigDict(frozen=True)

    intent: Intent
    # Short steps in plain words, shown in the activity panel.
    steps: tuple[str, ...]
    # The agents to run, in order, before the answer is written.
    agents: tuple[AgentName, ...]
    reason: str = ""


class SearchPlan(BaseModel):
    """The retrieval agent's search: where to look and for what.

    Written by the language model, so every field is checked before it is
    used. There is no role or access-level field on purpose: the access
    level always comes from the user's role (ADR-0007).
    """

    model_config = ConfigDict(frozen=True)

    query: str = Field(description="The question rewritten as a standalone search query.")
    collections: list[Collection] = Field(
        description="The document collections that could hold the answer (one or more)."
    )
    departments: list[str] = Field(
        default_factory=list,
        description="Only if the question names a department: payments, cards, "
        "core-banking, security or hr.",
    )
    date_from: str | None = Field(
        default=None, description="Only if the question limits dates: first day, YYYY-MM-DD."
    )
    date_to: str | None = Field(
        default=None, description="Only if the question limits dates: last day, YYYY-MM-DD."
    )


class ToolRecord(BaseModel):
    """One enterprise data tool call (the MCP server)."""

    model_config = ConfigDict(frozen=True)

    tool: str
    arguments: dict[str, Any]
    ok: bool
    items: int = 0
    error: str | None = None
    duration_ms: int = 0
    # The result as compact JSON, for the answer prompt (empty when it failed).
    text: str = ""


class BatchReport(BaseModel):
    """What one research sub-agent did with its batch."""

    model_config = ConfigDict(frozen=True)

    label: str
    depth: int
    hits: int
    findings: int
    # Set when the batch failed; the other batches still count.
    error: str | None = None
    # True when it had too much material and split into smaller batches.
    split: bool = False


class ResearchSummary(BaseModel):
    """What the research agent did, for the activity panel and traces."""

    model_config = ConfigDict(frozen=True)

    # "sandbox": the model's Python plan, run in the sandbox.
    # "fallback": the fixed plan (last twelve months by quarter).
    plan_source: str
    plan_code: str | None = None
    batches: tuple[BatchReport, ...]
    max_depth: int
    sources: int
    tokens_used: int
    stopped_by_budget: bool = False


class SearchSummary(BaseModel):
    """What the retrieval agent did, for the answer, the activity panel and traces."""

    model_config = ConfigDict(frozen=True)

    query: str
    collections: tuple[Collection, ...]
    filters: SearchFilters
    failed_collections: tuple[Collection, ...] = ()
    candidates: int
    hits: int
    rerank: RerankStatus
    alpha: float
    took_ms: int
    # True when the optional filters were dropped on a second try.
    relaxed: bool = False


# ---------------------------------------------------------------
# The answer
# ---------------------------------------------------------------


class AnswerKind(StrEnum):
    ANSWERED = "answered"  # written by the model from the sources
    CONVERSATION = "conversation"  # small talk, no sources needed
    NOT_FOUND = "not_found"  # nothing the user may read answers it
    UNAVAILABLE = "unavailable"  # document search did not work
    REFUSED = "refused"  # the input guard blocked the question
    OUT_OF_SCOPE = "out_of_scope"  # not something the assistant helps with
    FALLBACK = "fallback"  # no usable answer could be written; sources listed


# Answers of these kinds are written by the language model, so the
# validator checks them. The others are fixed texts written in code.
MODEL_WRITTEN = frozenset({AnswerKind.ANSWERED, AnswerKind.CONVERSATION})

CITATION_MARKER = re.compile(r"\[(\d{1,3})\]")


class NumberedCitation(BaseModel):
    model_config = ConfigDict(frozen=True)

    number: int
    citation: Citation


class Answer(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: AnswerKind
    # The answer itself. Facts carry markers such as [1] that point to `citations`.
    text: str
    citations: tuple[NumberedCitation, ...] = ()
    # "How I got this answer": built from the state, not from the model's
    # hidden reasoning, so it shows what really happened.
    steps: tuple[str, ...] = ()
    # What was missing or went wrong, e.g. a collection that did not answer.
    limits: tuple[str, ...] = ()
    # Which model wrote it, e.g. "gemini / gemini-3.6-flash".
    written_by: str | None = None

    def to_markdown(self) -> str:
        """The answer as the user sees it: text, sources, then how it was made."""
        parts = [self.text.strip()]
        if self.citations:
            lines = [f"[{item.number}] {item.citation.label}" for item in self.citations]
            parts.append("**Sources**\n\n" + "\n".join(f"- {line}" for line in lines))
        if self.steps:
            parts.append(
                "**How I got this answer**\n\n" + "\n".join(f"- {step}" for step in self.steps)
            )
        if self.limits:
            parts.append("**Limits**\n\n" + "\n".join(f"- {limit}" for limit in self.limits))
        return "\n\n".join(parts)


class Validation(BaseModel):
    model_config = ConfigDict(frozen=True)

    passed: bool
    issues: tuple[str, ...] = ()
    attempt: int = 1


# ---------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------


class Fresh(list[Any]):
    """A list update that replaces the collected list instead of adding to it.

    The input guard uses it to clear the previous question's steps and add
    its own in one update.
    """


def reset_or_extend(current: list[Any] | None, update: list[Any] | None) -> list[Any]:
    """Append to a list, or clear it when the update is `None`.

    Appending (rather than replacing) means nodes that run side by side,
    such as research sub-agents later, never overwrite each other's entries.
    A `Fresh` list replaces what was collected.
    """
    if update is None:
        return []
    if isinstance(update, Fresh):
        return list(update)
    return [*(current or []), *update]


def reset_or_merge(
    current: Mapping[str, AgentError] | None, update: Mapping[str, AgentError | None] | None
) -> dict[str, AgentError]:
    """Merge error slots by agent name.

    An update of `None` clears every slot (a new question). A slot set to
    `None` clears just that slot (an agent that failed, then succeeded on
    a retry).
    """
    if update is None:
        return {}
    merged = {**(current or {}), **update}
    return {agent: error for agent, error in merged.items() if error is not None}


# ---------------------------------------------------------------
# The state
# ---------------------------------------------------------------


class AgentState(TypedDict, total=False):
    # The conversation. With a checkpointer it survives between questions
    # in the same thread, which is what makes follow-up questions work.
    messages: Annotated[list[AnyMessage], add_messages]

    # Set by the caller for every question, from the validated login token.
    # Never read from the conversation or from model output.
    role: Role
    # The user's pseudonym from the login token. Keys their long-term memory.
    user: str | None
    request_id: str
    # A running summary of the older turns of a long thread, and how many
    # messages it covers (ADR-0008). Kept across questions.
    summary: str | None
    summary_covers: int
    # What the memory writer saved for this question (the `memory` event).
    memory_saved: list[str]

    # Set by the input guard.
    question: str
    blocked_reason: str | None

    # Set by the supervisor.
    plan: Plan | None
    next_agent: AgentName | None

    # Set by the retrieval agent.
    search_plan: SearchPlan | None
    search: SearchSummary | None
    hits: list[SearchHit]
    research: ResearchSummary | None
    # Findings from the research sub-agents, for the answer (as data).
    findings: list[str]
    # Enterprise data tool calls of this question, and their results.
    tool_results: list[ToolRecord]
    # The human approval step: "approved" or "rejected" (None: not asked).
    approval: str | None
    relax_filters: bool
    retrieval_attempts: int

    # Set by the response agent and the validator.
    draft: Answer | None
    response_attempts: int
    validation: Validation | None
    answer: Answer | None

    # Collected during one question (cleared by the input guard).
    errors: Annotated[dict[str, AgentError], reset_or_merge]
    steps: Annotated[list[Step], reset_or_extend]
    limits: Annotated[list[str], reset_or_extend]
    completed: Annotated[list[AgentName], reset_or_extend]
    llm_calls: Annotated[list[LLMCall], reset_or_extend]


# Every type stored in the state. The checkpointer may only rebuild these
# (agents/checkpoint.py), so a tampered checkpoint cannot create other objects.
STATE_TYPES: tuple[type, ...] = (
    AgentName,
    Intent,
    ErrorKind,
    AgentError,
    Step,
    LLMCall,
    Plan,
    SearchPlan,
    SearchSummary,
    BatchReport,
    ResearchSummary,
    ToolRecord,
    AnswerKind,
    NumberedCitation,
    Answer,
    Validation,
    Role,
)
