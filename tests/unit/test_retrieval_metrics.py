"""Hit rate, MRR and recall, worked out by hand."""

import pytest

from evals.metrics import hit_at_k, recall_at_k, reciprocal_rank, score_all


def test_hit_at_k_looks_only_at_the_top_k() -> None:
    ranked = ["A", "B", "C"]
    assert hit_at_k(ranked, {"C"}, k=3) == 1.0
    assert hit_at_k(ranked, {"C"}, k=2) == 0.0


def test_reciprocal_rank_uses_the_first_good_document() -> None:
    assert reciprocal_rank(["A", "B", "C"], {"B", "C"}) == 0.5
    assert reciprocal_rank(["A"], {"Z"}) == 0.0


def test_several_chunks_of_one_document_count_once() -> None:
    ranked = ["A", "A", "A", "B"]
    assert reciprocal_rank(ranked, {"B"}) == 0.5
    assert hit_at_k(ranked, {"B"}, k=2) == 1.0


def test_recall_is_the_share_of_good_documents_found() -> None:
    assert recall_at_k(["A", "B", "X"], {"A", "B", "C", "D"}, k=3) == 0.5
    with pytest.raises(ValueError, match="relevant"):
        recall_at_k(["A"], set(), k=1)


def test_scores_are_averaged_over_questions() -> None:
    scores = score_all(
        [
            (["A", "B"], {"A"}),  # rank 1
            (["X", "B"], {"B"}),  # rank 2
            (["X", "Y"], {"Z"}),  # missed
            (["X", "Y", "Z"], {"Z"}),  # rank 3, outside k=2
        ],
        k=2,
    )
    assert scores.questions == 4
    assert scores.hit_rate == pytest.approx(2 / 4)
    assert scores.mrr == pytest.approx((1 + 0.5 + 0 + 1 / 3) / 4)
    assert scores.recall == pytest.approx(2 / 4)


def test_scoring_nothing_is_an_error() -> None:
    with pytest.raises(ValueError, match="No questions"):
        score_all([], k=5)
