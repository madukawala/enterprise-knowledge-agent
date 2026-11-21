"""Builders for search tests: chunks, records and a ready-to-use retriever."""

from datetime import date
from pathlib import Path

from ingestion.pipeline import prepare_corpus, run_ingestion
from rag.bm25 import BM25Encoder, SparseVector
from rag.embeddings import PineconeEmbedder, QueryEmbedder
from rag.reranker import PineconeReranker
from rag.retrieval import HybridRetriever, RetrievalConfig
from rag.schema import ChunkMetadata, Collection, Department, DocumentType, chunk_id
from rag.vector_store import VectorRecord
from security.roles import AccessLevel
from tests.helpers.pinecone import FakeAsyncIndex, FakeAsyncInference, FakeInference, FakeStore

FIXTURE_DOCS = Path(__file__).parents[1] / "fixtures" / "sample_docs"
DIMENSION = 8


def make_chunk(
    doc_id: str,
    *,
    chunk_index: int = 0,
    access_level: AccessLevel = AccessLevel.INTERNAL,
    department: Department = Department.PAYMENTS,
    document_type: DocumentType = DocumentType.RUNBOOK,
    created_date: date = date(2026, 3, 1),
    text: str = "Some text.",
) -> ChunkMetadata:
    return ChunkMetadata(
        doc_id=doc_id,
        title=f"Title of {doc_id}",
        section="Steps",
        department=department,
        document_type=document_type,
        access_level=access_level,
        created_date=created_date,
        source_path=f"data/sample_docs/runbooks/{doc_id}.md",
        chunk_index=chunk_index,
        text=text,
    )


def make_record(
    chunk: ChunkMetadata, *, dense: list[float], sparse: SparseVector | None = None
) -> VectorRecord:
    return VectorRecord(
        id=chunk_id(chunk.doc_id, chunk.chunk_index),
        dense=dense,
        sparse=sparse or SparseVector(indices=[], values=[]),
        metadata=chunk.to_pinecone(),
    )


def unit_vector(position: int, scale: float = 1.0) -> list[float]:
    """A dense vector with one non-zero value, so dot products are easy to predict."""
    vector = [0.0] * DIMENSION
    vector[position] = scale
    return vector


def make_retriever(
    index: FakeAsyncIndex,
    *,
    inference: FakeAsyncInference | None = None,
    bm25: BM25Encoder | None = None,
    rerank: bool = False,
    **config: float | int | bool,
) -> HybridRetriever:
    inference = inference or FakeAsyncInference(dimension=DIMENSION)
    return HybridRetriever(
        index=index,
        embedder=QueryEmbedder(inference, model="e5", dimension=DIMENSION),
        bm25=bm25 or BM25Encoder.fit(["placeholder text"]),
        config=RetrievalConfig(**config),  # type: ignore[arg-type]
        reranker=PineconeReranker(inference, model="bge") if rerank else None,
    )


def index_fixture_documents(out_dir: Path) -> tuple[FakeAsyncIndex, BM25Encoder]:
    """Run the real ingestion pipeline on tests/fixtures into an in-memory index."""
    corpus = prepare_corpus(FIXTURE_DOCS, embedding_model="e5")
    store = FakeStore()
    run_ingestion(
        corpus,
        embedder=PineconeEmbedder(FakeInference(dimension=DIMENSION), model="e5", dimension=8),
        store=store,
        out_dir=out_dir,
    )
    return FakeAsyncIndex.from_store(store), corpus.encoder


ALL_COLLECTIONS = tuple(Collection)
