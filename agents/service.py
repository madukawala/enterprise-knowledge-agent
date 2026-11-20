"""Runs one question through the agent graph. The entry point for callers.

    async with open_agent_service(settings) as agents:
        run = await agents.ask("Why do card payments fail?", role=Role.ANALYST)
        print(run.answer.to_markdown())

`ask()` takes the role as a separate argument. In the app it comes from
the validated login token (`feature/auth-rbac`); the CLI takes it as an
option for local testing, like `make search`.

The streaming API (`feature/streaming-api`) uses the same graph, but reads
its events as they happen instead of waiting for the final answer.
"""

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager, AsyncExitStack, asynccontextmanager, nullcontext
from dataclasses import dataclass
from typing import Any, cast

import structlog
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphRecursionError
from langgraph.graph.state import CompiledStateGraph

from agents.checkpoint import memory_checkpointer
from agents.graph import GRAPH_NAME, build_graph
from agents.llm import build_gateway
from agents.nodes import NodeContext
from agents.nodes.response import fallback_answer
from agents.state import AgentError, AgentState, Answer, LLMCall, SearchSummary
from agents.telemetry import AgentMetrics
from agents.tracing import Tracing
from core.config import Settings
from core.errors import RetrievalError
from core.logging import get_logger
from rag.retrieval import RetrievalSetupError, SearchRequest, SearchResult, open_retriever
from rag.telemetry import RetrievalMetrics
from security.roles import Role

log = get_logger(__name__)

# A normal question takes about 7 steps, and a question with a retry and a
# rewrite about 12. This limit stops a routing bug from looping forever.
RECURSION_LIMIT = 30


@dataclass(frozen=True)
class AgentRun:
    """The result of one question."""

    answer: Answer
    thread_id: str
    request_id: str
    # The nodes in the order they ran, e.g. input_guard, supervisor, retrieval, ...
    nodes: tuple[str, ...]
    llm_calls: tuple[LLMCall, ...]
    search: SearchSummary | None
    errors: tuple[AgentError, ...]
    duration_ms: int


class AgentService:
    def __init__(
        self,
        graph: CompiledStateGraph[AgentState, None, AgentState, AgentState],
        *,
        metrics: AgentMetrics,
        tracing: Tracing | None = None,
    ) -> None:
        self.graph = graph
        self.metrics = metrics
        self.tracing = tracing

    async def ask(
        self,
        question: str,
        *,
        role: Role,
        thread_id: str | None = None,
        request_id: str | None = None,
    ) -> AgentRun:
        thread_id = thread_id or uuid.uuid4().hex
        request_id = request_id or uuid.uuid4().hex
        started = time.perf_counter()
        inputs: AgentState = {
            "messages": [HumanMessage(question)],
            "role": role,
            "request_id": request_id,
        }
        config = run_config(role=role, thread_id=thread_id, request_id=request_id)

        nodes: list[str] = []
        final: dict[str, Any] = {}
        # The question text is never logged; see ASSUMPTIONS S6.
        with structlog.contextvars.bound_contextvars(thread_id=thread_id, role=role.value):
            try:
                with self._traced(role):
                    async for mode, chunk in self.graph.astream(
                        inputs, config, stream_mode=["updates", "values"]
                    ):
                        data = cast(dict[str, Any], chunk)
                        if mode == "updates":
                            nodes.extend(data)
                        else:
                            final = data
            except GraphRecursionError:
                # A routing bug. Answer safely instead of failing the request.
                log.error("agent_recursion_limit", nodes=len(nodes))
                final = {**final, "answer": fallback_answer(final.get("hits", []))}

            answer: Answer = final.get("answer") or fallback_answer(final.get("hits", []))
            seconds = time.perf_counter() - started
            self.metrics.run_finished(answer.kind, seconds)
            log.info(
                "agent_run_finished",
                answer_kind=answer.kind.value,
                nodes=len(nodes),
                llm_calls=len(final.get("llm_calls", [])),
                errors=sorted(final.get("errors", {})),
                duration_ms=round(seconds * 1000),
            )
        return AgentRun(
            answer=answer,
            thread_id=thread_id,
            request_id=request_id,
            nodes=tuple(nodes),
            llm_calls=tuple(final.get("llm_calls", [])),
            search=final.get("search"),
            errors=tuple(final.get("errors", {}).values()),
            duration_ms=round(seconds * 1000),
        )

    def _traced(self, role: Role) -> AbstractContextManager[None]:
        if self.tracing is None:
            return nullcontext()
        return self.tracing.run(tags=["helix-agent", f"role:{role.value}"])


def run_config(*, role: Role, thread_id: str, request_id: str) -> RunnableConfig:
    """The LangGraph config for one run: thread, limits, and trace labels."""
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": RECURSION_LIMIT,
        "run_name": GRAPH_NAME,
        "tags": [f"role:{role.value}"],
        "metadata": {"request_id": request_id, "thread_id": thread_id, "role": role.value},
    }


class UnavailableSearcher:
    """Stands in for search when it could not be set up (no key, no index).

    Every search fails with `RetrievalError`, so the agents still run and
    answer "document search is unavailable" instead of the app refusing
    to start.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def search(self, request: SearchRequest, *, role: Role) -> SearchResult:
        raise RetrievalError()


@asynccontextmanager
async def open_agent_service(
    settings: Settings,
    *,
    metrics: AgentMetrics | None = None,
    retrieval_metrics: RetrievalMetrics | None = None,
) -> AsyncIterator[AgentService]:
    """Connect the models and the search index, and build the graph.

    Raises `LLMSetupError` when no model key is set. A search that cannot be
    set up is not fatal: the agents run and say documents are unavailable.
    """
    metrics = metrics or AgentMetrics()
    gateway = build_gateway(settings, metrics=metrics)
    tracing = Tracing(settings)
    async with AsyncExitStack() as stack:
        searcher: Any
        try:
            searcher = await stack.enter_async_context(
                open_retriever(settings, metrics=retrieval_metrics)
            )
        except RetrievalSetupError as error:
            log.warning("search_unavailable", reason=str(error))
            searcher = UnavailableSearcher(str(error))

        context = NodeContext(
            gateway=gateway,
            searcher=searcher,
            metrics=metrics,
            history_messages=settings.agent_history_messages,
        )
        try:
            yield AgentService(
                build_graph(context, checkpointer=memory_checkpointer()),
                metrics=metrics,
                tracing=tracing,
            )
        finally:
            tracing.flush()
