"""What every node has in common: its dependencies, timing and containment.

Each node is a small class with one `run()` method. Calling the node:

1. times it (`agent_node_duration_seconds{node}`);
2. runs it;
3. if it raises anything unexpected (a bug), logs the traceback, counts it
   and returns the node's `on_error()` update instead of crashing the run.
   This is the "one failing agent never breaks the others" rule (ADR-0006).

Expected failures (the model or Pinecone being down) are handled inside
`run()` itself, with a message that says what happened. Cancellation (the
user left) is not an error and is never caught.
"""

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, ClassVar, Protocol

from agents.llm import LLMGateway
from agents.state import AgentError, AgentName, AgentState, ErrorKind, Step
from agents.telemetry import AgentMetrics
from core.logging import get_logger
from rag.retrieval import SearchRequest, SearchResult
from security.roles import Role

log = get_logger(__name__)

StateUpdate = dict[str, Any]


class Searcher(Protocol):
    """Document search, as the retrieval agent sees it (rag.retrieval.HybridRetriever)."""

    async def search(self, request: SearchRequest, *, role: Role) -> SearchResult: ...


@dataclass(frozen=True)
class NodeContext:
    """The dependencies shared by every node, passed in when the graph is built."""

    gateway: LLMGateway
    searcher: Searcher
    metrics: AgentMetrics
    # Earlier messages the model sees (AGENT_HISTORY_MESSAGES).
    history_messages: int = 6
    # A function, so tests can fix "today" for date questions.
    today: Callable[[], date] = date.today


class AgentNode(ABC):
    name: ClassVar[AgentName]

    def __init__(self, context: NodeContext) -> None:
        self.context = context

    @abstractmethod
    async def run(self, state: AgentState) -> StateUpdate:
        """Do this node's work and return the state fields to update."""

    def on_error(self, state: AgentState, error: AgentError) -> StateUpdate:
        """The update to use when `run()` failed with a bug. Nodes override it
        to keep the run moving (for example, the response agent lists the
        sources it has)."""
        return {"errors": {self.name.value: error}}

    async def __call__(self, state: AgentState) -> StateUpdate:
        started = time.perf_counter()
        try:
            return await self.run(state)
        except Exception:
            log.exception("agent_node_failed", node=self.name.value)
            self.context.metrics.node_error(self.name, ErrorKind.INTERNAL)
            error = AgentError(
                agent=self.name,
                kind=ErrorKind.INTERNAL,
                message=f"The {self.name.value.replace('_', ' ')} step failed unexpectedly.",
            )
            update = self.on_error(state, error)
            if "errors" in update and update["errors"] is None:
                # The update clears the error slots (a new question starts).
                # The failure is still logged and counted above.
                return update
            update["errors"] = {**(update.get("errors") or {}), self.name.value: error}
            return update
        finally:
            self.context.metrics.node_duration.labels(node=self.name.value).observe(
                time.perf_counter() - started
            )

    # Small helpers so nodes read the same way.

    def step(self, summary: str) -> Step:
        return Step(agent=self.name, summary=summary)

    def failure(self, kind: ErrorKind, message: str, *, retryable: bool = False) -> AgentError:
        self.context.metrics.node_error(self.name, kind)
        return AgentError(agent=self.name, kind=kind, message=message, retryable=retryable)
