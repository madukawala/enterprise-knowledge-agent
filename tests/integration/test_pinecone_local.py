"""Search against Pinecone Local, Pinecone's emulator running in Docker.

The in-memory fakes in tests/helpers copy Pinecone's behaviour. This file
checks the real index server instead: the metadata filter syntax (the
permission filter above all), namespaces, the async client, and re-running
ingestion. No account and no quota are needed.

Opt-in, because it needs the emulator running:

    make test-pinecone-local

which starts the `pinecone-local` Compose service and sets
PINECONE_LOCAL_HOST. Without that variable every test here is skipped.

What the emulator cannot do (checked against v0.7.0 and v1.0.0.rc0):

- It only understands the older control-plane API. The SDK (10.x) creates
  and describes indexes with the newer 2026-07 API, which the emulator
  rejects. So this file creates and deletes the test index with plain HTTP
  in the older format, and uses the SDK only for reading and writing data.
- It ignores the sparse part of a query: a hybrid query scores like a
  dense-only one. So the hybrid score (alpha x dense + (1 - alpha) x BM25)
  is checked by tests/unit/test_hybrid.py and against real Pinecone in
  tests/live/test_pinecone_live.py, not here.

Pinecone Local has no hosted models either, so embeddings come from the
same fake used everywhere else in the tests. What is tested is the index,
not the model.
"""

import math
import os
import time
from collections.abc import AsyncIterator, Iterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from pinecone import AsyncPinecone, Pinecone

from ingestion.pipeline import prepare_corpus, run_ingestion
from rag.bm25 import BM25Encoder
from rag.embeddings import PineconeEmbedder, QueryEmbedder
from rag.filters import SearchFilters
from rag.retrieval import HybridRetriever, RetrievalConfig, SearchRequest
from rag.schema import Department
from rag.vector_store import METRIC, PineconeHybridStore
from security.roles import Role
from tests.helpers.pinecone import FakeAsyncInference, FakeInference, fake_vector
from tests.helpers.search import DIMENSION, FIXTURE_DOCS

LOCAL_HOST = os.environ.get("PINECONE_LOCAL_HOST", "")
INDEX_NAME = "helix-local-test"
FIXTURE_CHUNKS = 6

pytestmark = pytest.mark.skipif(
    not LOCAL_HOST, reason="Pinecone Local is not running (use make test-pinecone-local)"
)

Indexed = tuple[str, BM25Encoder]


def _http(host: str) -> str:
    # Pinecone Local reports index hosts as "localhost:5081". Without a
    # scheme the client would try TLS, which the emulator does not serve.
    return host if host.startswith("http") else f"http://{host}"


def _create_index(control: httpx.Client) -> str:
    """Create the test index in the emulator's (older) API format; return its host."""
    control.delete(f"/indexes/{INDEX_NAME}")  # left over from an interrupted run
    response = control.post(
        "/indexes",
        json={
            "name": INDEX_NAME,
            "dimension": DIMENSION,
            "metric": METRIC,
            "spec": {"serverless": {"cloud": "aws", "region": "us-east-1"}},
        },
    )
    response.raise_for_status()
    return _http(response.json()["host"])


class _LocalStore(PineconeHybridStore):
    """The real store, except that the index already exists.

    `ensure_index` uses the SDK's control-plane calls, which the emulator
    does not support (see the module docstring).
    """

    def ensure_index(self) -> bool:
        return False


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Indexed]:
    with httpx.Client(base_url=LOCAL_HOST, timeout=30) as control:
        host = _create_index(control)
        client = Pinecone(api_key="pclocal", host=LOCAL_HOST)
        store = _LocalStore(
            client,
            index_name=INDEX_NAME,
            dimension=DIMENSION,
            cloud="aws",
            region="us-east-1",
            index_host=host,
        )

        corpus = prepare_corpus(FIXTURE_DOCS, embedding_model="fake")
        embedder = PineconeEmbedder(FakeInference(dimension=DIMENSION), model="fake", dimension=8)
        out_dir: Path = tmp_path_factory.mktemp("index")
        # Twice: the second run must overwrite, not add.
        for _ in range(2):
            run_ingestion(corpus, embedder=embedder, store=store, out_dir=out_dir)
        _wait_for_count(client.Index(host=host), FIXTURE_CHUNKS)

        yield host, corpus.encoder
        control.delete(f"/indexes/{INDEX_NAME}")


