"""HybridRetriever against an in-memory index: merging, failures, re-ranking."""

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from core.errors import RetrievalError
from rag.bm25 import BM25Encoder
from rag.retrieval import (
    MAX_QUERY_CHARS,
    RerankStatus,
    SearchHit,
    SearchRequest,
    merge_hits,
)
from rag.schema import Collection
from security.roles import Role
from tests.helpers.pinecone import FakeAsyncIndex, FakeAsyncInference
from tests.helpers.search import make_chunk, make_record, make_retriever, unit_vector

QUESTION = "payhub pool"


def hit(doc_id: str, score: float, chunk_index: int = 0) -> SearchHit:
    return SearchHit(
        record_id=f"{doc_id}#{chunk_index}",
        collection=Collection.RUNBOOKS,
        score=score,
        chunk=make_chunk(doc_id, chunk_index=chunk_index),
    )


@pytest.fixture
def inference() -> FakeAsyncInference:
    # The question always points along the first axis, so a record's dense
    # score is simply the first value of its vector.
    return FakeAsyncInference(dimension=8, vectors={QUESTION: unit_vector(0)})


@pytest.fixture
def index() -> FakeAsyncIndex:
    index = FakeAsyncIndex()
    index.add(
        "runbooks",
        make_record(
            make_chunk("RB-1", text="Raise the payhub pool size."), dense=unit_vector(0, 0.9)
        ),
        make_record(make_chunk("RB-2"), dense=unit_vector(0, 0.2)),
    )
    index.add("incidents", make_record(make_chunk("INC-1"), dense=unit_vector(0, 0.5)))
    return index


def request(**fields: Any) -> SearchRequest:
    fields.setdefault("collections", (Collection.RUNBOOKS, Collection.INCIDENTS))
    return SearchRequest(query=QUESTION, alpha=1, **fields)


# ---------------------------------------------------------------
# Merging
# ---------------------------------------------------------------


async def test_results_from_every_collection_are_merged_by_score(
    index: FakeAsyncIndex, inference: FakeAsyncInference
) -> None:
    result = await make_retriever(index, inference=inference).search(request(), role=Role.VIEWER)

    assert result.doc_ids() == ["RB-1", "INC-1", "RB-2"]
    assert [round(h.score, 6) for h in result.hits] == [0.9, 0.5, 0.2]
    assert result.candidates == 3
    assert result.collections_searched == (Collection.RUNBOOKS, Collection.INCIDENTS)
    assert not result.partial


def test_one_document_cannot_fill_every_slot() -> None:
    hits = [hit("LONG", 0.9 - i / 100, chunk_index=i) for i in range(5)] + [hit("OTHER", 0.1)]
    merged = merge_hits([hits], max_per_document=3)
    assert [h.record_id for h in merged] == ["LONG#0", "LONG#1", "LONG#2", "OTHER#0"]


def test_equal_scores_are_ordered_the_same_way_every_time() -> None:
    merged = merge_hits([[hit("B", 0.5)], [hit("A", 0.5)]], max_per_document=3)
    assert [h.record_id for h in merged] == ["A#0", "B#0"]


async def test_top_k_limits_the_result(
    index: FakeAsyncIndex, inference: FakeAsyncInference
) -> None:
    result = await make_retriever(index, inference=inference).search(
        request(top_k=1), role=Role.VIEWER
    )
    assert result.doc_ids() == ["RB-1"]


async def test_collections_are_queried_at_the_same_time(inference: FakeAsyncInference) -> None:
    class CountingIndex(FakeAsyncIndex):
        running = 0
        most = 0

        async def query(self, **kwargs: Any) -> Any:
            CountingIndex.running += 1
            CountingIndex.most = max(CountingIndex.most, CountingIndex.running)
            await asyncio.sleep(0.01)
            CountingIndex.running -= 1
            return await super().query(**kwargs)

    await make_retriever(CountingIndex(), inference=inference).search(
        SearchRequest(query=QUESTION), role=Role.VIEWER
    )
    assert CountingIndex.most == len(Collection)


