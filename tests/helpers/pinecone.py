"""In-memory stand-ins for Pinecone, so tests never use the network or quota.

- `FakeInference` replaces `Pinecone().inference` and returns made-up but
  stable vectors (the same text always gives the same vector).
- `FakeStore` behaves like `PineconeHybridStore` for the ingestion pipeline.
- `FakePineconeClient` replaces the `Pinecone` client itself, for testing
  `PineconeHybridStore` directly.
- `FakeAsyncIndex` and `FakeAsyncInference` replace the async client used
  by search. The index really scores records (dot products) and really
  applies metadata filters, so permission tests are meaningful.
"""

import asyncio
import hashlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from pinecone import NotFoundError

from rag.vector_store import VectorRecord


def fake_vector(text: str, dimension: int) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    return [digest[i % len(digest)] / 255 for i in range(dimension)]


@dataclass
class FakeInference:
    dimension: int = 8
    calls: list[dict[str, Any]] = field(default_factory=list)

    def embed(
        self, model: str, inputs: Sequence[str], parameters: Mapping[str, Any] | None = None
    ) -> list[SimpleNamespace]:
        self.calls.append({"model": model, "inputs": list(inputs), "parameters": parameters})
        return [SimpleNamespace(values=fake_vector(text, self.dimension)) for text in inputs]


@dataclass
class FakeStore:
    """Namespaces of records, kept in dictionaries."""

    namespaces: dict[str, dict[str, VectorRecord]] = field(default_factory=dict)
    index_exists: bool = False
    fail_on_upsert_to: str | None = None

    def ensure_index(self) -> bool:
        created = not self.index_exists
        self.index_exists = True
        return created

    def upsert(self, namespace: str, records: Sequence[VectorRecord]) -> None:
        if namespace == self.fail_on_upsert_to:
            raise ConnectionError("simulated Pinecone failure")
        target = self.namespaces.setdefault(namespace, {})
        for record in records:
            target[record.id] = record

    def list_ids(self, namespace: str) -> set[str]:
        return set(self.namespaces.get(namespace, {}))

    def delete_ids(self, namespace: str, ids: set[str]) -> None:
        for record_id in ids:
            self.namespaces[namespace].pop(record_id)

    def all_ids(self) -> set[str]:
        return {record_id for records in self.namespaces.values() for record_id in records}


# ---------------------------------------------------------------
# A fake `Pinecone` client for PineconeHybridStore
# ---------------------------------------------------------------


@dataclass
class FakeIndex:
    records: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    upsert_calls: int = 0
    delete_calls: int = 0
    page_size: int = 2
    missing_namespaces_raise: bool = False

    def upsert(self, *, vectors: list[dict[str, Any]], namespace: str, **_: Any) -> None:
        self.upsert_calls += 1
        target = self.records.setdefault(namespace, {})
        for vector in vectors:
            target[vector["id"]] = vector

    def list(self, *, namespace: str) -> Iterator[list[SimpleNamespace]]:
        if namespace not in self.records:
            if self.missing_namespaces_raise:
                raise NotFoundError(message="namespace not found")
            return
        ids = sorted(self.records[namespace])
        for start in range(0, len(ids), self.page_size):
            yield [SimpleNamespace(id=item) for item in ids[start : start + self.page_size]]

    def delete(self, *, ids: Sequence[str], namespace: str) -> None:
        self.delete_calls += 1
        for record_id in ids:
            self.records[namespace].pop(record_id)


@dataclass
class FakePineconeClient:
    existing: dict[str, SimpleNamespace] = field(default_factory=dict)
    created: list[dict[str, Any]] = field(default_factory=list)
    index: FakeIndex = field(default_factory=FakeIndex)

    def has_index(self, name: str) -> bool:
        return name in self.existing

    def describe_index(self, name: str) -> SimpleNamespace:
        return self.existing[name]

    def create_index(self, **kwargs: Any) -> None:
        self.created.append(kwargs)
        self.existing[kwargs["name"]] = SimpleNamespace(
            dimension=kwargs["dimension"], metric=kwargs["metric"]
        )

    def Index(self, name: str, host: str = "") -> FakeIndex:  # noqa: N802 - same name as the real client
        return self.index


# ---------------------------------------------------------------
# Async fakes for search
# ---------------------------------------------------------------


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _sparse_dot(query: Mapping[str, Any] | None, record: VectorRecord) -> float:
    if query is None or record.sparse.is_empty():
        return 0.0
    weights = dict(zip(record.sparse.indices, record.sparse.values, strict=True))
    return sum(
        value * weights.get(index, 0.0)
        for index, value in zip(query["indices"], query["values"], strict=True)
    )


def _check(value: Any, operator: str, expected: Any) -> bool:
    values = value if isinstance(value, list) else [value]
    if operator == "$eq":
        return expected in values
    if operator == "$ne":
        return expected not in values
    if operator == "$in":
        return any(item in expected for item in values)
    if operator == "$nin":
        return all(item not in expected for item in values)
    comparisons = {
        "$gt": value > expected,
        "$gte": value >= expected,
        "$lt": value < expected,
        "$lte": value <= expected,
    }
    return comparisons[operator]


