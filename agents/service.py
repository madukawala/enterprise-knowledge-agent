"""Runs one question through the agent graph. The entry point for callers.

    async with open_agent_service(settings) as agents:
        # The whole answer at once (the CLI, tests):
        run = await agents.ask("Why do card payments fail?", role=Role.ANALYST)
        print(run.answer.to_markdown())

        # Or event by event, while the graph runs (the API's POST /chat):
        async for event in agents.stream("Why do card payments fail?", role=Role.ANALYST):
            print(event.type)

Both take the role as a separate argument. In the app it comes from the
validated login token (`feature/auth-rbac`); the CLI takes it as an option
for local testing, like `make search`.

`ask()` is built on `stream()`, so the CLI and the API run exactly the
same code. The events are described in agents/events.py.
"""

import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import structlog
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphRecursionError
from langgraph.graph.state import CompiledStateGraph

from agents.checkpoint import memory_checkpointer
from agents.events import AgentEvent
from agents.graph import GRAPH_NAME, build_graph
from agents.llm import build_gateway
from agents.nodes import NodeContext
from agents.state import AgentError, AgentState, Answer, LLMCall, SearchSummary
from agents.streaming import STREAM_MODES, RunRecorder
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
    # The LangGraph run ID, which is also the LangSmith trace ID when traced.
    run_id: str
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

    @property
    def traced(self) -> bool:
        return self.tracing is not None and self.tracing.enabled

    async def ask(
        self,
        question: str,
        *,
        role: Role,
        thread_id: str | None = None,
        request_id: str | None = None,
    ) -> AgentRun:
        """Run the question to the end and return the validated answer."""
        recorder = self._recorder(thread_id)
        request_id = request_id or uuid.uuid4().hex
        started = time.perf_counter()
        # The question text is never logged; see ASSUMPTIONS S6.
        with structlog.contextvars.bound_contextvars(
            thread_id=recorder.thread_id, run_id=recorder.run_id, role=role.value
        ):
            async with aclosing(self._events(question, role, request_id, recorder)) as events:
                async for _ in events:
                    pass
        final = recorder.final
        return AgentRun(
            answer=recorder.answer,
            thread_id=recorder.thread_id,
            request_id=request_id,
            run_id=recorder.run_id,
            nodes=tuple(recorder.nodes),
            llm_calls=tuple(final.get("llm_calls", [])),
            search=final.get("search"),
            errors=tuple(final.get("errors", {}).values()),
            duration_ms=round((time.perf_counter() - started) * 1000),
        )

    def stream(
        self,
        question: str,
        *,
        role: Role,
        thread_id: str | None = None,
        request_id: str | None = None,
        run_id: str | None = None,
    ) -> AsyncGenerator[AgentEvent]:
        """The run's events, as they happen. The last one is `response_end`.

        Stopping the iteration (the user left) cancels the graph run and any
        model or search call in progress. Close the iterator when you stop
        early, for example with `contextlib.aclosing`.
        """
        recorder = self._recorder(thread_id, run_id)
        return self._events(question, role, request_id or uuid.uuid4().hex, recorder)

    def _recorder(self, thread_id: str | None, run_id: str | None = None) -> RunRecorder:
        return RunRecorder(
            # LangSmith needs the run ID to be a UUID.
            run_id=run_id or str(uuid.uuid4()),
            thread_id=thread_id or uuid.uuid4().hex,
            traced=self.traced,
        )

    async def _events(
        self, question: str, role: Role, request_id: str, recorder: RunRecorder
    ) -> AsyncGenerator[AgentEvent]:
        started = time.perf_counter()
        inputs: AgentState = {
            "messages": [HumanMessage(question)],
            "role": role,
            "request_id": request_id,
        }
        config = run_config(
            role=role,
            thread_id=recorder.thread_id,
            request_id=request_id,
            run_id=recorder.run_id,
            callbacks=self._callbacks(role),
        )
        yield recorder.accepted()
        try:
            async for mode, chunk in self.graph.astream(inputs, config, stream_mode=STREAM_MODES):
                for event in recorder.translate(cast(str, mode), chunk):
                    yield event
        except GraphRecursionError:
            # A routing bug. Answer safely (with the sources found so far)
            # instead of failing the request.
            log.error("agent_recursion_limit", nodes=len(recorder.nodes))

        seconds = time.perf_counter() - started
        answer = recorder.answer
        self.metrics.run_finished(answer.kind, seconds)
        log.info(
            "agent_run_finished",
            answer_kind=answer.kind.value,
            nodes=len(recorder.nodes),
            llm_calls=len(recorder.final.get("llm_calls", [])),
            errors=sorted(recorder.final.get("errors", {})),
            duration_ms=round(seconds * 1000),
        )
        yield recorder.response_end(round(seconds * 1000))

    def _callbacks(self, role: Role) -> list[BaseCallbackHandler]:
        if self.tracing is None:
            return []
        return self.tracing.callbacks(tags=["helix-agent", f"role:{role.value}"])


def run_config(
    *,
    role: Role,
    thread_id: str,
    request_id: str,
    run_id: str | None = None,
    callbacks: Sequence[BaseCallbackHandler] = (),
) -> RunnableConfig:
    """The LangGraph config for one run: thread, limits, and trace labels."""
    config: RunnableConfig = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": RECURSION_LIMIT,
        "run_name": GRAPH_NAME,
        "tags": [f"role:{role.value}"],
        "metadata": {"request_id": request_id, "thread_id": thread_id, "role": role.value},
    }
    if run_id is not None:
        config["run_id"] = uuid.UUID(run_id)
    if callbacks:
        config["callbacks"] = list(callbacks)
    return config


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
