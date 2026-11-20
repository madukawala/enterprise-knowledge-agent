"""Build the catalog (rag/catalog.py) from the loaded documents and chunks."""

import hashlib
from collections import defaultdict
from collections.abc import Sequence

from ingestion.chunker import Chunk
from ingestion.loader import SourceDocument
from rag.catalog import Catalog, CollectionSummary, LevelSummary
from rag.schema import Collection, chunk_id
from security.roles import AccessLevel


def corpus_fingerprint(chunks: Sequence[Chunk]) -> str:
    """SHA-256 over every chunk ID, section and text, in a fixed order."""
    digest = hashlib.sha256()
    for chunk in sorted(chunks, key=lambda c: (c.doc_id, c.chunk_index)):
        for part in (chunk_id(chunk.doc_id, chunk.chunk_index), chunk.section, chunk.text):
            digest.update(part.encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest()


def build_catalog(
    documents: Sequence[SourceDocument],
    chunks_by_doc: dict[str, list[Chunk]],
    *,
    embedding_model: str,
) -> Catalog:
    grouped: dict[Collection, dict[AccessLevel, list[SourceDocument]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for document in documents:
        grouped[document.collection][document.meta.access_level].append(document)

    collections: dict[Collection, CollectionSummary] = {}
    for collection in Collection:
        levels: dict[AccessLevel, LevelSummary] = {}
        for level, docs in sorted(grouped[collection].items()):
            dates = [doc.meta.created_date for doc in docs]
            levels[level] = LevelSummary(
                documents=len(docs),
                chunks=sum(len(chunks_by_doc[doc.meta.doc_id]) for doc in docs),
                departments=sorted({doc.meta.department for doc in docs}),
                earliest=min(dates),
                latest=max(dates),
            )
        collections[collection] = CollectionSummary(
            documents=sum(level.documents for level in levels.values()),
            chunks=sum(level.chunks for level in levels.values()),
            by_access_level=levels,
        )

    all_chunks = [chunk for chunks in chunks_by_doc.values() for chunk in chunks]
    return Catalog(
        corpus_fingerprint=corpus_fingerprint(all_chunks),
        embedding_model=embedding_model,
        collections=collections,
    )
