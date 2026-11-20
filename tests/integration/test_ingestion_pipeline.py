"""The whole ingestion pipeline, from Markdown files to an (in-memory) index."""

import json
import shutil
from pathlib import Path

import pytest

from ingestion.pipeline import (
    BM25_FILE,
    CATALOG_FILE,
    PreparedCorpus,
    prepare_corpus,
    run_ingestion,
)
from rag.bm25 import BM25Encoder
from rag.catalog import Catalog
from rag.embeddings import PineconeEmbedder
from rag.schema import Collection
from security.roles import AccessLevel
from tests.helpers.pinecone import FakeInference, FakeStore

FIXTURES = Path(__file__).parents[1] / "fixtures" / "sample_docs"


@pytest.fixture
def docs_dir(tmp_path: Path) -> Path:
    target = tmp_path / "docs"
    shutil.copytree(FIXTURES, target)
    return target


def prepare(docs_dir: Path) -> PreparedCorpus:
    return prepare_corpus(docs_dir, embedding_model="test-model", base_dir=docs_dir.parent)


def ingest(docs_dir: Path, store: FakeStore, out_dir: Path) -> None:
    embedder = PineconeEmbedder(FakeInference(dimension=8), model="test-model", dimension=8)
    run_ingestion(prepare(docs_dir), embedder=embedder, store=store, out_dir=out_dir)


def test_records_land_in_their_collection_namespace(docs_dir: Path, tmp_path: Path) -> None:
    store = FakeStore()
    ingest(docs_dir, store, tmp_path / "out")

    incident = store.namespaces["incidents"]["INC-TEST-001#0"]
    assert incident.metadata["access_level"] == "confidential"
    assert incident.metadata["severity"] == "SEV1"
    assert incident.metadata["source_path"] == "docs/incidents/INC-TEST-001.md"
    assert len(incident.dense) == 8
    assert not incident.sparse.is_empty()
    # Collections with no documents get no namespace.
    assert set(store.namespaces) == {"incidents", "policies", "runbooks"}


def test_running_twice_changes_nothing(docs_dir: Path, tmp_path: Path) -> None:
    store = FakeStore()
    out_dir = tmp_path / "out"
    ingest(docs_dir, store, out_dir)
    first_records = {ns: dict(records) for ns, records in store.namespaces.items()}
    first_files = {name: (out_dir / name).read_text() for name in (BM25_FILE, CATALOG_FILE)}

    ingest(docs_dir, store, out_dir)

    assert store.namespaces == first_records
    assert {name: (out_dir / name).read_text() for name in first_files} == first_files


def test_deleted_and_shortened_documents_are_removed(docs_dir: Path, tmp_path: Path) -> None:
    store = FakeStore()
    ingest(docs_dir, store, tmp_path / "out")
    assert "INC-TEST-001#2" in store.all_ids()

    (docs_dir / "policies" / "POL-TEST-001.md").unlink()
    incident = docs_dir / "incidents" / "INC-TEST-001.md"
    incident.write_text(incident.read_text().split("## Root cause")[0])
    ingest(docs_dir, store, tmp_path / "out")

    assert not any(i.startswith("POL-TEST-001") for i in store.all_ids())
    assert {i for i in store.all_ids() if i.startswith("INC-TEST-001")} == {"INC-TEST-001#0"}


def test_saved_files_can_be_loaded_back(docs_dir: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    ingest(docs_dir, FakeStore(), out_dir)

    encoder = BM25Encoder.load(out_dir / BM25_FILE)
    assert encoder.n_docs == len(prepare(docs_dir).chunks)

    catalog = Catalog.load(out_dir / CATALOG_FILE)
    assert catalog.total_documents == 3
    assert catalog.embedding_model == "test-model"
    incidents = catalog.collections[Collection.INCIDENTS]
    assert set(incidents.by_access_level) == {AccessLevel.CONFIDENTIAL}
    assert catalog.collections[Collection.MEETING_NOTES].documents == 0
    # No leftover temporary files.
    assert sorted(p.name for p in out_dir.iterdir()) == [BM25_FILE, CATALOG_FILE]


def test_dry_run_sends_and_writes_nothing(docs_dir: Path, tmp_path: Path) -> None:
    report = run_ingestion(
        prepare(docs_dir), embedder=None, store=None, out_dir=tmp_path / "out", dry_run=True
    )
    assert report.dry_run
    assert report.documents == 3
    assert not (tmp_path / "out").exists()


def test_a_real_run_needs_an_embedder_and_a_store(docs_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="embedder and a store"):
        run_ingestion(prepare(docs_dir), embedder=None, store=FakeStore(), out_dir=tmp_path)


def test_files_are_not_saved_when_pinecone_fails(docs_dir: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    with pytest.raises(ConnectionError):
        ingest(docs_dir, FakeStore(fail_on_upsert_to="runbooks"), out_dir)
    assert not out_dir.exists()


def test_catalog_fingerprint_changes_only_with_content(docs_dir: Path) -> None:
    before = prepare(docs_dir).catalog.corpus_fingerprint
    assert prepare(docs_dir).catalog.corpus_fingerprint == before

    runbook = docs_dir / "runbooks" / "RB-TEST-001.md"
    runbook.write_text(runbook.read_text().replace("slow query", "slow report"))
    assert prepare(docs_dir).catalog.corpus_fingerprint != before


def test_unknown_catalog_format_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"format_version": 99}))
    with pytest.raises(ValueError, match="unknown format"):
        Catalog.load(path)
