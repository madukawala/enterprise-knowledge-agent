"""Hybrid search over the document collections, with permission filters.

One search, step by step (docs/architecture/04-search-pipeline.md):

1. Build the Pinecone filter from the user's role and the request's
   filters (rag/filters.py). The access-level rule is always in it.
2. Encode the question twice: a dense vector (e5, as a "query") and a BM25
   sparse vector, then weight them by alpha (rag/hybrid.py).
3. Query every requested collection (namespace) at the same time with
   asyncio. Each query has a time limit. If some collections fail, the
   others still answer and the result lists what was missed.
4. Merge the matches by hybrid score, with a cap on chunks per document.
5. Re-rank the best candidates (rag/reranker.py). If the re-ranker fails,
   keep the hybrid order: a search never fails because of the re-ranker.

Every returned chunk carries its document ID, title and section, so the
answer can cite exactly where each fact came from.

The role is a separate argument and never part of `SearchRequest`, because
it must come from the validated login token, not from the user's request.
"""

import asyncio
import time
from collections import Counter
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from pinecone import AsyncPinecone, NotFoundError, RetryConfig
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from core.config import Settings
from core.errors import RetrievalError
from core.logging import get_logger
from rag.bm25 import PARAMS_FILE, BM25Encoder
from rag.embeddings import QueryEmbedder
from rag.filters import SearchFilters, build_filter
from rag.hybrid import hybrid_query_vectors
from rag.reranker import PineconeReranker
from rag.schema import ChunkMetadata, Collection
from rag.telemetry import RetrievalMetrics
from security.roles import Role, allowed_access_levels

log = get_logger(__name__)

# Questions longer than this are almost certainly pasted documents, not
# questions. The limit also keeps the question well inside the embedding
# model's 512 tokens.
MAX_QUERY_CHARS = 1000

# Search runs while a user waits for an answer, so the client retries less
# than its default (3 retries, up to 60 s back-off). The time limit on each
# call (RETRIEVAL_TIMEOUT_SECONDS) is the real bound.
_SEARCH_RETRY = RetryConfig(max_retries=2, max_wait=2.0)


# ---------------------------------------------------------------
# Request and result
# ---------------------------------------------------------------


class SearchRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    # The namespaces to search. All of them unless the caller (the retrieval
    # agent) picks the ones that fit the question.
    collections: tuple[Collection, ...] = Field(default=tuple(Collection), min_length=1)
    filters: SearchFilters = SearchFilters()
    # Leave empty to use the configured defaults.
    top_k: int | None = Field(default=None, ge=1, le=20)
    alpha: float | None = Field(default=None, gt=0, le=1)

    @field_validator("collections")
    @classmethod
    def _no_repeats(cls, value: tuple[Collection, ...]) -> tuple[Collection, ...]:
        return tuple(dict.fromkeys(value))


class Citation(BaseModel):
    """Where a chunk came from. Shown under the answer."""

    model_config = ConfigDict(frozen=True)

    doc_id: str
    title: str
    section: str
    source_path: str

    @property
    def label(self) -> str:
        return f"{self.doc_id} ({self.title}), section: {self.section}"


class SearchHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    record_id: str
    collection: Collection
    # alpha x dense score + (1 - alpha) x BM25 score, as returned by Pinecone.
    score: float
    # Set when the re-ranker ordered this hit.
    rerank_score: float | None = None
    chunk: ChunkMetadata

    @property
    def citation(self) -> Citation:
        return Citation(
            doc_id=self.chunk.doc_id,
            title=self.chunk.title,
            section=self.chunk.section,
            source_path=self.chunk.source_path,
        )


class RerankStatus(StrEnum):
    APPLIED = "applied"
    FAILED = "failed"  # error, timeout or quota: the hybrid order was kept
    DISABLED = "disabled"
    NOT_NEEDED = "not_needed"  # fewer than two candidates


class SearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    hits: tuple[SearchHit, ...]
    collections_searched: tuple[Collection, ...]
    # Collections that timed out or failed. The hits come from the others.
    failed_collections: tuple[Collection, ...] = ()
    # Matches returned by Pinecone before merging, e.g. "32 found, 8 kept"
    # in the activity panel.
    candidates: int
    alpha: float
    rerank: RerankStatus
    took_ms: int

    @property
    def partial(self) -> bool:
        return bool(self.failed_collections)

    def doc_ids(self) -> list[str]:
        """Document IDs in result order, each once."""
        return list(dict.fromkeys(hit.chunk.doc_id for hit in self.hits))


# ---------------------------------------------------------------
# Settings and the parts the retriever needs
# ---------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalConfig:
    alpha: float = 0.5
    top_k: int = 8
    candidates_per_collection: int = 20
    max_chunks_per_document: int = 3
    timeout_seconds: float = 8.0
    rerank_enabled: bool = True
    rerank_candidates: int = 30

    @classmethod
    def from_settings(cls, settings: Settings) -> "RetrievalConfig":
        return cls(
            alpha=settings.hybrid_alpha,
            top_k=settings.retrieval_top_k,
            candidates_per_collection=settings.retrieval_candidates_per_collection,
            max_chunks_per_document=settings.retrieval_max_chunks_per_document,
            timeout_seconds=settings.retrieval_timeout_seconds,
            rerank_enabled=settings.rerank_enabled,
            rerank_candidates=settings.rerank_candidates,
        )


class QueryIndex(Protocol):
    """The part of Pinecone's async index used for search. Tests pass a fake."""

    async def query(
        self,
        *,
        top_k: int,
        vector: Sequence[float],
        namespace: str,
        filter: Mapping[str, Any],  # Pinecone's own argument name
        include_metadata: bool,
        sparse_vector: Mapping[str, Any] | None = None,
    ) -> Any: ...


class DenseEncoder(Protocol):
    async def embed_query(self, text: str) -> list[float]: ...


class Reranker(Protocol):
    async def rerank(
        self, query: str, texts: Sequence[str], *, top_n: int
    ) -> list[tuple[int, float]]: ...


# ---------------------------------------------------------------
# Merging
# ---------------------------------------------------------------


def merge_hits(
    per_collection: Iterable[Sequence[SearchHit]], *, max_per_document: int
) -> list[SearchHit]:
    """Merge the hits of several collections into one list, best score first.

    Scores from different namespaces can be compared directly, because they
    all come from one index with the same encoders and the same alpha.
    At most `max_per_document` chunks of one document are kept, so a long
    document cannot push every other source out of the answer.
    """
    every_hit = [hit for hits in per_collection for hit in hits]
    # Ties are broken by record ID, so the order is always the same.
    every_hit.sort(key=lambda hit: (-hit.score, hit.record_id))

    merged: list[SearchHit] = []
    seen: set[tuple[Collection, str]] = set()
    per_document: Counter[str] = Counter()
    for hit in every_hit:
        key = (hit.collection, hit.record_id)
        if key in seen or per_document[hit.chunk.doc_id] >= max_per_document:
            continue
        seen.add(key)
        per_document[hit.chunk.doc_id] += 1
        merged.append(hit)
    return merged


def rerank_text(chunk: ChunkMetadata) -> str:
    """What the re-ranker reads for a chunk: title and section give it context."""
    return f"{chunk.title} - {chunk.section}\n\n{chunk.text}"


# ---------------------------------------------------------------
# The retriever
# ---------------------------------------------------------------


class _CollectionSearchError(Exception):
    def __init__(self, collection: Collection, reason: str) -> None:
        super().__init__(f"{collection.value}: {reason}")
        self.collection = collection
        self.reason = reason


