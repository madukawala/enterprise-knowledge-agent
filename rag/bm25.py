"""BM25 keyword vectors for Pinecone's hybrid search.

BM25 is the classic keyword ranking formula. Pinecone stores a sparse
vector next to each dense vector, and the dot product of a query vector and
a document vector built here equals that document's BM25 score:

    document weight of a term = tf * (k1 + 1) / (tf + k1 * (1 - b + b * doc_len / avg_doc_len))
    query weight of a term    = idf(term), divided by the sum of the query's idf values
    idf(term)                 = log((n_docs + 1) / (doc_freq(term) + 0.5))

This is the same maths as `pinecone-text`'s BM25Encoder. We use our own
small version because `pinecone-text` downloads NLTK data the first time it
runs. That does not work in a read-only container, and it would pull a large
library into the API image (see ADR-0004).

The encoder is fitted on our own chunks during ingestion, so terms such as
"payhub" or "inc-2026-003" get sensible weights. The fitted statistics are
saved to a JSON file, and retrieval loads the same file to encode questions.
"""

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1
# Saved by ingestion in INDEX_ARTIFACTS_DIR, loaded by retrieval.
PARAMS_FILE = "bm25_params.json"

# Words, numbers and joined codes such as "payhub-db", "inc-2026-003" or
# "payments.status". The parts of a joined code are added as well, so a
# search for "payhub" also matches "payhub-db".
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-_./][a-z0-9]+)*")
_PART_RE = re.compile(r"[a-z0-9]+")

# Common English words that carry no meaning for search. A short list is
# enough, because BM25's idf already gives frequent words little weight.
_STOP_WORDS_TEXT = """
    a about after all also an and any are as at be been before being but by can could did
    do does for from had has have he her his how i if in into is it its may more most must
    no not of on or our out over she should so such than that the their them then there
    these they this those through to under up was we were what when where which while who
    will with would you your
"""
STOP_WORDS = frozenset(_STOP_WORDS_TEXT.split())


def tokenize(text: str) -> list[str]:
    """Split text into lower-case search terms, without stop words.

    No stemming: "payment" and "payments" stay different terms. The dense
    vector already matches word forms, and stemming would mangle codes.
    """
    terms: list[str] = []
    for match in _TOKEN_RE.finditer(text.lower()):
        token = match.group()
        if token not in STOP_WORDS:
            terms.append(token)
        parts = _PART_RE.findall(token)
        if len(parts) > 1:
            terms.extend(part for part in parts if part not in STOP_WORDS)
    return terms


def term_index(term: str) -> int:
    """Map a term to a fixed position in the sparse vector (a 32-bit number).

    A hash, not a vocabulary counter, so a term keeps the same position
    when the encoder is fitted again on a changed set of documents.
    """
    digest = hashlib.blake2b(term.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big")


@dataclass(frozen=True)
class SparseVector:
    """Positions and weights of the non-zero values, in the shape Pinecone expects."""

    indices: list[int]
    values: list[float]

    def is_empty(self) -> bool:
        return not self.indices

    def to_pinecone(self) -> dict[str, list[int] | list[float]]:
        return {"indices": self.indices, "values": self.values}


def _to_sparse(weights: dict[int, float]) -> SparseVector:
    ordered = sorted(weights.items())
    return SparseVector(
        indices=[index for index, _ in ordered], values=[value for _, value in ordered]
    )


class BM25Encoder:
    """Builds BM25 sparse vectors. Create one with `fit()` or `load()`."""

    def __init__(
        self,
        *,
        doc_freq: dict[str, int],
        n_docs: int,
        avg_doc_len: float,
        k1: float = 1.2,
        b: float = 0.75,
    ) -> None:
        if n_docs < 1 or avg_doc_len <= 0:
            raise ValueError("A BM25 encoder needs at least one non-empty document.")
        self.doc_freq = doc_freq
        self.n_docs = n_docs
        self.avg_doc_len = avg_doc_len
        self.k1 = k1
        self.b = b

    @classmethod
    def fit(cls, texts: Iterable[str], *, k1: float = 1.2, b: float = 0.75) -> "BM25Encoder":
        """Count how many texts contain each term, and the average text length."""
        doc_freq: Counter[str] = Counter()
        n_docs = 0
        total_len = 0
        for text in texts:
            terms = tokenize(text)
            n_docs += 1
            total_len += len(terms)
            doc_freq.update(set(terms))
        avg_doc_len = total_len / n_docs if n_docs else 0.0
        return cls(doc_freq=dict(doc_freq), n_docs=n_docs, avg_doc_len=avg_doc_len, k1=k1, b=b)

    def idf(self, term: str) -> float:
        return math.log((self.n_docs + 1) / (self.doc_freq.get(term, 0) + 0.5))

    def encode_document(self, text: str) -> SparseVector:
        """Term-frequency weights for one stored chunk."""
        counts = Counter(tokenize(text))
        doc_len = sum(counts.values())
        length_norm = 1 - self.b + self.b * doc_len / self.avg_doc_len
        weights: dict[int, float] = {}
        for term, tf in counts.items():
            index = term_index(term)
            weight = tf * (self.k1 + 1) / (tf + self.k1 * length_norm)
            # Two terms with the same hash share a position; add them up.
            weights[index] = weights.get(index, 0.0) + weight
        return _to_sparse(weights)

    def encode_documents(self, texts: Sequence[str]) -> list[SparseVector]:
        return [self.encode_document(text) for text in texts]

    def encode_query(self, text: str) -> SparseVector:
        """Idf weights for a question, scaled so they add up to 1.

        Terms that appear in no document are dropped: they cannot match
        anything and would only shrink the weight of the useful terms.
        """
        weights: dict[int, float] = {}
        for term in set(tokenize(text)):
            if term in self.doc_freq:
                index = term_index(term)
                weights[index] = weights.get(index, 0.0) + self.idf(term)
        total = sum(weights.values())
        if total <= 0:
            return SparseVector(indices=[], values=[])
        return _to_sparse({index: weight / total for index, weight in weights.items()})

    # -----------------------------------------------------------------
    # Saving and loading the fitted parameters
    # -----------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": FORMAT_VERSION,
            "k1": self.k1,
            "b": self.b,
            "n_docs": self.n_docs,
            "avg_doc_len": self.avg_doc_len,
            # Sorted, so the same documents always produce the same file.
            "doc_freq": dict(sorted(self.doc_freq.items())),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BM25Encoder":
        if data.get("format_version") != FORMAT_VERSION:
            raise ValueError(
                "The saved BM25 parameters use an unknown format. Run `make ingest` again."
            )
        return cls(
            doc_freq={str(term): int(count) for term, count in data["doc_freq"].items()},
            n_docs=int(data["n_docs"]),
            avg_doc_len=float(data["avg_doc_len"]),
            k1=float(data["k1"]),
            b=float(data["b"]),
        )

    @classmethod
    def load(cls, path: Path) -> "BM25Encoder":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
