"""Collections, document types and the metadata stored with every chunk.

Ingestion writes this metadata to Pinecone and retrieval reads it back, so
both sides use the models in this file. The field list matches the table in
docs/architecture/04-search-pipeline.md.
"""

from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from security.roles import AccessLevel

Severity = Literal["SEV1", "SEV2", "SEV3"]


class Collection(StrEnum):
    """A document collection. Each one is its own Pinecone namespace (ADR-0005).

    The value is also the folder name under data/sample_docs/.
    """

    POLICIES = "policies"
    ARCHITECTURE = "architecture"
    RUNBOOKS = "runbooks"
    INCIDENTS = "incidents"
    PRODUCT_SPECS = "product-specs"
    MEETING_NOTES = "meeting-notes"


class DocumentType(StrEnum):
    POLICY = "policy"
    ARCHITECTURE = "architecture"
    RUNBOOK = "runbook"
    INCIDENT = "incident"
    PRODUCT_SPEC = "product_spec"
    MEETING_NOTES = "meeting_notes"


class Department(StrEnum):
    PAYMENTS = "payments"
    CARDS = "cards"
    CORE_BANKING = "core-banking"
    SECURITY = "security"
    HR = "hr"


# Every document type lives in exactly one collection.
COLLECTION_BY_TYPE: Mapping[DocumentType, Collection] = MappingProxyType(
    {
        DocumentType.POLICY: Collection.POLICIES,
        DocumentType.ARCHITECTURE: Collection.ARCHITECTURE,
        DocumentType.RUNBOOK: Collection.RUNBOOKS,
        DocumentType.INCIDENT: Collection.INCIDENTS,
        DocumentType.PRODUCT_SPEC: Collection.PRODUCT_SPECS,
        DocumentType.MEETING_NOTES: Collection.MEETING_NOTES,
    }
)


def chunk_id(doc_id: str, chunk_index: int) -> str:
    """Return the record ID for a chunk, for example `INC-2026-003#2`.

    The ID depends only on the document and the chunk's position, so running
    ingestion again overwrites the same records instead of adding copies.
    """
    return f"{doc_id}#{chunk_index}"


def date_to_timestamp(value: date) -> int:
    """Midnight UTC of a date as a Unix timestamp.

    Pinecone can only compare numbers in range filters (`$gte`, `$lt`), so
    dates are also stored as integers.
    """
    return int(datetime.combine(value, time.min, tzinfo=UTC).timestamp())


class ChunkMetadata(BaseModel):
    """What is stored with each vector in Pinecone.

    Used for filters (department, document type, access level, date) and
    for citations (document, title, section). `text` is the chunk itself,
    so search results can be shown and quoted without another lookup.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    doc_id: str
    title: str
    section: str
    department: Department
    document_type: DocumentType
    access_level: AccessLevel
    created_date: date
    source_path: str
    chunk_index: int = Field(ge=0)
    text: str
    severity: Severity | None = None
    tags: tuple[str, ...] = ()

    @property
    def created_ts(self) -> int:
        return date_to_timestamp(self.created_date)

    def to_pinecone(self) -> dict[str, str | int | list[str]]:
        """Flatten to the value types Pinecone accepts: strings, numbers and string lists.

        Empty optional fields are left out, because Pinecone does not store nulls.
        """
        metadata: dict[str, str | int | list[str]] = {
            "doc_id": self.doc_id,
            "title": self.title,
            "section": self.section,
            "department": self.department.value,
            "document_type": self.document_type.value,
            "access_level": self.access_level.value,
            "created_date": self.created_date.isoformat(),
            "created_ts": self.created_ts,
            "source_path": self.source_path,
            "chunk_index": self.chunk_index,
            "text": self.text,
        }
        if self.severity is not None:
            metadata["severity"] = self.severity
        if self.tags:
            metadata["tags"] = list(self.tags)
        return metadata

    @classmethod
    def from_pinecone(cls, metadata: Mapping[str, Any]) -> "ChunkMetadata":
        """Read the metadata of a search match back into this model.

        `created_ts` is dropped because it is worked out from `created_date`.
        Pinecone returns every number as a float (3.0), which the int fields
        accept. Raises `ValidationError` if a field is missing or wrong, for
        example a record written by an older version of ingestion.
        """
        fields = {key: value for key, value in metadata.items() if key != "created_ts"}
        return cls.model_validate(fields)