async def test_every_collection_gets_the_same_filter_and_vectors(
    index: FakeAsyncIndex, inference: FakeAsyncInference
) -> None:
    bm25 = BM25Encoder.fit(["payhub pool exhausted", "card certificate"])
    await make_retriever(index, inference=inference, bm25=bm25).search(
        SearchRequest(query=QUESTION, alpha=0.5), role=Role.VIEWER
    )

    assert {q["namespace"] for q in index.queries} == {c.value for c in Collection}
    assert len({repr(q["filter"]) for q in index.queries}) == 1
    sparse = index.queries[0]["sparse"]
    assert sparse is not None
    # The BM25 query weights add up to 1, so after scaling they add up to 1 - alpha.
    assert sum(sparse["values"]) == pytest.approx(0.5)


# ---------------------------------------------------------------
# Failures
# ---------------------------------------------------------------


async def test_a_failing_collection_gives_a_partial_result(
    index: FakeAsyncIndex, inference: FakeAsyncInference
) -> None:
    index.failing.add("incidents")
    retriever = make_retriever(index, inference=inference)

    result = await retriever.search(request(), role=Role.VIEWER)

    assert result.partial
    assert result.failed_collections == (Collection.INCIDENTS,)
    assert result.doc_ids() == ["RB-1", "RB-2"]
    errors = retriever.metrics.registry.get_sample_value(
        "retrieval_collection_errors_total", {"collection": "incidents", "reason": "error"}
    )
    assert errors == 1
    partial = retriever.metrics.registry.get_sample_value(
        "retrieval_searches_total", {"outcome": "partial"}
    )
    assert partial == 1


async def test_a_slow_collection_times_out_without_holding_up_the_rest(
    index: FakeAsyncIndex, inference: FakeAsyncInference
) -> None:
    index.slow.add("incidents")
    retriever = make_retriever(index, inference=inference, timeout_seconds=0.05)

    result = await retriever.search(request(), role=Role.VIEWER)

    assert result.failed_collections == (Collection.INCIDENTS,)
    assert result.took_ms < 2000
    timeouts = retriever.metrics.registry.get_sample_value(
        "retrieval_collection_errors_total", {"collection": "incidents", "reason": "timeout"}
    )
    assert timeouts == 1


async def test_search_fails_clearly_when_every_collection_fails(
    index: FakeAsyncIndex, inference: FakeAsyncInference
) -> None:
    index.failing.update({"runbooks", "incidents"})
    retriever = make_retriever(index, inference=inference)

    with pytest.raises(RetrievalError):
        await retriever.search(request(), role=Role.VIEWER)
    failed = retriever.metrics.registry.get_sample_value(
        "retrieval_searches_total", {"outcome": "failed"}
    )
    assert failed == 1


async def test_search_fails_clearly_when_the_question_cannot_be_embedded(
    index: FakeAsyncIndex,
) -> None:
    inference = FakeAsyncInference(embed_error=ConnectionError("down"))

    with pytest.raises(RetrievalError):
        await make_retriever(index, inference=inference).search(request(), role=Role.VIEWER)
    assert index.queries == []


async def test_cancelling_a_search_cancels_its_queries(
    index: FakeAsyncIndex, inference: FakeAsyncInference
) -> None:
    """For example when the user closes the page while the answer is streaming."""
    index.slow.update({"runbooks", "incidents"})
    task = asyncio.create_task(
        make_retriever(index, inference=inference).search(request(), role=Role.VIEWER)
    )
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)


async def test_a_record_with_broken_metadata_is_skipped(
    index: FakeAsyncIndex, inference: FakeAsyncInference
) -> None:
    broken = make_record(make_chunk("RB-OLD"), dense=unit_vector(0, 0.99))
    index.add("runbooks", broken)
    # Simulate a record written by an older ingestion, without a title.
    index.namespaces["runbooks"]["RB-OLD#0"] = broken.__class__(
        id=broken.id,
        dense=broken.dense,
        sparse=broken.sparse,
        metadata={k: v for k, v in broken.metadata.items() if k != "title"},
    )
    retriever = make_retriever(index, inference=inference)

    result = await retriever.search(request(), role=Role.VIEWER)

    assert "RB-OLD" not in result.doc_ids()
    dropped = retriever.metrics.registry.get_sample_value(
        "retrieval_dropped_records_total", {"reason": "invalid_metadata"}
    )
    assert dropped == 1


# ---------------------------------------------------------------
# Re-ranking
# ---------------------------------------------------------------


