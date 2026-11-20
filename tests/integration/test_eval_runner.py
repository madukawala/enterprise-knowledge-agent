"""The retrieval evaluation runner, on the fixture documents with fake Pinecone."""

from datetime import date
from pathlib import Path

import pytest
from pydantic import SecretStr

import evals.retrieval as runner
import rag.retrieval as retrieval
from core.config import Settings
from evals.dataset import load_retrieval_set
from evals.metrics import Scores
from evals.retrieval import AlphaRun, best_run, evaluate, render_report
from tests.helpers.pinecone import FakeAsyncInference, FakeAsyncPinecone
from tests.helpers.search import DIMENSION, index_fixture_documents, make_retriever

CASES = """
cases:
  - id: pool
    question: connection pool runbook payhub-db
    role: viewer
    expected: [RB-TEST-001]
  - id: certificate
    question: mTLS certificate expired network gateway
    role: analyst
    collections: [incidents]
    expected: [INC-TEST-001]
"""


@pytest.fixture
def cases_file(tmp_path: Path) -> Path:
    path = tmp_path / "cases.yaml"
    path.write_text(CASES)
    return path


async def test_evaluation_scores_every_alpha(tmp_path: Path, cases_file: Path) -> None:
    index, bm25 = index_fixture_documents(tmp_path)
    retriever = make_retriever(index, bm25=bm25)
    dataset = load_retrieval_set(cases_file)

    runs = [await evaluate(retriever, dataset, alpha=a, k=3) for a in (0.2, 0.9)]

    # Keyword-heavy questions on a tiny corpus: both runs should find both.
    for run in runs:
        assert run.scores.questions == 2
        assert run.scores.hit_rate == 1.0
    report = render_report(
        runs, k=3, rerank=False, settings=Settings(_env_file=None), today=date(2026, 11, 21)
    )
    assert "| alpha | hit@3 | MRR | recall@3 |" in report
    assert f"| {best_run(runs).alpha} (best) |" in report
    assert "| certificate | analyst | INC-TEST-001 | 1 |" in report


def test_best_alpha_prefers_mrr_then_hit_rate_then_the_middle() -> None:
    def run(alpha: float, mrr: float, hit: float) -> AlphaRun:
        return AlphaRun(alpha=alpha, scores=Scores(hit, mrr, 1.0, 1), outcomes=[])

    assert best_run([run(0.3, 0.8, 1.0), run(0.7, 0.9, 0.5)]).alpha == 0.7
    assert best_run([run(0.3, 0.9, 0.5), run(0.7, 0.9, 1.0)]).alpha == 0.7
    assert best_run([run(0.3, 0.9, 1.0), run(0.5, 0.9, 1.0)]).alpha == 0.5


def test_cli_writes_the_report(
    tmp_path: Path,
    cases_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    index, _ = index_fixture_documents(tmp_path / "index")
    fake = FakeAsyncPinecone(fake_index=index, inference=FakeAsyncInference(dimension=DIMENSION))
    monkeypatch.setattr(retrieval, "AsyncPinecone", fake)
    settings = Settings(
        _env_file=None,
        pinecone_api_key=SecretStr("pcsk_test"),
        index_artifacts_dir=tmp_path / "index",
        embedding_dimension=DIMENSION,
    )
    report = tmp_path / "docs" / "retrieval.md"

    code = runner.main(
        ["--alphas", "0.5,0.8", "--k", "3", "--dataset", str(cases_file), "--report", str(report)],
        settings=settings,
    )

    assert code == runner.EXIT_OK
    assert report.read_text().startswith("# Retrieval evaluation")
    assert fake.inference.rerank_calls == []  # off unless --rerank
    # One embedding per question: the second alpha reuses the cached vectors.
    assert len(fake.inference.embed_calls) == 2
    assert "Saved" in capsys.readouterr().out


def test_cli_rejects_a_bad_alpha(capsys: pytest.CaptureFixture[str]) -> None:
    assert runner.main(["--alphas", "0,0.5"], settings=Settings(_env_file=None)) == 1
    assert "alpha" in capsys.readouterr().err


def test_cli_needs_ingestion_first(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings(
        _env_file=None, pinecone_api_key=SecretStr("k"), index_artifacts_dir=tmp_path
    )
    assert runner.main([], settings=settings) == runner.EXIT_NOT_CONFIGURED
    assert "make ingest" in capsys.readouterr().err
