"""Runs one question through the agent graph. The entry point for callers.

    async with open_agent_service(settings) as agents:
        # The whole answer at once (the CLI, tests):
        run = await agents.ask("Why do card payments fail?", role=Role.ANALYST)
        print(run.answer.to_markdown())

        # Or event by event, while the graph runs (the API's POST /chat):
        async for event in agents.stream("Why do card payments fail?", role=Role.ANALYST):
            print(event.type)

Both take the role as a separate argument. In the app it comes from the
validated login token (api/identity.py), together with the pseudonymised
user ID that labels the run's logs and traces; the CLI takes the role as
an option for local testing, like `make search`.

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
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from langgraph.store.postgres.aio import AsyncPostgresStore
from langgraph.types import Command

from agents.checkpoint import memory_checkpointer, serializer
from agents.events import AgentEvent, ApprovalRequiredEvent
from agents.graph import GRAPH_NAME, build_graph
from agents.llm import LLMGateway, build_gateway
from agents.memory import LongTermMemory
from agents.nodes import NodeContext
from agents.state import AgentError, AgentState, Answer, LLMCall, SearchSummary
from agents.streaming import STREAM_MODES, RunRecorder
from agents.telemetry import AgentMetrics
from agents.tools.enterprise import EnterpriseTools
from agents.tools.sandbox import CodeRunner
from agents.tracing import TracedSearcher, Tracing
from core.config import Settings
from core.errors import RetrievalError
from core.logging import get_logger
from rag.catalog import CATALOG_FILE, Catalog
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
        memory: LongTermMemory | None = None,
        store: BaseStore | None = None,
        gateway: LLMGateway | None = None,
    ) -> None:
        self.graph = graph
        self.metrics = metrics
        self.tracing = tracing
        # Long-term memory, for the API's "my memory" endpoints.
        self.memory = memory
        # The store memory lives in; the API also keeps thread owners there.
        self.store = store
        # The model gateway, for callers outside the graph (the evaluations).
        self.gateway = gateway

    @property
    def traced(self) -> bool:
        return self.tracing is not None and self.tracing.enabled

    async def ask(
        self,
        question: str,
        *,
        role: Role,
        user: str | None = None,
        thread_id: str | None = None,
        request_id: str | None = None,
        approve: bool = False,
    ) -> AgentRun:
        """Run the question to the end and return the validated answer.

        If the run stops for human approval, it is resumed straight away
        with `approve` (False by default: research is skipped, the safe
        choice). The CLI passes True, because the person typing is the one
        who would approve.
        """
        recorder = self._recorder(thread_id)
        request_id = request_id or uuid.uuid4().hex
        started = time.perf_counter()
        nodes: list[str] = []
        # The question text is never logged; see ASSUMPTIONS S6.
        with structlog.contextvars.bound_contextvars(
            thread_id=recorder.thread_id, run_id=recorder.run_id, role=role.value, user=user
        ):
            last = await _drain(self._events(question, role, user, request_id, recorder))
            if isinstance(last, ApprovalRequiredEvent):
                nodes += recorder.nodes
                recorder = self._recorder(recorder.thread_id)
                command = _resume_command(approve, role, user)
                await _drain(self._run(command, role, user, request_id, recorder))
        nodes += recorder.nodes
        final = recorder.final
        return AgentRun(
            answer=recorder.answer,
            thread_id=recorder.thread_id,
            request_id=request_id,
            run_id=recorder.run_id,
            nodes=tuple(nodes),
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
        user: str | None = None,
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
        return self._events(question, role, user, request_id or uuid.uuid4().hex, recorder)

    def _recorder(self, thread_id: str | None, run_id: str | None = None) -> RunRecorder:
        return RunRecorder(
            # LangSmith needs the run ID to be a UUID.
            run_id=run_id or str(uuid.uuid4()),
            thread_id=thread_id or uuid.uuid4().hex,
            traced=self.traced,
        )

    def _events(
        self,
        question: str,
        role: Role,
        user: str | None,
        request_id: str,
        recorder: RunRecorder,
    ) -> AsyncGenerator[AgentEvent]:
        inputs: AgentState = {
            "messages": [HumanMessage(question)],
            "role": role,
            "user": user,
            "request_id": request_id,
        }
        return self._run(inputs, role, user, request_id, recorder)

    def resume(
        self,
        thread_id: str,
        *,
        approved: bool,
        role: Role,
        user: str | None = None,
        request_id: str | None = None,
        run_id: str | None = None,
    ) -> AsyncGenerator[AgentEvent]:
        """Continue a run that stopped for approval, with the user's answer.

        The events are the same as for `stream`; the last is `response_end`.
        """
        recorder = self._recorder(thread_id, run_id)
        return self._run(
            _resume_command(approved, role, user),
            role,
            user,
            request_id or uuid.uuid4().hex,
            recorder,
        )

    async def pending_approval(self, thread_id: str) -> dict[str, Any] | None:
        """What the thread is waiting to have approved, or None."""
        snapshot = await self.graph.aget_state({"configurable": {"thread_id": thread_id}})
        for item in snapshot.interrupts:
            value = item.value
            return dict(value) if isinstance(value, dict) else {"message": str(value)}
        return None

    async def _run(
        self,
        inputs: AgentState | Command[Any],
        role: Role,
        user: str | None,
        request_id: str,
        recorder: RunRecorder,
    ) -> AsyncGenerator[AgentEvent]:
        started = time.perf_counter()
        config = run_config(
            role=role,
            user=user,
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

        interrupts = recorder.final.get("__interrupt__") or ()
        if interrupts:
            # Stopped for human approval: the saved state waits for the answer.
            request = interrupts[0].value if isinstance(interrupts[0].value, dict) else {}
            log.info("agent_run_waiting_for_approval", action=request.get("action"))
            yield ApprovalRequiredEvent(
                approval_id=recorder.thread_id,
                action=str(request.get("action", "")),
                message=str(request.get("message", "")),
            )
            return

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

    # ---------------------------------------------------------------
    # Saved conversations (the checkpointer)
    # ---------------------------------------------------------------

    async def history(self, thread_id: str) -> list[dict[str, str]]:
        """The thread's questions and answers, oldest first: [{"role", "text"}]."""
        snapshot = await self.graph.aget_state({"configurable": {"thread_id": thread_id}})
        turns = []
        for message in snapshot.values.get("messages", []) if snapshot.values else []:
            if isinstance(message, HumanMessage):
                turns.append({"role": "user", "text": message.text})
            elif isinstance(message, AIMessage):
                turns.append({"role": "assistant", "text": message.text})
        return turns

    async def forget_thread(self, thread_id: str) -> None:
        """Delete every saved step of the thread."""
        checkpointer = self.graph.checkpointer
        if isinstance(checkpointer, BaseCheckpointSaver):
            await checkpointer.adelete_thread(thread_id)

    def _callbacks(self, role: Role) -> list[BaseCallbackHandler]:
        if self.tracing is None:
            return []
        return self.tracing.callbacks(tags=["helix-agent", f"role:{role.value}"])