async def test_reranker_order_is_used(index: FakeAsyncIndex) -> None:
    # RB-2 has the lowest hybrid score but is the only chunk with both words.
    index.add(
        "runbooks",
        make_record(make_chunk("RB-2", text="payhub pool steps"), dense=unit_vector(0, 0.2)),
    )
    inference = FakeAsyncInference(vectors={QUESTION: unit_vector(0)})
    index.namespaces["runbooks"]["RB-1#0"] = make_record(
        make_chunk("RB-1", text="unrelated"), dense=unit_vector(0, 0.9)
    )

    result = await make_retriever(index, inference=inference, rerank=True).search(
        request(), role=Role.VIEWER
    )

    assert result.rerank is RerankStatus.APPLIED
    assert result.doc_ids()[0] == "RB-2"
    assert result.hits[0].rerank_score is not None
    sent = inference.rerank_calls[0]
    assert sent["documents"][0]["text"].startswith("Title of RB-1 - Steps\n\n")


async def test_reranker_failure_keeps_the_hybrid_order(index: FakeAsyncIndex) -> None:
    inference = FakeAsyncInference(
        vectors={QUESTION: unit_vector(0)}, rerank_error=RuntimeError("quota used up")
    )
    retriever = make_retriever(index, inference=inference, rerank=True)

    result = await retriever.search(request(), role=Role.VIEWER)

    assert result.rerank is RerankStatus.FAILED
    assert result.doc_ids() == ["RB-1", "INC-1", "RB-2"]
    assert all(h.rerank_score is None for h in result.hits)
    failed = retriever.metrics.registry.get_sample_value(
        "retrieval_rerank_total", {"status": "failed"}
    )
    assert failed == 1


async def test_reranker_can_be_switched_off(
    index: FakeAsyncIndex, inference: FakeAsyncInference
) -> None:
    result = await make_retriever(
        index, inference=inference, rerank=True, rerank_enabled=False
    ).search(request(), role=Role.VIEWER)
    assert result.rerank is RerankStatus.DISABLED
    assert inference.rerank_calls == []


async def test_reranker_is_not_called_for_a_single_candidate(
    index: FakeAsyncIndex, inference: FakeAsyncInference
) -> None:
    result = await make_retriever(index, inference=inference, rerank=True).search(
        request(collections=(Collection.INCIDENTS,)), role=Role.VIEWER
    )
    assert result.rerank is RerankStatus.NOT_NEEDED
    assert inference.rerank_calls == []


async def test_only_the_best_candidates_go_to_the_reranker(
    index: FakeAsyncIndex, inference: FakeAsyncInference
) -> None:
    await make_retriever(index, inference=inference, rerank=True, rerank_candidates=2).search(
        request(top_k=1), role=Role.VIEWER
    )
    assert len(inference.rerank_calls[0]["documents"]) == 2


# ---------------------------------------------------------------
# The request
# ---------------------------------------------------------------


@pytest.mark.parametrize("query", ["", "   ", "x" * (MAX_QUERY_CHARS + 1)])
def test_empty_or_huge_questions_are_rejected(query: str) -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query=query)


def test_no_collections_is_rejected_and_repeats_are_removed() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query="q", collections=())
    repeated = SearchRequest(query="q", collections=(Collection.RUNBOOKS, Collection.RUNBOOKS))
    assert repeated.collections == (Collection.RUNBOOKS,)


def test_a_request_cannot_carry_a_role() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query="q", role="admin")  # type: ignore[call-arg]


def test_a_hit_knows_its_citation() -> None:
    citation = hit("RB-001", 0.5).citation
    assert citation.label == "RB-001 (Title of RB-001), section: Steps"
    assert citation.source_path == "data/sample_docs/runbooks/RB-001.md"


async def test_a_bug_is_raised_not_hidden_as_a_failed_collection(
    inference: FakeAsyncInference,
) -> None:
    class BadResponseIndex(FakeAsyncIndex):
        async def query(self, **kwargs: Any) -> Any:
            return object()  # no `.matches`: a programming error, not an outage

    with pytest.raises(AttributeError):
        await make_retriever(BadResponseIndex(), inference=inference).search(
            request(), role=Role.VIEWER
        )


async def test_reranker_gets_at_least_top_k_candidates(
    index: FakeAsyncIndex, inference: FakeAsyncInference
) -> None:
    result = await make_retriever(
        index, inference=inference, rerank=True, rerank_candidates=1
    ).search(request(top_k=3), role=Role.VIEWER)
    assert len(inference.rerank_calls[0]["documents"]) == 3
    assert len(result.hits) == 3