class HybridRetriever:
    def __init__(
        self,
        *,
        index: QueryIndex,
        embedder: DenseEncoder,
        bm25: BM25Encoder,
        config: RetrievalConfig,
        reranker: Reranker | None = None,
        metrics: RetrievalMetrics | None = None,
    ) -> None:
        self._index = index
        self._embedder = embedder
        self._bm25 = bm25
        self._reranker = reranker
        self.config = config
        self.metrics = metrics or RetrievalMetrics()

    async def search(self, request: SearchRequest, *, role: Role) -> SearchResult:
        started = time.perf_counter()
        alpha = request.alpha if request.alpha is not None else self.config.alpha
        top_k = request.top_k or self.config.top_k
        metadata_filter = build_filter(role, request.filters)

        try:
            dense, sparse = await self._encode(request.query, alpha)
            outcomes = await asyncio.gather(
                *(
                    self._query_collection(
                        collection,
                        dense=dense,
                        sparse=sparse,
                        metadata_filter=metadata_filter,
                        role=role,
                    )
                    for collection in request.collections
                ),
                return_exceptions=True,
            )
        except RetrievalError:
            self._finish("failed", started)
            raise

        per_collection: list[list[SearchHit]] = []
        failed: list[Collection] = []
        for outcome in outcomes:
            if isinstance(outcome, _CollectionSearchError):
                failed.append(outcome.collection)
            elif isinstance(outcome, BaseException):
                # A bug, or the search being cancelled: never hide it.
                raise outcome
            else:
                per_collection.append(outcome)

        if len(failed) == len(request.collections):
            self._finish("failed", started)
            log.warning("search_failed", collections=[c.value for c in failed])
            raise RetrievalError()

        candidates = sum(len(hits) for hits in per_collection)
        merged = merge_hits(per_collection, max_per_document=self.config.max_chunks_per_document)
        hits, rerank_status = await self._rerank(request.query, merged, top_k)

        took_ms = self._finish("partial" if failed else "ok", started)
        self.metrics.rerank.labels(status=rerank_status.value).inc()
        log.info(
            "search_finished",
            role=role.value,
            collections=[c.value for c in request.collections],
            failed_collections=[c.value for c in failed],
            # The question itself is not logged: it may contain personal data.
            query_chars=len(request.query),
            alpha=alpha,
            candidates=candidates,
            hits=len(hits),
            rerank=rerank_status.value,
            took_ms=took_ms,
        )
        return SearchResult(
            hits=tuple(hits),
            collections_searched=request.collections,
            failed_collections=tuple(failed),
            candidates=candidates,
            alpha=alpha,
            rerank=rerank_status,
            took_ms=took_ms,
        )

    # -- steps ------------------------------------------------------

    async def _encode(
        self, query: str, alpha: float
    ) -> tuple[list[float], Mapping[str, Any] | None]:
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                dense = await self._embedder.embed_query(query)
        except Exception as error:
            # Without a query vector no collection can be searched.
            log.warning("query_embedding_failed", error_type=type(error).__name__)
            raise RetrievalError() from error

        scaled_dense, scaled_sparse = hybrid_query_vectors(
            dense, self._bm25.encode_query(query), alpha
        )
        return scaled_dense, scaled_sparse.to_pinecone() if scaled_sparse else None

    async def _query_collection(
        self,
        collection: Collection,
        *,
        dense: list[float],
        sparse: Mapping[str, Any] | None,
        metadata_filter: Mapping[str, Any],
        role: Role,
    ) -> list[SearchHit]:
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                response = await self._index.query(
                    top_k=self.config.candidates_per_collection,
                    vector=dense,
                    sparse_vector=sparse,
                    namespace=collection.value,
                    filter=metadata_filter,
                    include_metadata=True,
                )
        except TimeoutError as error:
            self._collection_failed(collection, "timeout", error)
            raise _CollectionSearchError(collection, "timeout") from error
        except Exception as error:
            self._collection_failed(collection, "error", error)
            raise _CollectionSearchError(collection, "error") from error

        return [
            hit
            for match in response.matches
            if (hit := self._to_hit(match, collection, role)) is not None
        ]

    def _to_hit(self, match: Any, collection: Collection, role: Role) -> SearchHit | None:
        try:
            chunk = ChunkMetadata.from_pinecone(match.metadata or {})
        except ValidationError:
            # For example a record written by an older version of ingestion.
            self.metrics.dropped_records.labels(reason="invalid_metadata").inc()
            log.warning("record_skipped", record_id=match.id, reason="invalid_metadata")
            return None

        # Second line of defence. Pinecone already applied the access-level
        # filter, so this only triggers if that filter has a bug. A chunk
        # the role may not read is dropped and reported, never returned.
        if chunk.access_level not in allowed_access_levels(role):
            self.metrics.dropped_records.labels(reason="access_level_mismatch").inc()
            log.error(
                "access_filter_mismatch",
                record_id=match.id,
                access_level=chunk.access_level.value,
                role=role.value,
            )
            return None

        return SearchHit(
            record_id=match.id, collection=collection, score=float(match.score), chunk=chunk
        )

    async def _rerank(
        self, query: str, merged: list[SearchHit], top_k: int
    ) -> tuple[list[SearchHit], RerankStatus]:
        if self._reranker is None or not self.config.rerank_enabled:
            return merged[:top_k], RerankStatus.DISABLED
        # Never send fewer candidates than the result needs.
        pool = merged[: max(self.config.rerank_candidates, top_k)]
        if len(pool) < 2:
            return pool[:top_k], RerankStatus.NOT_NEEDED

        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                ranked = await self._reranker.rerank(
                    query, [rerank_text(hit.chunk) for hit in pool], top_n=top_k
                )
        except Exception as error:
            # Includes the free quota running out (HTTP 429). Not worth
            # failing a search over: the hybrid order is still good.
            log.warning("rerank_skipped", error_type=type(error).__name__)
            return merged[:top_k], RerankStatus.FAILED

        return [
            pool[position].model_copy(update={"rerank_score": score}) for position, score in ranked
        ], RerankStatus.APPLIED

    # -- bookkeeping ------------------------------------------------

    def _collection_failed(self, collection: Collection, reason: str, error: Exception) -> None:
        self.metrics.collection_errors.labels(collection=collection.value, reason=reason).inc()
        log.warning(
            "collection_search_failed",
            collection=collection.value,
            reason=reason,
            error_type=type(error).__name__,
        )

    def _finish(self, outcome: str, started: float) -> int:
        elapsed = time.perf_counter() - started
        self.metrics.searches.labels(outcome=outcome).inc()
        self.metrics.duration.observe(elapsed)
        return round(elapsed * 1000)


