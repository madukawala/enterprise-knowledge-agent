"""The Pinecone filter: the access-level rule is always there, the rest is optional."""

from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from rag.filters import MAX_DOC_IDS, SearchFilters, build_filter
from rag.schema import Department, DocumentType, date_to_timestamp
from security.roles import AccessLevel, Role, allowed_access_levels
from tests.helpers.pinecone import matches_filter


def _access_rule(search_filter: dict[str, object]) -> object:
    if "$and" in search_filter:
        return search_filter["$and"][0]  # type: ignore[index]
    return search_filter


@pytest.mark.parametrize(
    ("role", "levels"),
    [
        (Role.VIEWER, ["internal", "public"]),
        (Role.ANALYST, ["confidential", "internal", "public"]),
        (Role.ADMIN, ["confidential", "internal", "public", "restricted"]),
    ],
)
def test_each_role_gets_its_own_access_levels(role: Role, levels: list[str]) -> None:
    assert build_filter(role) == {"access_level": {"$in": levels}}


def test_optional_filters_are_added_after_the_access_rule() -> None:
    search_filter = build_filter(
        Role.ANALYST,
        SearchFilters(
            departments=(Department.PAYMENTS, Department.CARDS),
            document_types=(DocumentType.INCIDENT,),
            severities=("SEV1",),
            doc_ids=("INC-2026-003",),
        ),
    )
    assert search_filter == {
        "$and": [
            {"access_level": {"$in": ["confidential", "internal", "public"]}},
            {"department": {"$in": ["cards", "payments"]}},
            {"document_type": {"$in": ["incident"]}},
            {"severity": {"$in": ["SEV1"]}},
            {"doc_id": {"$in": ["INC-2026-003"]}},
        ]
    }


def test_both_ends_of_the_date_range_are_included() -> None:
    search_filter = build_filter(
        Role.VIEWER, SearchFilters(date_from=date(2026, 1, 1), date_to=date(2026, 3, 31))
    )
    date_rule = search_filter["$and"][1]["created_ts"]

    def kept(day: date) -> bool:
        return bool(date_rule["$gte"] <= date_to_timestamp(day) < date_rule["$lt"])

    assert kept(date(2026, 1, 1))
    assert kept(date(2026, 3, 31))
    assert not kept(date(2025, 12, 31))
    assert not kept(date(2026, 4, 1))


def test_a_date_range_can_be_open_on_one_side() -> None:
    search_filter = build_filter(Role.VIEWER, SearchFilters(date_from=date(2026, 1, 1)))
    assert search_filter["$and"][1] == {"created_ts": {"$gte": date_to_timestamp(date(2026, 1, 1))}}


def test_dates_in_the_wrong_order_are_rejected() -> None:
    with pytest.raises(ValidationError, match="date_from"):
        SearchFilters(date_from=date(2026, 5, 1), date_to=date(2026, 4, 1))


def test_filters_have_no_access_level_field() -> None:
    """The level comes from the role. A request cannot ask for more."""
    with pytest.raises(ValidationError):
        SearchFilters(access_level=["restricted"])  # type: ignore[call-arg]


def test_too_many_document_ids_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SearchFilters(doc_ids=tuple(f"DOC-{n}" for n in range(MAX_DOC_IDS + 1)))


@given(
    role=st.sampled_from(Role),
    level=st.sampled_from(AccessLevel),
    departments=st.lists(st.sampled_from(Department), max_size=3),
)
def test_no_optional_filter_can_widen_access(
    role: Role, level: AccessLevel, departments: list[Department]
) -> None:
    """Whatever else is asked for, a chunk above the role's level never matches."""
    search_filter = build_filter(role, SearchFilters(departments=tuple(departments)))
    chunk = {
        "access_level": level.value,
        "department": departments[0].value if departments else "payments",
    }
    if level not in allowed_access_levels(role):
        assert not matches_filter(chunk, search_filter)
    assert _access_rule(search_filter) == {
        "access_level": {"$in": sorted(lvl.value for lvl in allowed_access_levels(role))}
    }
