"""Builders for agent tests: scripted model replies, a fake search, a graph.

model = ScriptedChatModel(
    structured={
        "SupervisorDecision": [decision("document_question")],
        "SearchPlan": [search_plan("pool exhausted", ["runbooks"])],
    },
    texts=["Raise the pool size [1]."],
)
searcher = FakeSearcher([result_with(hit("RB-001"))])
graph = agent_graph(model, searcher)
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from agents.graph import build_graph
from agents.llm import ChatProvider, LLMGateway
from agents.nodes import NodeContext, Searcher
from agents.service import AgentService
from agents.state import AgentState
from agents.telemetry import AgentMetrics
from rag.retrieval import RerankStatus, SearchHit, SearchRequest, SearchResult
from rag.schema import ChunkMetadata, Collection
from security.roles import AccessLevel, Role
from tests.helpers.llm import ScriptedChatModel
from tests.helpers.search import make_chunk

TODAY = date(2026, 11, 21)


def decision(
    intent: str, plan: Sequence[str] = ("Search the documents", "Answer")
) -> dict[str, Any]:
    return {"intent": intent, "plan": list(plan), "reason": "test"}


def search_plan(
    query: str,
    collections: Sequence[str] = ("runbooks",),
    *,
    departments: Sequence[str] = (),
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    return {
        "query": query,
        "collections": list(collections),
        "departments": list(departments),
        "date_from": date_from,
        "date_to": date_to,
    }


def hit(
    doc_id: str,
    *,
    access_level: AccessLevel = AccessLevel.INTERNAL,
    text: str = "Some text.",
    chunk: ChunkMetadata | None = None,
) -> SearchHit:
    chunk = chunk or make_chunk(doc_id, access_level=access_level, text=text)
    return SearchHit(
        record_id=f"{chunk.doc_id}#{chunk.chunk_index}",
        collection=Collection.RUNBOOKS,
        score=0.5,
        chunk=chunk,
    )


def result_with(
    *hits: SearchHit,
    failed: Sequence[Collection] = (),
    rerank: RerankStatus = RerankStatus.APPLIED,
) -> SearchResult:
    return SearchResult(
        hits=tuple(hits),
        collections_searched=(Collection.RUNBOOKS,),
        failed_collections=tuple(failed),
        candidates=len(hits) * 4,
        alpha=0.5,
        rerank=rerank,
        took_ms=12,
    )


@dataclass
class FakeSearcher:
    """Returns the scripted results in order (the last one repeats).

    A scripted exception is raised instead. Every request and role is kept,
    so tests can check what the agent asked for and as whom.
    """

    results: list[SearchResult | Exception] = field(default_factory=lambda: [result_with()])
    requests: list[tuple[SearchRequest, Role]] = field(default_factory=list)

    async def search(self, request: SearchRequest, *, role: Role) -> SearchResult:
        self.requests.append((request, role))
        outcome = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def gateway(
    *models: ScriptedChatModel, timeout: float = 5.0, metrics: AgentMetrics | None = None
) -> LLMGateway:
    names = ("anthropic", "gemini", "third")
    return LLMGateway(
        [
            ChatProvider(name=names[i], model=f"{names[i]}-test", chat=m)
            for i, m in enumerate(models)
        ],
        timeout_seconds=timeout,
        metrics=metrics,
    )


def node_context(
    model: ScriptedChatModel | LLMGateway,
    searcher: Searcher | None = None,
    *,
    metrics: AgentMetrics | None = None,
    **options: Any,
) -> NodeContext:
    """The nodes' dependencies. `options` sets any other NodeContext field,
    for example `sandbox=LocalSandbox()` or `research_max_depth=2`."""
    metrics = metrics or AgentMetrics()
    return NodeContext(
        gateway=model if isinstance(model, LLMGateway) else gateway(model, metrics=metrics),
        searcher=searcher or FakeSearcher(),
        metrics=metrics,
        history_messages=6,
        today=lambda: TODAY,
        **options,
    )


def agent_graph(
    model: ScriptedChatModel | LLMGateway,
    searcher: Searcher | None = None,
    *,
    metrics: AgentMetrics | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    **options: Any,
) -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
    return build_graph(
        node_context(model, searcher, metrics=metrics, **options), checkpointer=checkpointer
    )


def agent_service(
    model: ScriptedChatModel | LLMGateway,
    searcher: Searcher | None = None,
    *,
    metrics: AgentMetrics | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    **options: Any,
) -> AgentService:
    metrics = metrics or AgentMetrics()
    memory = options.get("memory")
    return AgentService(
        agent_graph(model, searcher, metrics=metrics, checkpointer=checkpointer, **options),
        metrics=metrics,
        memory=memory,
        store=memory.store if memory is not None else None,
    )
