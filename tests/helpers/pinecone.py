"""In-memory stand-ins for Pinecone, so tests never use the network or quota.

- `FakeInference` replaces `Pinecone().inference` and returns made-up but
  stable vectors (the same text always gives the same vector).
- `FakeStore` behaves like `PineconeHybridStore` for the ingestion pipeline.
- `FakePineconeClient` replaces the `Pinecone` client itself, for testing
  `PineconeHybridStore` directly.
"""

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

    def Index(self, name: str) -> FakeIndex:  # noqa: N802 - same name as the real client
        return self.index
