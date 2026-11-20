"""Ingestion must not run code from documents, read files outside its folder,
guess an access level, or leak the Pinecone key."""

import os
import shutil
from pathlib import Path

import pytest
from pinecone import UnauthorizedError
from pydantic import SecretStr

import ingestion.cli as cli
from core.config import Settings
from ingestion.loader import CorpusError, load_documents, parse_document
from ingestion.pipeline import prepare_corpus
from rag.schema import Collection
from security.roles import AccessLevel, Role, allowed_access_levels
from tests.helpers.pinecone import FakeInference, FakePineconeClient

FIXTURES = Path(__file__).parents[1] / "fixtures" / "sample_docs"

RUNBOOK = """---
doc_id: RB-950
title: Harmless title
department: payments
document_type: runbook
access_level: internal
created_date: 2026-01-01
{extra}
---
# Harmless title

Text.
"""


def test_yaml_tags_that_build_python_objects_are_refused(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    text = RUNBOOK.format(extra=f'owner: !!python/object/apply:os.system ["touch {marker}"]')

    with pytest.raises(ValueError, match="not valid YAML"):
        parse_document(text, source_path="x.md", collection=Collection.RUNBOOKS)
    assert not marker.exists()


def test_a_symlink_to_a_file_outside_the_folder_is_not_read(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    shutil.copytree(FIXTURES, docs)
    secret = tmp_path / "secret.md"
    secret.write_text(RUNBOOK.format(extra="").replace("Text.", "TOP SECRET VALUE"))
    os.symlink(secret, docs / "runbooks" / "RB-950.md")

    with pytest.raises(CorpusError) as caught:
        load_documents(docs, base_dir=tmp_path)

    assert "points outside the documents folder" in str(caught.value.problems[0])


def test_a_document_without_an_access_level_stops_ingestion(tmp_path: Path) -> None:
    """Never fall back to a default level: that could make a document public."""
    docs = tmp_path / "docs"
    shutil.copytree(FIXTURES, docs)
    target = docs / "policies" / "POL-TEST-001.md"
    target.write_text(target.read_text().replace("access_level: restricted\n", ""))

    with pytest.raises(CorpusError) as caught:
        prepare_corpus(docs, embedding_model="m")
    assert "access_level" in str(caught.value.problems[0])


def test_every_chunk_keeps_its_documents_access_level() -> None:
    corpus = prepare_corpus(FIXTURES, embedding_model="m")
    levels = {c.metadata.doc_id: c.metadata.access_level for c in corpus.chunks}
    assert levels["POL-TEST-001"] is AccessLevel.RESTRICTED
    assert levels["INC-TEST-001"] is AccessLevel.CONFIDENTIAL
    for chunk in corpus.chunks:
        assert chunk.metadata.to_pinecone()["access_level"] == chunk.metadata.access_level.value


def test_catalog_lets_restricted_documents_be_hidden_from_a_viewer() -> None:
    catalog = prepare_corpus(FIXTURES, embedding_model="m").catalog
    viewer_levels = allowed_access_levels(Role.VIEWER)
    visible = {
        collection: sum(
            summary.documents
            for level, summary in catalog.collections[collection].by_access_level.items()
            if level in viewer_levels
        )
        for collection in Collection
    }
    assert visible[Collection.POLICIES] == 0  # the only policy is restricted
    assert visible[Collection.INCIDENTS] == 0  # the only incident is confidential
    assert visible[Collection.RUNBOOKS] == 1


def test_pinecone_key_is_never_printed_or_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = "pcsk_super-secret-key"

    class RejectingClient(FakePineconeClient):
        def __init__(self, api_key: str) -> None:
            super().__init__()
            self.inference = FakeInference(dimension=8)

        def has_index(self, name: str) -> bool:
            raise UnauthorizedError()

    monkeypatch.setattr(cli, "Pinecone", RejectingClient)
    settings = Settings(
        _env_file=None,
        sample_docs_dir=FIXTURES,
        index_artifacts_dir=tmp_path / "out",
        pinecone_api_key=SecretStr(key),
        embedding_dimension=8,
    )

    assert cli.main([], settings=settings) == cli.EXIT_FAILED

    captured = capsys.readouterr()
    assert key not in captured.out
    assert key not in captured.err
    assert key not in repr(settings)
    assert "Invalid or missing API key" in captured.err
    # The log line names only the error type.
    log_line = next(line for line in captured.out.splitlines() if "ingest_failed" in line)
    assert "UnauthorizedError" in log_line
