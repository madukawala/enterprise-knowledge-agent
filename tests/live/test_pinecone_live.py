"""Calls the real Pinecone service. Run with `make test-live`.

Uses your PINECONE_API_KEY from .env and a tiny amount of the free
quota. Nothing is written to any index. The search tests need `make ingest`
to have run; they are skipped otherwise.
"""

import math

import pytest
from pinecone import AsyncPinecone, Pinecone

from core.config import Settings
from rag.bm25 import PARAMS_FILE, BM25Encoder
from rag.embeddings import PineconeEmbedder, QueryEmbedder
from rag.filters import build_filter
from rag.hybrid import hybrid_query_vectors
from rag.retrieval import RerankStatus, SearchRequest, open_retriever
from rag.schema import Collection
from security.roles import AccessLevel, Role

pytestmark = pytest.mark.live


@pytest.fixture
def live_settings() -> Settings:
    settings = Settings()
    if settings.pinecone_api_key is None or not settings.pinecone_api_key.get_secret_value():
        pytest.skip("PINECONE_API_KEY is not set")
    return settings


def test_hosted_embedding_model_returns_the_configured_dimension(live_settings: Settings) -> None:
    assert live_settings.pinecone_api_key is not None
    client = Pinecone(api_key=live_settings.pinecone_api_key.get_secret_value())
    embedder = PineconeEmbedder(
        client.inference,
        model=live_settings.embedding_model,
        dimension=live_settings.embedding_dimension,
    )

    vectors = embedder.embed_passages(["Card payments were declined.", "The pool ran out."])

    assert len(vectors) == 2
    assert all(len(v) == live_settings.embedding_dimension for v in vectors)


@pytest.fixture
def ingested(live_settings: Settings) -> Settings:
    if not (live_settings.index_artifacts_dir / PARAMS_FILE).is_file():
        pytest.skip("run `make ingest` first")
    return live_settings


async def test_real_search_finds_the_runbook_with_a_citation(ingested: Settings) -> None:
    async with open_retriever(ingested) as retriever:
        result = await retriever.search(
            SearchRequest(
                query="What should I do when the PayHub database connection pool runs out?",
                collections=(Collection.RUNBOOKS, Collection.INCIDENTS),
            ),
            role=Role.VIEWER,
        )

    assert result.failed_collections == ()
    assert "RB-001" in result.doc_ids()[:3]
    assert result.rerank in {RerankStatus.APPLIED, RerankStatus.FAILED}
    assert all(
        hit.chunk.access_level in {AccessLevel.PUBLIC, AccessLevel.INTERNAL} for hit in result.hits
    )


async def test_real_search_hides_restricted_documents_from_a_viewer(ingested: Settings) -> None:
    async with open_retriever(ingested) as retriever:
        result = await retriever.search(
            SearchRequest(query="Sentinel fraud screening rules and score thresholds"),
            role=Role.VIEWER,
        )

    assert result.hits, "a viewer should still get the public and internal documents"
    assert {"ARC-007", "PS-008"}.isdisjoint(result.doc_ids())


async def test_real_pinecone_score_is_alpha_dense_plus_one_minus_alpha_bm25(
    ingested: Settings,
) -> None:
    """The score merge, checked on the real service.

    Pinecone Local ignores sparse vectors, so this is the one place the
    weighted sum is checked against Pinecone itself. The query asks for the
    stored vectors back, so the expected score can be worked out here.
    """
    assert ingested.pinecone_api_key is not None
    question = "PayHub database connection pool exhausted"
    alpha = 0.4
    bm25 = BM25Encoder.load(ingested.index_artifacts_dir / PARAMS_FILE)

    async with AsyncPinecone(api_key=ingested.pinecone_api_key.get_secret_value()) as client:
        embedder = QueryEmbedder(
            client.inference,
            model=ingested.embedding_model,
            dimension=ingested.embedding_dimension,
        )
        query_dense = await embedder.embed_query(question)
        query_sparse = bm25.encode_query(question)
        dense, sparse = hybrid_query_vectors(query_dense, query_sparse, alpha)
        assert sparse is not None

        index = await client.index(name=ingested.pinecone_index_name)
        async with index:
            response = await index.query(
                top_k=3,
                vector=dense,
                sparse_vector=sparse.to_pinecone(),
                namespace=Collection.RUNBOOKS.value,
                filter=build_filter(Role.ADMIN),
                include_values=True,
            )

    assert response.matches
    for match in response.matches:
        assert match.sparse_values is not None, "hybrid records must keep their BM25 vector"
        dense_score = sum(q * d for q, d in zip(query_dense, match.values, strict=True))
        stored = dict(zip(match.sparse_values.indices, match.sparse_values.values, strict=True))
        bm25_score = sum(
            value * stored.get(index, 0.0)
            for index, value in zip(query_sparse.indices, query_sparse.values, strict=True)
        )
        expected = alpha * dense_score + (1 - alpha) * bm25_score
        # Pinecone stores 32-bit floats, so allow a small rounding difference.
        assert math.isclose(match.score, expected, rel_tol=1e-3, abs_tol=1e-4), match.id
