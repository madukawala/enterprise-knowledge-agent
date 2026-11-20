"""PineconeEmbedder and PineconeHybridStore, tested against in-memory fakes."""

from types import SimpleNamespace
from typing import Any

import pytest

from rag.bm25 import SparseVector
from rag.embeddings import MAX_BATCH_SIZE, PineconeEmbedder, QueryEmbedder
from rag.reranker import PineconeReranker
from rag.vector_store import IndexConfigError, PineconeHybridStore, VectorRecord
from tests.helpers.pinecone import (
    FakeAsyncInference,
    FakeIndex,
    FakeInference,
    FakePineconeClient,
)


def store(client: FakePineconeClient) -> PineconeHybridStore:
    return PineconeHybridStore(
        client,  # type: ignore[arg-type]
        index_name="helix-test",
        dimension=4,
        cloud="aws",
        region="us-east-1",
    )


def record(record_id: str, sparse: SparseVector | None = None) -> VectorRecord:
    return VectorRecord(
        id=record_id,
        dense=[0.1, 0.2, 0.3, 0.4],
        sparse=sparse or SparseVector(indices=[7], values=[0.5]),
        metadata={"doc_id": record_id.split("#")[0]},
    )


# ---------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------


def test_passages_are_embedded_in_batches_as_passages() -> None:
    inference = FakeInference(dimension=8)
    embedder = PineconeEmbedder(inference, model="m", dimension=8, batch_size=2)

    vectors = embedder.embed_passages(["a", "b", "c"])

    assert len(vectors) == 3
    assert [len(call["inputs"]) for call in inference.calls] == [2, 1]
    assert inference.calls[0]["parameters"] == {"input_type": "passage", "truncate": "END"}


def test_wrong_dimension_is_reported_clearly() -> None:
    embedder = PineconeEmbedder(FakeInference(dimension=8), model="m", dimension=1024)
    with pytest.raises(ValueError, match="EMBEDDING_DIMENSION is 1024"):
        embedder.embed_passages(["a"])


def test_missing_embeddings_are_an_error() -> None:
    class ShortInference:
        def embed(self, **_: Any) -> list[SimpleNamespace]:
            return []

    embedder = PineconeEmbedder(ShortInference(), model="m", dimension=8)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="received 0"):
        embedder.embed_passages(["a"])


@pytest.mark.parametrize("size", [0, MAX_BATCH_SIZE + 1])
def test_batch_size_must_be_within_the_model_limit(size: int) -> None:
    with pytest.raises(ValueError, match="batch_size"):
        PineconeEmbedder(FakeInference(), model="m", dimension=8, batch_size=size)


# ---------------------------------------------------------------
# The index
# ---------------------------------------------------------------


def test_missing_index_is_created_as_a_dotproduct_serverless_index() -> None:
    client = FakePineconeClient()
    assert store(client).ensure_index() is True
    created = client.created[0]
    assert created["metric"] == "dotproduct"
    assert created["dimension"] == 4
    assert created["spec"].cloud == "aws"


def test_matching_existing_index_is_reused() -> None:
    client = FakePineconeClient(
        existing={"helix-test": SimpleNamespace(dimension=4, metric="dotproduct")}
    )
    assert store(client).ensure_index() is False
    assert client.created == []


@pytest.mark.parametrize(("dimension", "metric"), [(4, "cosine"), (1536, "dotproduct")])
def test_existing_index_with_other_settings_is_refused(dimension: int, metric: str) -> None:
    client = FakePineconeClient(
        existing={"helix-test": SimpleNamespace(dimension=dimension, metric=metric)}
    )
    with pytest.raises(IndexConfigError, match="PINECONE_INDEX_NAME"):
        store(client).ensure_index()


def test_upsert_sends_batches_and_sparse_values() -> None:
    client = FakePineconeClient()
    records = [record(f"D-1#{i}") for i in range(250)]
    store(client).upsert("runbooks", records)

    assert client.index.upsert_calls == 3
    saved = client.index.records["runbooks"]["D-1#0"]
    assert saved["sparse_values"] == {"indices": [7], "values": [0.5]}
    assert saved["metadata"] == {"doc_id": "D-1"}


