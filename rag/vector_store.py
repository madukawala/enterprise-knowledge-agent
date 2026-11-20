"""The Pinecone hybrid index: one index, one namespace per collection.

Each record holds a dense vector, a BM25 sparse vector and the chunk
metadata (ADR-0003). The index must use the `dotproduct` metric, because
that is the only metric that adds the dense and sparse scores together.

Ingestion uses this class to create the index and keep it in sync with the
documents. Searching is added by the hybrid retrieval branch.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pinecone import NotFoundError, Pinecone, ServerlessSpec

from rag.bm25 import SparseVector

METRIC = "dotproduct"

# Pinecone limits one request to about 2 MB. 100 records with 1024 floats
# and a chunk of text each stays well under that.
UPSERT_BATCH_SIZE = 100
# The most IDs one delete request accepts.
DELETE_BATCH_SIZE = 1000
# How long to wait for a new index to become ready.
CREATE_TIMEOUT_SECONDS = 300


class IndexConfigError(Exception):
    """The index exists but was created with settings this app cannot use."""


@dataclass(frozen=True)
class VectorRecord:
    id: str
    dense: list[float]
    sparse: SparseVector
    metadata: Mapping[str, str | int | list[str]] = field(default_factory=dict)

    def to_pinecone(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "id": self.id,
            "values": self.dense,
            "metadata": dict(self.metadata),
        }
        # Pinecone rejects an empty sparse vector, so a chunk with no
        # searchable words (rare) is stored with its dense vector only.
        if not self.sparse.is_empty():
            record["sparse_values"] = self.sparse.to_pinecone()
        return record


def _batches[T](items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class PineconeHybridStore:
    def __init__(
        self,
        client: Pinecone,
        *,
        index_name: str,
        dimension: int,
        cloud: str,
        region: str,
    ) -> None:
        self._client = client
        self.index_name = index_name
        self.dimension = dimension
        self.cloud = cloud
        self.region = region
        self._index: Any = None

    def ensure_index(self) -> bool:
        """Create the index if it does not exist. Returns True when it was created.

        An existing index is checked first: one with a different dimension or
        metric would accept the upserts but give wrong search results, so it
        is better to stop with a clear message.
        """
        if self._client.has_index(self.index_name):
            description = self._client.describe_index(self.index_name)
            if description.dimension != self.dimension or description.metric != METRIC:
                raise IndexConfigError(
                    f"Pinecone index '{self.index_name}' uses dimension "
                    f"{description.dimension} and metric '{description.metric}', but this app "
                    f"needs dimension {self.dimension} and metric '{METRIC}'. Delete the index "
                    "in the Pinecone console or set PINECONE_INDEX_NAME to a new name."
                )
            return False

        self._client.create_index(
            name=self.index_name,
            dimension=self.dimension,
            metric=METRIC,
            spec=ServerlessSpec(cloud=self.cloud, region=self.region),
            timeout=CREATE_TIMEOUT_SECONDS,
        )
        return True

    def _data_plane(self) -> Any:
        if self._index is None:
            self._index = self._client.Index(name=self.index_name)
        return self._index

    def upsert(self, namespace: str, records: Sequence[VectorRecord]) -> None:
        """Insert or overwrite records. Existing records with the same ID are replaced."""
        index = self._data_plane()
        for batch in _batches(records, UPSERT_BATCH_SIZE):
            index.upsert(
                vectors=[record.to_pinecone() for record in batch],
                namespace=namespace,
                show_progress=False,
            )

    def list_ids(self, namespace: str) -> set[str]:
        """Every record ID in a namespace. An unknown namespace is simply empty."""
        index = self._data_plane()
        ids: set[str] = set()
        try:
            for page in index.list(namespace=namespace):
                ids.update(item.id for item in page)
        except NotFoundError:
            return set()
        return ids

    def delete_ids(self, namespace: str, ids: Iterable[str]) -> None:
        index = self._data_plane()
        for batch in _batches(sorted(ids), DELETE_BATCH_SIZE):
            index.delete(ids=list(batch), namespace=namespace)
