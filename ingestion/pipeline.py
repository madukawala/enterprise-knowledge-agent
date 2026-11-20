"""The ingestion pipeline behind `make ingest`.

Running it again with the same documents leaves the index as it was:

- record IDs are stable (`<doc_id>#<chunk_index>`), so records are
  overwritten, never duplicated;
- records whose document was deleted or got shorter are removed, so the
  index always matches the folder exactly;
- the saved files contain no timestamps, so they only change when the
  documents change.

The BM25 parameters and the catalog are saved only after Pinecone has been
updated. If an upsert fails half-way, retrieval keeps using the previous
parameters, and the next run fixes the index.

This is a batch job run from the command line, so it is written as plain
synchronous code. The API side (retrieval) uses the async client.
"""

import json
import os
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from core.logging import get_logger
from ingestion.catalog import build_catalog
from ingestion.chunker import Chunk, ChunkingConfig, chunk_document, embedding_text
from ingestion.loader import SourceDocument, load_documents
from rag.bm25 import BM25Encoder
from rag.catalog import Catalog
from rag.schema import ChunkMetadata, Collection, chunk_id
from rag.vector_store import VectorRecord

BM25_FILE = "bm25_params.json"
CATALOG_FILE = "catalog.json"

log = get_logger(__name__)


class PassageEmbedder(Protocol):
    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...


class HybridStore(Protocol):
    def ensure_index(self) -> bool: ...
    def upsert(self, namespace: str, records: Sequence[VectorRecord]) -> None: ...
    def list_ids(self, namespace: str) -> set[str]: ...
    def delete_ids(self, namespace: str, ids: set[str]) -> None: ...


@dataclass(frozen=True)
class PreparedChunk:
    """A chunk with everything needed to index it, before any vectors exist."""

    record_id: str
    collection: Collection
    embedding_text: str
    metadata: ChunkMetadata


@dataclass(frozen=True)
class PreparedCorpus:
    documents: list[SourceDocument]
    chunks: list[PreparedChunk]
    encoder: BM25Encoder
    catalog: Catalog


@dataclass
class IngestReport:
    documents: int
    chunks: int
    dry_run: bool
    index_created: bool = False
    upserted: dict[str, int] = field(default_factory=dict)
    deleted: dict[str, int] = field(default_factory=dict)


def prepare_corpus(
    docs_dir: Path,
    *,
    embedding_model: str,
    chunking: ChunkingConfig | None = None,
    base_dir: Path | None = None,
) -> PreparedCorpus:
    """Load, check and chunk every document, and fit BM25. No network calls."""
    documents = load_documents(docs_dir, base_dir=base_dir)
    chunks_by_doc: dict[str, list[Chunk]] = {
        doc.meta.doc_id: chunk_document(doc, chunking) for doc in documents
    }

    prepared: list[PreparedChunk] = []
    for document in documents:
        meta = document.meta
        for chunk in chunks_by_doc[meta.doc_id]:
            prepared.append(
                PreparedChunk(
                    record_id=chunk_id(meta.doc_id, chunk.chunk_index),
                    collection=document.collection,
                    embedding_text=embedding_text(document, chunk),
                    metadata=ChunkMetadata(
                        doc_id=meta.doc_id,
                        title=meta.title,
                        section=chunk.section,
                        department=meta.department,
                        document_type=meta.document_type,
                        access_level=meta.access_level,
                        created_date=meta.created_date,
                        source_path=document.source_path,
                        chunk_index=chunk.chunk_index,
                        text=chunk.text,
                        severity=meta.severity,
                        tags=meta.tags,
                    ),
                )
            )

    encoder = BM25Encoder.fit(chunk.embedding_text for chunk in prepared)
    catalog = build_catalog(documents, chunks_by_doc, embedding_model=embedding_model)
    return PreparedCorpus(documents=documents, chunks=prepared, encoder=encoder, catalog=catalog)


def _write_atomic(path: Path, text: str) -> None:
    """Write to a temporary file, then rename it, so readers never see half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def save_artifacts(corpus: PreparedCorpus, out_dir: Path) -> None:
    _write_atomic(out_dir / BM25_FILE, json.dumps(corpus.encoder.to_dict(), indent=1) + "\n")
    _write_atomic(out_dir / CATALOG_FILE, corpus.catalog.to_json())


def _sync_namespace(
    collection: Collection,
    chunks: list[PreparedChunk],
    *,
    corpus: PreparedCorpus,
    embedder: PassageEmbedder,
    store: HybridStore,
    report: IngestReport,
) -> None:
    namespace = collection.value
    if chunks:
        texts = [chunk.embedding_text for chunk in chunks]
        dense = embedder.embed_passages(texts)
        sparse = corpus.encoder.encode_documents(texts)
        records = [
            VectorRecord(
                id=chunk.record_id,
                dense=dense_vector,
                sparse=sparse_vector,
                metadata=chunk.metadata.to_pinecone(),
            )
            for chunk, dense_vector, sparse_vector in zip(chunks, dense, sparse, strict=True)
        ]
        store.upsert(namespace, records)

    # Anything left in the namespace that is not in this run belongs to a
    # deleted document, or to a document that now has fewer chunks.
    stale = store.list_ids(namespace) - {chunk.record_id for chunk in chunks}
    if stale:
        store.delete_ids(namespace, stale)

    report.upserted[namespace] = len(chunks)
    report.deleted[namespace] = len(stale)
    log.info("namespace_synced", namespace=namespace, upserted=len(chunks), deleted=len(stale))


def run_ingestion(
    corpus: PreparedCorpus,
    *,
    embedder: PassageEmbedder | None,
    store: HybridStore | None,
    out_dir: Path,
    dry_run: bool = False,
) -> IngestReport:
    """Push a prepared corpus to Pinecone and save the BM25 and catalog files.

    With `dry_run=True` nothing is sent and nothing is written: the run only
    proves the documents load and chunk correctly.
    """
    started = time.perf_counter()
    report = IngestReport(
        documents=len(corpus.documents), chunks=len(corpus.chunks), dry_run=dry_run
    )
    if dry_run:
        log.info("ingest_dry_run", documents=report.documents, chunks=report.chunks)
        return report
    if embedder is None or store is None:
        raise ValueError("An embedder and a store are needed unless dry_run is set.")

    report.index_created = store.ensure_index()
    for collection in Collection:
        _sync_namespace(
            collection,
            [chunk for chunk in corpus.chunks if chunk.collection is collection],
            corpus=corpus,
            embedder=embedder,
            store=store,
            report=report,
        )

    save_artifacts(corpus, out_dir)
    log.info(
        "ingest_finished",
        documents=report.documents,
        chunks=report.chunks,
        deleted=sum(report.deleted.values()),
        duration_ms=round((time.perf_counter() - started) * 1000),
    )
    return report