def test_empty_sparse_vector_is_left_out() -> None:
    sent = record("D-1#0", SparseVector(indices=[], values=[])).to_pinecone()
    assert "sparse_values" not in sent


def test_list_ids_reads_every_page() -> None:
    client = FakePineconeClient()
    hybrid = store(client)
    hybrid.upsert("runbooks", [record(f"D-1#{i}") for i in range(5)])
    assert hybrid.list_ids("runbooks") == {f"D-1#{i}" for i in range(5)}


@pytest.mark.parametrize("raises", [False, True])
def test_unknown_namespace_is_empty(raises: bool) -> None:
    client = FakePineconeClient(index=FakeIndex(missing_namespaces_raise=raises))
    assert store(client).list_ids("never-used") == set()


def test_delete_ids_removes_records_in_batches() -> None:
    client = FakePineconeClient()
    hybrid = store(client)
    hybrid.upsert("runbooks", [record(f"D-1#{i}") for i in range(3)])
    hybrid.delete_ids("runbooks", {"D-1#1", "D-1#2"})
    assert hybrid.list_ids("runbooks") == {"D-1#0"}
    assert client.index.delete_calls == 1


# ---------------------------------------------------------------
# Search side: question embeddings and the re-ranker
# ---------------------------------------------------------------


async def test_questions_are_embedded_as_queries() -> None:
    inference = FakeAsyncInference(dimension=8)
    embedder = QueryEmbedder(inference, model="m", dimension=8)

    vector = await embedder.embed_query("why do card payments fail?")

    assert len(vector) == 8
    assert inference.embed_calls[0]["parameters"] == {"input_type": "query", "truncate": "END"}


async def test_a_repeated_question_is_not_embedded_twice() -> None:
    inference = FakeAsyncInference(dimension=8)
    embedder = QueryEmbedder(inference, model="m", dimension=8, cache_size=2)

    first = await embedder.embed_query("a")
    first.append(99.0)  # changing the returned list must not change the cache
    await embedder.embed_query("b")
    again = await embedder.embed_query("a")
    await embedder.embed_query("c")  # pushes out "b", the least recently used
    await embedder.embed_query("b")

    assert len(again) == 8
    assert [call["inputs"] for call in inference.embed_calls] == [["a"], ["b"], ["c"], ["b"]]


async def test_a_query_vector_of_the_wrong_size_is_refused() -> None:
    embedder = QueryEmbedder(FakeAsyncInference(dimension=4), model="m", dimension=8)
    with pytest.raises(ValueError, match="EMBEDDING_DIMENSION"):
        await embedder.embed_query("q")


async def test_reranker_returns_positions_and_scores_best_first() -> None:
    inference = FakeAsyncInference()
    reranker = PineconeReranker(inference, model="bge-reranker-v2-m3")

    ranked = await reranker.rerank("card certificate", ["payhub", "card certificate"], top_n=5)

    assert ranked == [(1, 2.0), (0, 0.0)]
    assert inference.rerank_calls[0]["top_n"] == 2


async def test_reranker_answer_outside_the_list_is_refused() -> None:
    class BrokenInference(FakeAsyncInference):
        async def rerank(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(data=[SimpleNamespace(index=7, score=1.0)])

    with pytest.raises(ValueError, match="outside"):
        await PineconeReranker(BrokenInference(), model="m").rerank("q", ["a"], top_n=1)


def test_store_passes_an_explicit_index_host_to_the_client() -> None:
    class RecordingClient(FakePineconeClient):
        hosts: list[str] = []  # noqa: RUF012 - test-only class attribute

        def Index(self, name: str, host: str = "") -> FakeIndex:  # noqa: N802
            RecordingClient.hosts.append(host)
            return self.index

    hybrid = PineconeHybridStore(
        RecordingClient(),  # type: ignore[arg-type]
        index_name="i",
        dimension=4,
        cloud="aws",
        region="us-east-1",
        index_host="http://localhost:5081",
    )
    hybrid.upsert("runbooks", [record("RB-1#0")])
    assert RecordingClient.hosts == ["http://localhost:5081"]
