import pytest

from security.roles import AccessLevel, Role, allowed_access_levels


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (Role.VIEWER, {AccessLevel.PUBLIC, AccessLevel.INTERNAL}),
        (Role.ANALYST, {AccessLevel.PUBLIC, AccessLevel.INTERNAL, AccessLevel.CONFIDENTIAL}),
        (Role.ADMIN, set(AccessLevel)),
    ],
)
def test_access_levels_match_the_documented_role_table(
    role: Role, expected: set[AccessLevel]
) -> None:
    assert allowed_access_levels(role) == expected


def test_only_admins_can_read_restricted_documents() -> None:
    readers = {role for role in Role if AccessLevel.RESTRICTED in allowed_access_levels(role)}

    assert readers == {Role.ADMIN}


def test_higher_roles_see_everything_lower_roles_see() -> None:
    viewer = allowed_access_levels(Role.VIEWER)
    analyst = allowed_access_levels(Role.ANALYST)
    admin = allowed_access_levels(Role.ADMIN)

    assert viewer < analyst < admin


def test_role_names_match_keycloak() -> None:
    assert Role("viewer") is Role.VIEWER
    with pytest.raises(ValueError, match="superuser"):
        Role("superuser")
