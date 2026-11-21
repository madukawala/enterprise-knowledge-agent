"""Retrieval metrics, at document level.

Each function takes the documents a search returned, best first, and the
documents a person marked as good answers. Several chunks of one document
count once, because what the answer needs is the right document.

- hit@k: 1 if any good document is in the top k, else 0. The average over
  all questions is the "hit rate".
- reciprocal rank: 1 / position of the first good document (1, 0.5, 0.33...),
  or 0 if none was returned. The average is the MRR.
- recall@k: the share of the good documents that are in the top k. Matters
  when a question has several good sources, as research questions do.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass


def _unique(ranked: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(ranked))


def hit_at_k(ranked: Sequence[str], relevant: Collection[str], k: int) -> float:
    return 1.0 if any(doc in relevant for doc in _unique(ranked)[:k]) else 0.0


def reciprocal_rank(ranked: Sequence[str], relevant: Collection[str]) -> float:
    for position, doc in enumerate(_unique(ranked), start=1):
        if doc in relevant:
            return 1.0 / position
    return 0.0


def recall_at_k(ranked: Sequence[str], relevant: Collection[str], k: int) -> float:
    if not relevant:
        raise ValueError("A question needs at least one relevant document.")
    found = set(_unique(ranked)[:k]) & set(relevant)
    return len(found) / len(set(relevant))


@dataclass(frozen=True)
class Scores:
    hit_rate: float
    mrr: float
    recall: float
    questions: int


def score_all(results: Sequence[tuple[Sequence[str], Collection[str]]], *, k: int) -> Scores:
    """Average the metrics over `(ranked, relevant)` pairs, one per question."""
    if not results:
        raise ValueError("No questions to score.")
    count = len(results)
    return Scores(
        hit_rate=sum(hit_at_k(r, rel, k) for r, rel in results) / count,
        mrr=sum(reciprocal_rank(r, rel) for r, rel in results) / count,
        recall=sum(recall_at_k(r, rel, k) for r, rel in results) / count,
        questions=count,
    )