# ---------------------------------------------------------------
# Building a retriever from the settings
# ---------------------------------------------------------------


class RetrievalSetupError(Exception):
    """Search cannot start. The message says what to do about it."""


@asynccontextmanager
async def open_retriever(
    settings: Settings, *, metrics: RetrievalMetrics | None = None
) -> AsyncIterator[HybridRetriever]:
    """Connect to Pinecone and load the BM25 parameters saved by `make ingest`.

        async with open_retriever(settings) as retriever:
            result = await retriever.search(SearchRequest(query="..."), role=Role.ANALYST)

    Connections are closed when the block ends.
    """
    if settings.pinecone_api_key is None:
        raise RetrievalSetupError("PINECONE_API_KEY is not set. Add it to .env.")
    params_path = settings.index_artifacts_dir / PARAMS_FILE
    if not params_path.is_file():
        raise RetrievalSetupError(
            f"{params_path} was not found. Run `make ingest` first: search needs the "
            "BM25 parameters it saves."
        )
    bm25 = BM25Encoder.load(params_path)

    async with AsyncPinecone(
        api_key=settings.pinecone_api_key.get_secret_value(),
        timeout=settings.retrieval_timeout_seconds,
        retry_config=_SEARCH_RETRY,
    ) as client:
        try:
            index = await client.index(name=settings.pinecone_index_name)
        except NotFoundError as error:
            raise RetrievalSetupError(
                f"The Pinecone index '{settings.pinecone_index_name}' does not exist. "
                "Run `make ingest` first."
            ) from error

        async with index:
            yield HybridRetriever(
                index=index,
                embedder=QueryEmbedder(
                    client.inference,
                    model=settings.embedding_model,
                    dimension=settings.embedding_dimension,
                ),
                bm25=bm25,
                config=RetrievalConfig.from_settings(settings),
                reranker=PineconeReranker(client.inference, model=settings.rerank_model),
                metrics=metrics,
            )
