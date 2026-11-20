"""`python -m ingestion`, with Pinecone replaced by in-memory fakes."""

import shutil
from pathlib import Path

import pytest
from pydantic import SecretStr

import ingestion.cli as cli
from core.config import Settings
from ingestion.pipeline import BM25_FILE, CATALOG_FILE
from tests.helpers.pinecone import FakeInference, FakePineconeClient

FIXTURES = Path(__file__).parents[1] / "fixtures" / "sample_docs"


def make_settings(tmp_path: Path, api_key: str | None = None) -> Settings:
    return Settings(
        _env_file=None,
        sample_docs_dir=FIXTURES,
        index_artifacts_dir=tmp_path / "out",
        pinecone_api_key=SecretStr(api_key) if api_key is not None else None,
        embedding_dimension=8,
    )


class FakePineconeWithInference(FakePineconeClient):
    def __init__(self, api_key: str) -> None:
        super().__init__()
        self.api_key = api_key
        self.inference = FakeInference(dimension=8)


def test_dry_run_checks_documents_without_a_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--dry-run"], settings=make_settings(tmp_path)) == cli.EXIT_OK
    output = capsys.readouterr().out
    assert "incidents" in output
    assert "Total                    3" in output
    assert "Nothing was sent to Pinecone" in output
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("key", [None, ""])
def test_missing_api_key_explains_what_to_do(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], key: str | None
) -> None:
    assert cli.main([], settings=make_settings(tmp_path, key)) == cli.EXIT_NOT_CONFIGURED
    assert "PINECONE_API_KEY is not set" in capsys.readouterr().err


def test_bad_documents_are_listed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    docs = tmp_path / "docs"
    shutil.copytree(FIXTURES, docs)
    bad = docs / "runbooks" / "RB-TEST-001.md"
    bad.write_text(bad.read_text().replace("access_level: internal", "access_level: secret"))

    code = cli.main(["--dry-run", "--docs-dir", str(docs)], settings=make_settings(tmp_path))

    assert code == cli.EXIT_FAILED
    error = capsys.readouterr().err
    assert "1 problem(s)" in error
    assert "RB-TEST-001.md: access_level" in error


def test_full_run_creates_the_index_and_saves_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clients: list[FakePineconeWithInference] = []

    def fake_pinecone(api_key: str) -> FakePineconeWithInference:
        clients.append(FakePineconeWithInference(api_key))
        return clients[-1]

    monkeypatch.setattr(cli, "Pinecone", fake_pinecone)

    code = cli.main([], settings=make_settings(tmp_path, "pc-test-key"))

    assert code == cli.EXIT_OK
    assert clients[0].api_key == "pc-test-key"
    assert clients[0].created[0]["metric"] == "dotproduct"
    assert (tmp_path / "out" / BM25_FILE).exists()
    assert (tmp_path / "out" / CATALOG_FILE).exists()
    output = capsys.readouterr().out
    assert "Created the Pinecone index." in output
    assert "pc-test-key" not in output


def test_wrong_existing_index_stops_with_a_clear_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from types import SimpleNamespace

    def fake_pinecone(api_key: str) -> FakePineconeWithInference:
        client = FakePineconeWithInference(api_key)
        client.existing["helix-knowledge"] = SimpleNamespace(dimension=8, metric="cosine")
        return client

    monkeypatch.setattr(cli, "Pinecone", fake_pinecone)

    assert cli.main([], settings=make_settings(tmp_path, "k")) == cli.EXIT_FAILED
    assert "metric 'cosine'" in capsys.readouterr().err
