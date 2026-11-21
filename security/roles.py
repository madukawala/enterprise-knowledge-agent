"""User roles and the document access levels each role may read.

The role always comes from the validated login token (security/auth.py),
never from the request body or the conversation (ADR-0007). What each role
may do (routes, tools, admin actions) is in security/policies.py; this file
only holds the document access levels, which the search needs as a set.
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class Role(StrEnum):
    """Keycloak realm roles. The values match the role names in the realm file."""

    VIEWER = "viewer"
    ANALYST = "analyst"
    ADMIN = "admin"


class AccessLevel(StrEnum):
    """Sensitivity label stored on every document chunk."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


_ALLOWED_ACCESS_LEVELS: Mapping[Role, frozenset[AccessLevel]] = MappingProxyType(
    {
        Role.VIEWER: frozenset({AccessLevel.PUBLIC, AccessLevel.INTERNAL}),
        Role.ANALYST: frozenset(
            {AccessLevel.PUBLIC, AccessLevel.INTERNAL, AccessLevel.CONFIDENTIAL}
        ),
        Role.ADMIN: frozenset(AccessLevel),
    }
)


def allowed_access_levels(role: Role) -> frozenset[AccessLevel]:
    """Return the document access levels a role may read.

    Retrieval adds this set as a filter on every search, so documents above
    the user's level never reach the language model.
    """
    return _ALLOWED_ACCESS_LEVELS[role]
