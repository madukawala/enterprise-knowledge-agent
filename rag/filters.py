"""Metadata filters for search, with the permission filter always included.

Every search sends one Pinecone filter. It is built here and always starts
with the access-level rule for the user's role:

    {"access_level": {"$in": ["internal", "public"]}}

so chunks above the user's level are never returned by Pinecone and never
reach the language model (ADR-0007). The optional filters (department,
document type, severity, dates, document IDs) are added after it with `$and`.

Callers pass the role, never a list of access levels. That keeps the
"which levels may this role read" decision in one place,
security/roles.py.
"""

from datetime import date, timedelta
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rag.schema import Department, DocumentType, Severity, date_to_timestamp
from security.roles import Role, allowed_access_levels

# A filter on many document IDs makes a large request. Research asks for a
# handful at a time, so this limit is generous.
MAX_DOC_IDS = 50


class SearchFilters(BaseModel):
    """Optional narrowing of a search. Empty means "no limit" for that field.

    There is deliberately no access-level field: the access level always
    comes from the user's role, never from the request or the conversation.
    `extra="forbid"` makes any attempt to pass one fail loudly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    departments: tuple[Department, ...] = ()
    document_types: tuple[DocumentType, ...] = ()
    severities: tuple[Severity, ...] = ()
    # Both ends are included: date_to=2026-03-31 keeps documents from 31 March.
    date_from: date | None = None
    date_to: date | None = None
    doc_ids: tuple[str, ...] = Field(default=(), max_length=MAX_DOC_IDS)

    @model_validator(mode="after")
    def _dates_in_order(self) -> Self:
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be on or before date_to")
        return self


def _one_of(field: str, values: tuple[str, ...]) -> dict[str, Any]:
    # Sorted, so the same request always gives the same filter (easier to
    # read in logs and traces, and stable in tests).
    return {field: {"$in": sorted(set(values))}}


def build_filter(role: Role, filters: SearchFilters | None = None) -> dict[str, Any]:
    """The Pinecone metadata filter for one search by a user with `role`."""
    filters = filters or SearchFilters()
    conditions: list[dict[str, Any]] = [
        _one_of("access_level", tuple(level.value for level in allowed_access_levels(role)))
    ]

    if filters.departments:
        conditions.append(_one_of("department", tuple(d.value for d in filters.departments)))
    if filters.document_types:
        conditions.append(_one_of("document_type", tuple(t.value for t in filters.document_types)))
    if filters.severities:
        conditions.append(_one_of("severity", filters.severities))
    if filters.doc_ids:
        conditions.append(_one_of("doc_id", filters.doc_ids))

    # Dates are stored as midnight-UTC timestamps (`created_ts`), because
    # Pinecone can only compare numbers. "Up to and including date_to" is
    # "before midnight of the next day".
    date_range: dict[str, int] = {}
    if filters.date_from:
        date_range["$gte"] = date_to_timestamp(filters.date_from)
    if filters.date_to:
        date_range["$lt"] = date_to_timestamp(filters.date_to + timedelta(days=1))
    if date_range:
        conditions.append({"created_ts": date_range})

    return conditions[0] if len(conditions) == 1 else {"$and": conditions}