def _wait_for_count(index: Any, expected: int) -> None:
    """Writes become searchable a moment after the upsert returns."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if index.describe_index_stats().total_vector_count == expected:
            return
        time.sleep(0.25)
    raise AssertionError(f"index never reached {expected} records")


@pytest.fixture
async def retriever(indexed: Indexed) -> AsyncIterator[HybridRetriever]:
    host, bm25 = indexed
    async with AsyncPinecone(api_key="pclocal", host=LOCAL_HOST) as client:
        index = await client.index(host=host)
        async with index:
            yield HybridRetriever(
                index=index,
                embedder=QueryEmbedder(
                    FakeAsyncInference(dimension=DIMENSION), model="fake", dimension=DIMENSION
                ),
                bm25=bm25,
                config=RetrievalConfig(top_k=20, rerank_enabled=False),
            )


def test_running_ingestion_twice_did_not_add_copies(indexed: Indexed) -> None:
    stats = Pinecone(api_key="pclocal", host=LOCAL_HOST).Index(host=indexed[0])
    assert stats.describe_index_stats().total_vector_count == FIXTURE_CHUNKS


@pytest.mark.parametrize(
    ("role", "visible"),
    [
        (Role.VIEWER, {"RB-TEST-001"}),
        (Role.ANALYST, {"RB-TEST-001", "INC-TEST-001"}),
        (Role.ADMIN, {"RB-TEST-001", "INC-TEST-001", "POL-TEST-001"}),
    ],
)
async def test_pinecone_applies_the_permission_filter(
    retriever: HybridRetriever, role: Role, visible: set[str]
) -> None:
    result = await retriever.search(SearchRequest(query="payments"), role=role)
    assert set(result.doc_ids()) == visible
    assert result.failed_collections == ()


async def test_department_and_date_filters_work_in_pinecone(retriever: HybridRetriever) -> None:
    cards = await retriever.search(
        SearchRequest(query="outage", filters=SearchFilters(departments=(Department.CARDS,))),
        role=Role.ADMIN,
    )
    march = await retriever.search(
        SearchRequest(
            query="outage",
            filters=SearchFilters(date_from=date(2026, 3, 2), date_to=date(2026, 3, 2)),
        ),
        role=Role.ADMIN,
    )
    assert set(cards.doc_ids()) == {"INC-TEST-001"}
    assert set(march.doc_ids()) == {"INC-TEST-001"}


async def test_dense_score_and_metadata_come_back_from_pinecone(
    retriever: HybridRetriever,
) -> None:
    """At alpha 1 no sparse part is sent, so the score is the plain dense dot product.

    Also proves the metadata survives the round trip (numbers come back as
    floats) and is read back into ChunkMetadata for citations.
    """
    question = "mTLS certificate expired"
    result = await retriever.search(SearchRequest(query=question, alpha=1), role=Role.ADMIN)

    corpus = prepare_corpus(FIXTURE_DOCS, embedding_model="fake")
    by_id = {chunk.record_id: chunk for chunk in corpus.chunks}
    query_dense = fake_vector(question, DIMENSION)

    assert len(result.hits) == FIXTURE_CHUNKS
    for hit in result.hits:
        chunk = by_id[hit.record_id]
        doc_dense = fake_vector(chunk.embedding_text, DIMENSION)
        expected = sum(q * d for q, d in zip(query_dense, doc_dense, strict=True))
        # Pinecone stores 32-bit floats, so allow a small rounding difference.
        assert math.isclose(hit.score, expected, rel_tol=1e-3, abs_tol=1e-4), hit.record_id
        assert hit.chunk == chunk.metadata
