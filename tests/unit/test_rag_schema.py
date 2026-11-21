from datetime import date

from rag.schema import (
    COLLECTION_BY_TYPE,
    ChunkMetadata,
    Collection,
    Department,
    DocumentType,
    chunk_id,
    date_to_timestamp,
)
from security.roles import AccessLevel


def metadata(**overrides: object) -> ChunkMetadata:
    values: dict[str, object] = {
        "doc_id": "INC-2026-003",
        "title": "Salary-day failures",
        "section": "Root cause",
        "department": Department.PAYMENTS,
        "document_type": DocumentType.INCIDENT,
        "access_level": AccessLevel.CONFIDENTIAL,
        "created_date": date(2026, 1, 30),
        "source_path": "data/sample_docs/incidents/INC-2026-003.md",
        "chunk_index": 2,
        "text": "The pool ran out.",
    }
    values.update(overrides)
    return ChunkMetadata.model_validate(values)


def test_every_document_type_has_one_collection() -> None:
    assert set(COLLECTION_BY_TYPE) == set(DocumentType)
    assert set(COLLECTION_BY_TYPE.values()) == set(Collection)


def test_chunk_ids_are_stable() -> None:
    assert chunk_id("INC-2026-003", 2) == "INC-2026-003#2"


def test_dates_become_midnight_utc_timestamps() -> None:
    assert date_to_timestamp(date(1970, 1, 2)) == 86_400


def test_pinecone_metadata_uses_plain_values() -> None:
    flat = metadata().to_pinecone()
    assert flat["access_level"] == "confidential"
    assert flat["department"] == "payments"
    assert flat["created_date"] == "2026-01-30"
    assert flat["created_ts"] == date_to_timestamp(date(2026, 1, 30))
    # Empty optional fields are left out, because Pinecone stores no nulls.
    assert "severity" not in flat
    assert "tags" not in flat


def test_optional_fields_are_kept_when_set() -> None:
    flat = metadata(severity="SEV1", tags=("payhub", "pool")).to_pinecone()
    assert flat["severity"] == "SEV1"
    assert flat["tags"] == ["payhub", "pool"]


def test_metadata_read_back_from_pinecone_matches_what_was_written() -> None:
    chunk = ChunkMetadata(
        doc_id="INC-2026-003",
        title="Salary-day failures",
        section="Root cause",
        department=Department.PAYMENTS,
        document_type=DocumentType.INCIDENT,
        access_level=AccessLevel.CONFIDENTIAL,
        created_date=date(2026, 1, 30),
        source_path="data/sample_docs/incidents/INC-2026-003.md",
        chunk_index=2,
        text="text",
        severity="SEV1",
        tags=("payhub",),
    )
    # Pinecone gives numbers back as floats.
    returned = {
        key: float(value) if isinstance(value, int) else value
        for key, value in chunk.to_pinecone().items()
    }

    assert ChunkMetadata.from_pinecone(returned) == chunk