def _resume_command(approved: bool, role: Role, user: str | None) -> Command[Any]:
    """Resume a waiting run with the user's answer.

    The role and user are set again from the caller's current login, never
    taken from the saved state: a resumed run gets exactly the access of
    the person resuming it (and a checkpoint that stores the role as plain
    text cannot change its type).
    """
    return Command(resume={"approved": approved, "by": user}, update={"role": role, "user": user})


async def _drain(events: AsyncGenerator[AgentEvent]) -> AgentEvent | None:
    """Run the events to the end; return the last one."""
    last = None
    async with aclosing(events) as running:
        async for event in running:
            last = event
    return last


def run_config(
    *,
    role: Role,
    thread_id: str,
    user: str | None = None,
    request_id: str,
    run_id: str | None = None,
    callbacks: Sequence[BaseCallbackHandler] = (),
) -> RunnableConfig:
    """The LangGraph config for one run: thread, limits, and trace labels.

    `user` is the pseudonymised user ID, never the username or e-mail, so
    traces can be grouped per user without saying who the user is.
    """
    metadata = {"request_id": request_id, "thread_id": thread_id, "role": role.value}
    if user is not None:
        metadata["user"] = user
    config: RunnableConfig = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": RECURSION_LIMIT,
        "run_name": GRAPH_NAME,
        "tags": [f"role:{role.value}"],
        "metadata": metadata,
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


async def _open_memory(
    stack: AsyncExitStack, settings: Settings
) -> tuple[BaseCheckpointSaver[Any], BaseStore]:
    """Where conversations and long-term memory are kept.

    With POSTGRES_URL (Docker Compose): LangGraph's Postgres checkpointer and
    store, so conversations survive restarts. The tables are created on the
    first start. Without it (`make run-api` on its own): in memory, gone when
    the API stops.
    """
    url = settings.postgres_url.get_secret_value() if settings.postgres_url else ""
    if not url:
        log.info("memory_in_process")
        return memory_checkpointer(), InMemoryStore()
    saver = await stack.enter_async_context(
        AsyncPostgresSaver.from_conn_string(url, serde=serializer())
    )
    await saver.setup()
    store = await stack.enter_async_context(AsyncPostgresStore.from_conn_string(url))
    await store.setup()
    log.info("memory_in_postgres")
    return saver, store


def load_catalog(settings: Settings) -> Catalog | None:
    """The document catalog saved by `make ingest`, or None if there is none."""
    path = settings.index_artifacts_dir / CATALOG_FILE
    try:
        return Catalog.load(path)
    except (OSError, ValueError) as error:
        log.info("catalog_unavailable", reason=type(error).__name__)
        return None


@asynccontextmanager
async def open_agent_service(
    settings: Settings,
    *,
    metrics: AgentMetrics | None = None,
    retrieval_metrics: RetrievalMetrics | None = None,
    sandbox: CodeRunner | None = None,
) -> AsyncIterator[AgentService]:
    """Connect the models and the search index, and build the graph.

    `sandbox` runs the research agent's Python plans; without one, research
    uses its fixed plan. The document catalog is read from the index folder
    if `make ingest` has written it.

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

        checkpointer, store = await _open_memory(stack, settings)
        memory = LongTermMemory(store) if settings.memory_enabled else None
        context = NodeContext(
            gateway=gateway,
            # Each search is its own step in a trace, with its filter and results.
            searcher=TracedSearcher(searcher),
            metrics=metrics,
            history_messages=settings.agent_history_messages,
            sandbox=sandbox,
            catalog=load_catalog(settings),
            research_max_depth=settings.research_max_depth,
            research_max_fanout=settings.research_max_fanout,
            research_token_budget=settings.research_token_budget,
            research_max_sources=settings.research_max_sources,
            research_parallel_calls=settings.research_parallel_calls,
            research_max_extractions=settings.research_max_extractions,
            memory=memory,
            summary_after_messages=settings.memory_summary_after_messages,
            approval_for_research=settings.approval_for_research,
            enterprise=(
                EnterpriseTools(settings.mcp_url, timeout_seconds=settings.mcp_timeout_seconds)
                if settings.mcp_url
                else None
            ),
        )
        try:
            yield AgentService(
                build_graph(context, checkpointer=checkpointer),
                metrics=metrics,
                tracing=tracing,
                memory=memory,
                store=store,
                gateway=gateway,
            )
        finally:
            tracing.flush()
