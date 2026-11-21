"""Checks on the demo documents in data/sample_docs.

These keep the promises in docs/ASSUMPTIONS.md (D1-D3) true as the
documents are edited: enough documents in every collection, every access
level used, and enough payment-failure incidents in the last year for the
research example to have a real answer.
"""

from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

from ingestion.chunker import ChunkingConfig, estimate_tokens
from ingestion.pipeline import PreparedCorpus, prepare_corpus
from rag.schema import Collection, Department, DocumentType
from security.roles import AccessLevel

SAMPLE_DOCS = Path(__file__).parents[2] / "data" / "sample_docs"
# The date the demo data was written for. Fixed, so the test does not
# start failing as time passes.
REFERENCE_DATE = date(2026, 11, 20)


@pytest.fixture(scope="module")
def corpus() -> PreparedCorpus:
    return prepare_corpus(
        SAMPLE_DOCS, embedding_model="multilingual-e5-large", base_dir=SAMPLE_DOCS.parents[1]
    )


def test_corpus_size_matches_the_plan(corpus: PreparedCorpus) -> None:
    assert 60 <= len(corpus.documents) <= 80


def test_every_collection_has_documents(corpus: PreparedCorpus) -> None:
    counts = Counter(doc.collection for doc in corpus.documents)
    assert all(counts[collection] >= 8 for collection in Collection), counts


def test_every_access_level_and_department_is_used(corpus: PreparedCorpus) -> None:
    assert {doc.meta.access_level for doc in corpus.documents} == set(AccessLevel)
    assert {doc.meta.department for doc in corpus.documents} == set(Department)


def test_last_year_has_enough_payment_failure_incidents(corpus: PreparedCorpus) -> None:
    year_ago = REFERENCE_DATE - timedelta(days=365)
    payment_incidents = [
        doc
        for doc in corpus.documents
        if doc.meta.document_type is DocumentType.INCIDENT
        and doc.meta.department in {Department.PAYMENTS, Department.CARDS}
    ]
    recent = [
        doc for doc in payment_incidents if year_ago < doc.meta.created_date <= REFERENCE_DATE
    ]
    # Some older ones too, so a date filter visibly changes the answer.
    assert 15 <= len(recent) <= 20
    assert len(payment_incidents) > len(recent)


def test_incidents_have_a_severity(corpus: PreparedCorpus) -> None:
    incidents = [d for d in corpus.documents if d.meta.document_type is DocumentType.INCIDENT]
    assert all(d.meta.severity is not None for d in incidents)


def test_every_chunk_fits_the_embedding_model(corpus: PreparedCorpus) -> None:
    limit = ChunkingConfig().max_tokens
    too_long = [c.record_id for c in corpus.chunks if estimate_tokens(c.metadata.text) > limit]
    assert too_long == []


def test_chunks_point_back_to_their_file(corpus: PreparedCorpus) -> None:
    for chunk in corpus.chunks:
        assert chunk.metadata.source_path.startswith("data/sample_docs/")
        assert chunk.metadata.source_path.endswith(f"/{chunk.metadata.doc_id}.md")
