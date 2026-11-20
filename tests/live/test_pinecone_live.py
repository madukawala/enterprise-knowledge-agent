"""Calls the real Pinecone service. Run with `make test-live`.

Uses your PINECONE_API_KEY from .env and a tiny amount of the free
embedding quota. Nothing is written to any index.
"""

import pytest
from pinecone import Pinecone

from core.config import Settings
from rag.embeddings import PineconeEmbedder

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