def matches_filter(metadata: Mapping[str, Any], condition: Mapping[str, Any]) -> bool:
    """A small version of Pinecone's metadata filter language.

    Enough for the filters the app builds: `$and`, `$or`, `$in`, `$nin`,
    `$eq`, `$ne` and the four range operators. A list field (tags) matches
    `$in` when any of its values is in the list, as in Pinecone.
    """
    for key, rule in condition.items():
        if key == "$and":
            if not all(matches_filter(metadata, part) for part in rule):
                return False
            continue
        if key == "$or":
            if not any(matches_filter(metadata, part) for part in rule):
                return False
            continue
        if key not in metadata:
            return False
        value = metadata[key]
        rules = rule if isinstance(rule, Mapping) else {"$eq": rule}
        if not all(_check(value, op, expected) for op, expected in rules.items()):
            return False
    return True


@dataclass
class FakeAsyncIndex:
    """Behaves like Pinecone's async index for `query`.

    Scores are real dot products (dense plus sparse), and filters are
    really applied, so tests of ranking and permissions mean something.
    Numbers in the returned metadata are floats, as Pinecone returns them.
    """

    namespaces: dict[str, dict[str, VectorRecord]] = field(default_factory=dict)
    failing: set[str] = field(default_factory=set)
    slow: set[str] = field(default_factory=set)
    slow_seconds: float = 5.0
    # Simulates a bug in the filter: every record is returned.
    ignore_filter: bool = False
    queries: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_store(cls, store: FakeStore) -> "FakeAsyncIndex":
        return cls(namespaces={name: dict(records) for name, records in store.namespaces.items()})

    async def __aenter__(self) -> "FakeAsyncIndex":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def add(self, namespace: str, *records: VectorRecord) -> None:
        target = self.namespaces.setdefault(namespace, {})
        for record in records:
            target[record.id] = record

    async def query(
        self,
        *,
        top_k: int,
        vector: Sequence[float],
        namespace: str,
        filter: Mapping[str, Any],
        include_metadata: bool,
        sparse_vector: Mapping[str, Any] | None = None,
    ) -> SimpleNamespace:
        self.queries.append(
            {"namespace": namespace, "filter": filter, "top_k": top_k, "sparse": sparse_vector}
        )
        if namespace in self.slow:
            await asyncio.sleep(self.slow_seconds)
        if namespace in self.failing:
            raise ConnectionError("simulated Pinecone failure")

        scored = []
        for record in self.namespaces.get(namespace, {}).values():
            if not self.ignore_filter and not matches_filter(record.metadata, filter):
                continue
            score = _dot(vector, record.dense) + _sparse_dot(sparse_vector, record)
            scored.append((score, record))
        scored.sort(key=lambda item: (-item[0], item[1].id))
        return SimpleNamespace(
            matches=[
                SimpleNamespace(
                    id=record.id,
                    score=score,
                    metadata=_as_returned(record.metadata) if include_metadata else None,
                )
                for score, record in scored[:top_k]
            ]
        )


def _as_returned(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: float(value) if isinstance(value, int) and not isinstance(value, bool) else value
        for key, value in metadata.items()
    }


@dataclass
class FakeAsyncInference:
    """Stands in for `AsyncPinecone().inference`: embeddings and re-ranking.

    The re-ranker scores a text by how many of the question's words it
    contains, which is enough to check that its order is used.
    """

    dimension: int = 8
    embed_calls: list[dict[str, Any]] = field(default_factory=list)
    rerank_calls: list[dict[str, Any]] = field(default_factory=list)
    embed_error: Exception | None = None
    rerank_error: Exception | None = None
    vectors: dict[str, list[float]] = field(default_factory=dict)

    async def embed(
        self, model: str, inputs: Sequence[str], parameters: Mapping[str, Any] | None = None
    ) -> list[SimpleNamespace]:
        self.embed_calls.append({"model": model, "inputs": list(inputs), "parameters": parameters})
        if self.embed_error is not None:
            raise self.embed_error
        return [
            SimpleNamespace(values=self.vectors.get(text, fake_vector(text, self.dimension)))
            for text in inputs
        ]

    async def rerank(
        self,
        model: str,
        query: str,
        documents: Sequence[Mapping[str, Any]],
        rank_fields: Sequence[str] = ("text",),
        return_documents: bool = True,
        top_n: int | None = None,
        parameters: Mapping[str, Any] | None = None,
    ) -> SimpleNamespace:
        self.rerank_calls.append(
            {"model": model, "query": query, "documents": list(documents), "top_n": top_n}
        )
        if self.rerank_error is not None:
            raise self.rerank_error
        words = set(query.lower().split())
        scored = sorted(
            (
                (sum(word in doc["text"].lower() for word in words), position)
                for position, doc in enumerate(documents)
            ),
            key=lambda item: (-item[0], item[1]),
        )
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=position, score=float(score))
                for score, position in scored[: top_n or len(documents)]
            ]
        )


@dataclass
class FakeAsyncPinecone:
    """Replaces the `AsyncPinecone` class in `open_retriever`.

    Patch the class with an instance: calling it (as `AsyncPinecone(...)`)
    records the arguments and returns the same instance.
    """

    fake_index: FakeAsyncIndex = field(default_factory=FakeAsyncIndex)
    inference: FakeAsyncInference = field(default_factory=FakeAsyncInference)
    missing_index: bool = False
    opened_with: dict[str, Any] = field(default_factory=dict)
    closed: bool = False

    def __call__(self, **kwargs: Any) -> "FakeAsyncPinecone":
        self.opened_with = kwargs
        return self

    async def __aenter__(self) -> "FakeAsyncPinecone":
        return self

    async def __aexit__(self, *_: object) -> None:
        self.closed = True

    async def index(self, name: str = "", host: str = "") -> FakeAsyncIndex:
        if self.missing_index:
            raise NotFoundError(message=f"index {name} not found")
        return self.fake_index
