import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rag.bm25 import BM25Encoder, SparseVector, term_index, tokenize

CORPUS = [
    "PayHub connection pool exhausted on salary day",
    "Card payments declined after the certificate expired",
    "FastLane timeouts caused failed instant payments",
    "PayHub month-end failures and the payhub-db pool",
]


def dot(query: SparseVector, document: SparseVector) -> float:
    weights = dict(zip(document.indices, document.values, strict=True))
    return sum(
        value * weights.get(index, 0.0)
        for index, value in zip(query.indices, query.values, strict=True)
    )


def test_tokenize_lowercases_and_drops_stop_words() -> None:
    assert tokenize("The Card IS declined") == ["card", "declined"]


def test_tokenize_keeps_codes_and_adds_their_parts() -> None:
    terms = tokenize("See INC-2026-003 and payhub-db")
    assert "inc-2026-003" in terms
    assert {"inc", "2026", "003"} <= set(terms)
    assert {"payhub-db", "payhub", "db"} <= set(terms)


def test_term_index_is_stable_and_fits_in_32_bits() -> None:
    assert term_index("payhub") == term_index("payhub")
    assert term_index("payhub") != term_index("cardswitch")
    assert 0 <= term_index("payhub") < 2**32


def test_fit_counts_documents_per_term() -> None:
    encoder = BM25Encoder.fit(CORPUS)
    assert encoder.n_docs == 4
    assert encoder.doc_freq["payhub"] == 2
    assert encoder.doc_freq["fastlane"] == 1


def test_rare_terms_get_a_higher_idf_than_common_ones() -> None:
    encoder = BM25Encoder.fit(CORPUS)
    assert encoder.idf("fastlane") > encoder.idf("payhub") > 0


def test_query_weights_add_up_to_one_and_skip_unknown_terms() -> None:
    encoder = BM25Encoder.fit(CORPUS)
    query = encoder.encode_query("payhub pool zebra")
    assert len(query.indices) == 2  # "zebra" is in no document
    assert math.isclose(sum(query.values), 1.0)


def test_query_with_no_known_terms_is_empty() -> None:
    encoder = BM25Encoder.fit(CORPUS)
    assert encoder.encode_query("the and of zebra").is_empty()


def test_matching_document_scores_highest() -> None:
    encoder = BM25Encoder.fit(CORPUS)
    query = encoder.encode_query("certificate expired")
    scores = [dot(query, vector) for vector in encoder.encode_documents(CORPUS)]
    assert scores.index(max(scores)) == 1
    assert scores[0] == 0


def test_document_vectors_are_sorted_and_positive() -> None:
    encoder = BM25Encoder.fit(CORPUS)
    vector = encoder.encode_document(CORPUS[0])
    assert vector.indices == sorted(vector.indices)
    assert all(value > 0 for value in vector.values)
    assert vector.to_pinecone() == {"indices": vector.indices, "values": vector.values}


def test_round_trip_through_a_dict_gives_the_same_vectors() -> None:
    encoder = BM25Encoder.fit(CORPUS)
    loaded = BM25Encoder.from_dict(encoder.to_dict())
    assert loaded.encode_query("payhub pool") == encoder.encode_query("payhub pool")
    assert loaded.encode_document(CORPUS[2]) == encoder.encode_document(CORPUS[2])


def test_load_reads_a_saved_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json

    path = tmp_path / "bm25.json"
    path.write_text(json.dumps(BM25Encoder.fit(CORPUS).to_dict()))
    assert BM25Encoder.load(path).n_docs == 4


def test_unknown_file_format_is_rejected() -> None:
    data = BM25Encoder.fit(CORPUS).to_dict() | {"format_version": 99}
    with pytest.raises(ValueError, match="unknown format"):
        BM25Encoder.from_dict(data)


def test_fitting_on_nothing_is_an_error() -> None:
    with pytest.raises(ValueError, match="at least one"):
        BM25Encoder.fit([])


@given(st.text(max_size=300))
def test_encoding_any_text_never_fails(text: str) -> None:
    encoder = BM25Encoder.fit(CORPUS)
    document = encoder.encode_document(text)
    query = encoder.encode_query(text)
    assert len(document.indices) == len(document.values)
    assert len(set(query.indices)) == len(query.indices)
