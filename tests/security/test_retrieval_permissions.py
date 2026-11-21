"""Search never returns a chunk above the user's access level.

The fixture documents hold one internal runbook, one confidential incident
and one restricted policy. They go through the real ingestion pipeline into
an in-memory index that really applies Pinecone-style filters.
"""

import json
from pathlib import Path

import pytest

from core.config import LogFormat
from core.logging import configure_logging
from rag.bm25 import BM25Encoder
from rag.filters import SearchFilters
from rag.retrieval import SearchRequest
from security.roles import AccessLevel, Role, allowed_access_levels
from tests.helpers.pinecone import FakeAsyncIndex
from tests.helpers.search import index_fixture_documents, make_retriever

Indexed = tuple[FakeAsyncIndex, BM25Encoder]

SECRET_QUESTION = "privileged accounts reviewed every quarter by the security team"


@pytest.fixture
def indexed(tmp_path: Path) -> Indexed:
    return index_fixture_documents(tmp_path)


@pytest.mark.parametrize(
    ("role", "visible"),
    [
        (Role.VIEWER, {"RB-TEST-001"}),
        (Role.ANALYST, {"RB-TEST-001", "INC-TEST-001"}),
        (Role.ADMIN, {"RB-TEST-001", "INC-TEST-001", "POL-TEST-001"}),
    ],
)
async def test_each_role_finds_only_the_documents_it_may_read(
    indexed: Indexed, role: Role, visible: set[str]
) -> None:
    index, bm25 = indexed
    retriever = make_retriever(index, bm25=bm25, top_k=20)

    result = await retriever.search(SearchRequest(query="payments"), role=role)

    assert set(result.doc_ids()) == visible
    assert {hit.chunk.access_level for hit in result.hits} <= allowed_access_levels(role)


async def test_asking_about_restricted_content_by_name_returns_nothing_restricted(
    indexed: Indexed,
) -> None:
    index, bm25 = indexed
    retriever = make_retriever(index, bm25=bm25)

    by_words = await retriever.search(SearchRequest(query=SECRET_QUESTION), role=Role.VIEWER)
    by_id = await retriever.search(
        SearchRequest(query="policy", filters=SearchFilters(doc_ids=("POL-TEST-001",))),
        role=Role.VIEWER,
    )

    assert "POL-TEST-001" not in by_words.doc_ids()
    assert by_id.hits == ()
    for query in index.queries:
        assert "access_level" in json.dumps(query["filter"])


async def test_a_broken_filter_still_cannot_leak_chunks(
    indexed: Indexed, capsys: pytest.CaptureFixture[str]
) -> None:
    """Defence in depth: if Pinecone returned everything, the retriever drops
    what the role may not read and raises an alarm in the logs and metrics."""
    configure_logging("INFO", LogFormat.JSON)
    index, bm25 = indexed
    index.ignore_filter = True
    retriever = make_retriever(index, bm25=bm25, top_k=20)

    result = await retriever.search(SearchRequest(query=SECRET_QUESTION), role=Role.VIEWER)

    assert {hit.chunk.access_level for hit in result.hits} <= {
        AccessLevel.PUBLIC,
        AccessLevel.INTERNAL,
    }
    dropped = retriever.metrics.registry.get_sample_value(
        "retrieval_dropped_records_total", {"reason": "access_level_mismatch"}
    )
    assert dropped is not None
    assert dropped > 0
    assert "access_filter_mismatch" in capsys.readouterr().out


async def test_the_question_is_not_written_to_the_logs(
    indexed: Indexed, capsys: pytest.CaptureFixture[str]
) -> None:
    """Questions can contain personal data (a customer's name, an account)."""
    configure_logging("INFO", LogFormat.JSON)
    index, bm25 = indexed
    question = "why was the payment for John Smith account 12345678 declined"

    await make_retriever(index, bm25=bm25).search(SearchRequest(query=question), role=Role.ANALYST)

    output = capsys.readouterr().out
    assert "search_finished" in output
    assert "John Smith" not in output
    assert "12345678" not in output
