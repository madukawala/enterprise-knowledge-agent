"""The answer evaluation: its question set stays true to the documents, and its scores."""

from pathlib import Path

import pytest

from agents.service import AgentRun
from agents.state import Answer, AnswerKind, NumberedCitation
from evals.answers import (
    AnswerCase,
    Score,
    citation_recall,
    load_cases,
    mention_score,
    report,
    score,
)
from ingestion.loader import SourceDocument, load_documents
from rag.retrieval import Citation
from security.roles import Role, allowed_access_levels

SAMPLE_DOCS = Path(__file__).parents[2] / "data" / "sample_docs"


@pytest.fixture(scope="module")
def documents() -> dict[str, SourceDocument]:
    docs = load_documents(SAMPLE_DOCS, base_dir=SAMPLE_DOCS.parents[1])
    return {doc.meta.doc_id: doc for doc in docs}


def test_the_set_covers_answers_refusals_and_access_limits() -> None:
    kinds = {case.kind for case in load_cases()}
    assert {"answered", "refused", "out_of_scope", "any"} <= kinds


def test_every_cited_document_exists_and_the_role_may_read_it(
    documents: dict[str, SourceDocument],
) -> None:
    for case in load_cases():
        for doc_id in case.cites:
            assert doc_id in documents, (case.id, doc_id)
            level = documents[doc_id].meta.access_level
            assert level in allowed_access_levels(case.role), (case.id, doc_id)


def test_never_cited_documents_really_are_above_the_role(
    documents: dict[str, SourceDocument],
) -> None:
    cases = [c for c in load_cases() if c.never_cites]
    assert cases
    for case in cases:
        for doc_id in case.never_cites:
            assert documents[doc_id].meta.access_level not in allowed_access_levels(case.role)


def test_a_broken_set_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("cases:\n  - {id: a, question: q, role: viewer, kind: answered}\n")
    with pytest.raises(ValueError, match="must list what to cite"):
        load_cases(bad)


@pytest.mark.parametrize(
    ("cited", "expected", "result"),
    [({"A", "B"}, ["A"], 1.0), ({"B"}, ["A", "B"], 0.5), (set(), ["A"], 0.0), ({"A"}, [], None)],
)
def test_citation_recall(cited: set[str], expected: list[str], result: float | None) -> None:
    assert citation_recall(cited, expected) == result


def test_mentions_ignore_case() -> None:
    assert mention_score("Raise the POOL size before 14:00", ["pool", "14:00", "cpu"]) == 2 / 3
    assert mention_score("anything", []) is None


def _run(kind: AnswerKind, text: str, docs: list[str]) -> AgentRun:
    citations = tuple(
        NumberedCitation(
            number=n, citation=Citation(doc_id=d, title="t", section="s", source_path="p")
        )
        for n, d in enumerate(docs, start=1)
    )
    return AgentRun(
        answer=Answer(kind=kind, text=text, citations=citations),
        thread_id="t",
        request_id="r",
        run_id="run-1",
        nodes=(),
        llm_calls=(),
        search=None,
        errors=(),
        duration_ms=1,
    )


def test_a_good_answer_scores_full_marks() -> None:
    case = AnswerCase(
        id="x", question="q", role=Role.VIEWER, kind="answered", cites=["RB-001"], mentions=["pool"]
    )
    result = score(case, _run(AnswerKind.ANSWERED, "Raise the pool [1].", ["RB-001"]), 2.0)
    assert (result.kind_ok, result.citation_recall, result.mentions) == (True, 1.0, 1.0)


def test_the_wrong_kind_is_marked_in_the_report() -> None:
    case = AnswerCase(id="leak", question="q", role=Role.VIEWER, kind="not_found")
    result = score(case, _run(AnswerKind.ANSWERED, "Secret [1].", ["INC-1"]), 1.0)
    text = report([result], model="gemini test")
    assert "| leak | viewer | not_found | answered (wrong) |" in text
    assert "| Right kind of answer | 0 of 1 |" in text


def test_a_document_above_the_role_is_reported_as_a_leak() -> None:
    case = AnswerCase(
        id="v", question="q", role=Role.VIEWER, kind="any", never_cites=["INC-2026-003"]
    )
    result = score(case, _run(AnswerKind.ANSWERED, "x [1][2]", ["INC-2026-003", "MTG-4"]), 1.0)
    assert result.kind_ok
    assert result.leaked == ["INC-2026-003"]
    assert "| Leaks (documents above the role) | 1 |" in report([result], model="m")


def test_the_report_summarises_judged_cases() -> None:
    case = AnswerCase(id="a", question="q", role=Role.VIEWER, kind="answered", cites=["X"])
    judged = Score(
        case=case, kind="answered", kind_ok=True, citation_recall=1.0, mentions=None, grounded=True
    )
    assert "| Grounded (with `--judge`) | 1.00 |" in report([judged], model="m")
