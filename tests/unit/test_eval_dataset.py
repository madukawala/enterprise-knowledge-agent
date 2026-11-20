"""The retrieval question set must stay true to the sample documents.

If a document is renamed, moved to a higher access level or deleted, the
evaluation would quietly score it as a miss. These checks fail first.
"""

from pathlib import Path

import pytest

from evals.dataset import RetrievalSet, load_retrieval_set
from ingestion.loader import SourceDocument, load_documents
from security.roles import allowed_access_levels

SAMPLE_DOCS = Path(__file__).parents[2] / "data" / "sample_docs"


@pytest.fixture(scope="module")
def dataset() -> RetrievalSet:
    return load_retrieval_set()


@pytest.fixture(scope="module")
def documents() -> dict[str, SourceDocument]:
    docs = load_documents(SAMPLE_DOCS, base_dir=SAMPLE_DOCS.parents[1])
    return {doc.meta.doc_id: doc for doc in docs}


def test_the_set_is_big_enough_to_mean_something(dataset: RetrievalSet) -> None:
    assert len(dataset.cases) >= 15


def test_every_expected_document_exists(
    dataset: RetrievalSet, documents: dict[str, SourceDocument]
) -> None:
    missing = {
        (case.id, doc_id)
        for case in dataset.cases
        for doc_id in case.expected
        if doc_id not in documents
    }
    assert missing == set()


def test_the_role_may_read_every_expected_document(
    dataset: RetrievalSet, documents: dict[str, SourceDocument]
) -> None:
    hidden = {
        (case.id, doc_id)
        for case in dataset.cases
        for doc_id in case.expected
        if documents[doc_id].meta.access_level not in allowed_access_levels(case.role)
    }
    assert hidden == set()


def test_expected_documents_are_in_the_collections_searched(
    dataset: RetrievalSet, documents: dict[str, SourceDocument]
) -> None:
    outside = {
        (case.id, doc_id)
        for case in dataset.cases
        for doc_id in case.expected
        if documents[doc_id].collection not in case.collections
    }
    assert outside == set()


def test_expected_documents_pass_the_case_filters(
    dataset: RetrievalSet, documents: dict[str, SourceDocument]
) -> None:
    for case in dataset.cases:
        for doc_id in case.expected:
            created = documents[doc_id].meta.created_date
            if case.filters.date_from:
                assert created >= case.filters.date_from, (case.id, doc_id)
            if case.filters.date_to:
                assert created <= case.filters.date_to, (case.id, doc_id)


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "set.yaml"
    case = "  - {id: same, question: q, role: viewer, expected: [RB-001]}\n"
    path.write_text("cases:\n" + case + case)
    with pytest.raises(ValueError, match="same id"):
        load_retrieval_set(path)
