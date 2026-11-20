"""The score merge: alpha x dense score + (1 - alpha) x BM25 score."""

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rag.bm25 import BM25Encoder, SparseVector
from rag.hybrid import hybrid_query_vectors


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _sparse_dot(left: SparseVector | None, right: SparseVector) -> float:
    if left is None:
        return 0.0
    weights = dict(zip(right.indices, right.values, strict=True))
    return sum(v * weights.get(i, 0.0) for i, v in zip(left.indices, left.values, strict=True))


floats = st.floats(min_value=-1, max_value=1, allow_nan=False)


@given(
    alpha=st.floats(min_value=0.01, max_value=0.99),
    query_dense=st.lists(floats, min_size=4, max_size=4),
    doc_dense=st.lists(floats, min_size=4, max_size=4),
    query_weights=st.lists(st.floats(min_value=0.01, max_value=1), min_size=1, max_size=3),
    doc_weights=st.lists(st.floats(min_value=0.01, max_value=3), min_size=1, max_size=3),
)
def test_one_dot_product_equals_the_weighted_sum_of_both_scores(
    alpha: float,
    query_dense: list[float],
    doc_dense: list[float],
    query_weights: list[float],
    doc_weights: list[float],
) -> None:
    """What Pinecone computes for a scaled query is exactly the hybrid score."""
    query_sparse = SparseVector(indices=list(range(len(query_weights))), values=query_weights)
    doc_sparse = SparseVector(indices=list(range(len(doc_weights))), values=doc_weights)

    dense, sparse = hybrid_query_vectors(query_dense, query_sparse, alpha)
    pinecone_score = _dot(dense, doc_dense) + _sparse_dot(sparse, doc_sparse)

    expected = alpha * _dot(query_dense, doc_dense) + (1 - alpha) * _sparse_dot(
        query_sparse, doc_sparse
    )
    assert math.isclose(pinecone_score, expected, rel_tol=1e-9, abs_tol=1e-9)


def test_bm25_part_is_the_real_bm25_score() -> None:
    encoder = BM25Encoder.fit(["payhub pool exhausted", "card certificate expired", "payhub"])
    doc = encoder.encode_document("payhub pool exhausted")
    query = encoder.encode_query("payhub pool")

    _, sparse = hybrid_query_vectors([1.0], query, alpha=0.25)

    assert math.isclose(_sparse_dot(sparse, doc), 0.75 * _sparse_dot(query, doc))


def test_alpha_one_means_meaning_only() -> None:
    dense, sparse = hybrid_query_vectors([0.5, 0.5], SparseVector([1], [1.0]), alpha=1)
    assert dense == [0.5, 0.5]
    assert sparse is None


def test_a_question_with_no_searchable_words_sends_no_sparse_part() -> None:
    _, sparse = hybrid_query_vectors([0.5], SparseVector([], []), alpha=0.5)
    assert sparse is None


@pytest.mark.parametrize("alpha", [0, -0.1, 1.01])
def test_alpha_outside_the_range_is_rejected(alpha: float) -> None:
    with pytest.raises(ValueError, match="alpha"):
        hybrid_query_vectors([1.0], SparseVector([1], [1.0]), alpha)
