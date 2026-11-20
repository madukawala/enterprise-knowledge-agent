"""From ingested documents to cited search results, through `open_retriever`
and the search command line. Pinecone is replaced by in-memory fakes."""

from pathlib import Path

import pytest
from pydantic import SecretStr

import rag.cli as cli
import rag.retrieval as retrieval
from core.config import Settings
from rag.retrieval import RerankStatus, RetrievalSetupError, SearchRequest, open_retriever
from rag.schema import Collection
from security.roles import Role
from tests.helpers.pinecone import FakeAsyncInference, FakeAsyncPinecone
from tests.helpers.search import DIMENSION, index_fixture_documents


@pytest.fixture
def pinecone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeAsyncPinecone:
    index, _ = index_fixture_documents(tmp_path / "index")
    fake = FakeAsyncPinecone(fake_index=index, inference=FakeAsyncInference(dimension=DIMENSION))
    monkeypatch.setattr(retrieval, "AsyncPinecone", fake)
    return fake


def search_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "pinecone_api_key": SecretStr("pcsk_test-key"),
        "index_artifacts_dir": tmp_path / "index",
        "embedding_dimension": DIMENSION,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


async def test_search_returns_cited_chunks_from_the_ingested_documents(
    tmp_path: Path, pinecone: FakeAsyncPinecone
) -> None:
    async with open_retriever(search_settings(tmp_path)) as retriever:
        result = await retriever.search(
            SearchRequest(query="mTLS certificate expired on the network gateway"),
            role=Role.ANALYST,
        )

    assert result.hits
    assert result.rerank is RerankStatus.APPLIED
    # The re-ranker puts the chunk that names the certificate first.
    first = result.hits[0]
    assert first.chunk.doc_id == "INC-TEST-001"
    assert first.citation.section == "Root cause"
    assert first.citation.source_path == "tests/fixtures/sample_docs/incidents/INC-TEST-001.md"
    # Connections are closed at the end of the block.
    assert pinecone.closed


async def test_settings_reach_the_client_and_the_retriever(
    tmp_path: Path, pinecone: FakeAsyncPinecone
) -> None:
    settings = search_settings(tmp_path, hybrid_alpha=0.3, retrieval_timeout_seconds=4)
    async with open_retriever(settings) as retriever:
        result = await retriever.search(
            SearchRequest(query="payhub pool", collections=(Collection.RUNBOOKS,)),
            role=Role.VIEWER,
        )

    assert result.alpha == 0.3
    assert pinecone.opened_with["timeout"] == 4
    assert pinecone.opened_with["api_key"] == "pcsk_test-key"
    assert pinecone.inference.embed_calls[0]["model"] == "multilingual-e5-large"
    assert pinecone.inference.rerank_calls[0]["model"] == "bge-reranker-v2-m3"


async def test_search_needs_an_api_key(tmp_path: Path) -> None:
    with pytest.raises(RetrievalSetupError, match="PINECONE_API_KEY"):
        async with open_retriever(search_settings(tmp_path, pinecone_api_key=None)):
            pass


async def test_search_needs_ingestion_to_have_run(tmp_path: Path) -> None:
    settings = search_settings(tmp_path, index_artifacts_dir=tmp_path / "empty")
    with pytest.raises(RetrievalSetupError, match="make ingest"):
        async with open_retriever(settings):
            pass


async def test_a_missing_index_gives_a_clear_message(
    tmp_path: Path, pinecone: FakeAsyncPinecone
) -> None:
    pinecone.missing_index = True
    with pytest.raises(RetrievalSetupError, match="does not exist"):
        async with open_retriever(search_settings(tmp_path)):
            pass


# ---------------------------------------------------------------
# Command line
# ---------------------------------------------------------------


def test_cli_prints_ranked_citations(
    tmp_path: Path, pinecone: FakeAsyncPinecone, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(
        ["connection pool", "--role", "admin", "--no-rerank", "--show-text"],
        settings=search_settings(tmp_path),
    )

    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert 'Search as admin: "connection pool"' in out
    assert "re-ranker disabled" in out
    assert "RB-TEST-001 (Test connection pool runbook)" in out
    assert pinecone.inference.rerank_calls == []


def test_cli_searches_as_a_viewer_by_default(
    tmp_path: Path, pinecone: FakeAsyncPinecone, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(
        ["privileged accounts", "--collection", "policies"], settings=search_settings(tmp_path)
    )

    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "Search as viewer" in out
    assert "No matching chunks" in out


def test_cli_explains_missing_setup(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["q"], settings=search_settings(tmp_path, index_artifacts_dir=tmp_path))
    assert code == cli.EXIT_NOT_CONFIGURED
    assert "make ingest" in capsys.readouterr().err


def test_cli_rejects_a_bad_date_range(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(
        ["q", "--from", "2026-05-01", "--to", "2026-01-01"], settings=search_settings(tmp_path)
    )
    assert code == cli.EXIT_FAILED
    assert "date_from" in capsys.readouterr().err


def test_cli_reports_a_failed_search(
    tmp_path: Path, pinecone: FakeAsyncPinecone, capsys: pytest.CaptureFixture[str]
) -> None:
    pinecone.inference.embed_error = ConnectionError("down")
    code = cli.main(["q"], settings=search_settings(tmp_path))
    assert code == cli.EXIT_FAILED
    assert "Document search is unavailable" in capsys.readouterr().err
