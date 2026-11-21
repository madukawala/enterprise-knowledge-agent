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

from langgraph.errors import GraphBubbleUp

from agents.llm import LLMGateway
from agents.memory import LongTermMemory
from agents.state import AgentError, AgentName, AgentState, ErrorKind, Step
from agents.telemetry import AgentMetrics
from agents.tools.enterprise import EnterpriseTools
from agents.tools.sandbox import CodeRunner
from core.logging import get_logger
from rag.catalog import Catalog
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
    # The research agent's tools and limits. Without a sandbox, research
    # uses its fixed plan; without a catalog, it explores nothing.
    sandbox: CodeRunner | None = None
    catalog: Catalog | None = None
    research_max_depth: int = 3
    research_max_fanout: int = 5
    research_token_budget: int = 40_000
    research_max_sources: int = 20
    research_parallel_calls: int = 2
    # Ask the user before running deep research (the human approval step).
    approval_for_research: bool = False
    # The enterprise data tools (the MCP server). None: not configured.
    enterprise: EnterpriseTools | None = None
    # Long-term memory (None: off) and when to summarise a long thread.
    memory: LongTermMemory | None = None
    summary_after_messages: int = 12
    research_max_extractions: int = 4


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
        except GraphBubbleUp:
            # LangGraph's own control flow, such as `interrupt()` for human
            # approval: not a failure, so it must reach the graph untouched.
            raise
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
