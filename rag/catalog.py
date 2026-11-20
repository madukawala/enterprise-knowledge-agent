"""A summary of what is in the index, saved by ingestion as `catalog.json`.

Research step 1 ("explore the document collections", ADR-0011) reads this
file to see what each collection holds (how many documents, which
departments, which dates) without loading any documents.

Counts are kept separately for each access level, so a user is only ever
told about documents their role may read. For example, a Viewer is not told
that restricted security documents exist.
"""

import json
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from rag.schema import Collection, Department
from security.roles import AccessLevel

FORMAT_VERSION = 1


class LevelSummary(BaseModel):
    """Documents of one access level inside one collection."""

    model_config = ConfigDict(frozen=True)

    documents: int
    chunks: int
    departments: list[Department]
    earliest: date
    latest: date


class CollectionSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    documents: int
    chunks: int
    by_access_level: dict[AccessLevel, LevelSummary]


class Catalog(BaseModel):
    model_config = ConfigDict(frozen=True)

    format_version: int = FORMAT_VERSION
    # A hash of every chunk ID and text. It changes only when the indexed
    # content changes, which makes it easy to see whether an index is stale.
    corpus_fingerprint: str
    embedding_model: str
    collections: dict[Collection, CollectionSummary]

    @property
    def total_documents(self) -> int:
        return sum(summary.documents for summary in self.collections.values())

    @property
    def total_chunks(self) -> int:
        return sum(summary.chunks for summary in self.collections.values())

    def to_json(self) -> str:
        return self.model_dump_json(indent=2) + "\n"

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format_version") != FORMAT_VERSION:
            raise ValueError("The saved catalog uses an unknown format. Run `make ingest` again.")
        return cls.model_validate(data)
