"""The role matrix in security/policies.py, checked against the documented table.

The expected table is written out here by hand, on purpose. If someone
changes the matrix in code, this test fails until the table (and the docs
it mirrors) is changed too.
"""

import json

import pytest

from core.config import LogFormat
from core.errors import PermissionDeniedError
from core.logging import configure_logging
from security.policies import (
    ACTION_LABELS,
    Action,
    allowed_actions,
    is_allowed,
    matrix,
    require,
)
from security.roles import Role

V, A, AD = Role.VIEWER, Role.ANALYST, Role.ADMIN

# Action -> the roles that may do it. Same as the table in docs/security/RBAC.md.
EXPECTED: dict[Action, set[Role]] = {
    Action.CHAT: {V, A, AD},
    Action.KNOWLEDGE_SEARCH: {V, A, AD},
    Action.PYTHON_ANALYSIS: {A, AD},
    Action.MCP_TOOLS: {A, AD},
    Action.RECURSIVE_RESEARCH: {A, AD},
    Action.VIEW_ACCESS_MATRIX: {AD},
    Action.VIEW_AUDIT_LOG: {AD},
    Action.MANAGE_RATE_LIMITS: {AD},
    Action.VIEW_ALL_FEEDBACK: {AD},
    Action.REINDEX: {AD},
    Action.MEMORY_CLEANUP: {AD},
}


def test_every_action_has_a_decision_in_the_expected_table() -> None:
    """A new action must be added to this table on purpose."""
    assert set(EXPECTED) == set(Action)
    assert set(ACTION_LABELS) == set(Action)


@pytest.mark.parametrize("action", list(Action))
@pytest.mark.parametrize("role", list(Role))
def test_the_matrix_matches_the_documented_table(role: Role, action: Action) -> None:
    assert is_allowed(role, action) is (role in EXPECTED[action])


def test_higher_roles_can_do_everything_lower_roles_can() -> None:
    assert allowed_actions(V) < allowed_actions(A) < allowed_actions(AD)


def test_only_admins_have_admin_actions() -> None:
    admin_only = {action for action in Action if action.value.startswith("admin_")}
    for action in admin_only:
        assert {role for role in Role if is_allowed(role, action)} == {AD}


def test_an_unknown_role_may_do_nothing() -> None:
    """Deny by default: a value that is not one of our roles gets no actions."""
    assert allowed_actions("superuser") == frozenset()  # type: ignore[arg-type]


def test_require_passes_silently_when_allowed() -> None:
    require(A, Action.PYTHON_ANALYSIS)


def test_require_refuses_and_writes_a_security_log_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO", LogFormat.JSON)
    with pytest.raises(PermissionDeniedError):
        require(V, Action.REINDEX, user="u_1234")

    output = capsys.readouterr().out.splitlines()
    lines = [json.loads(line) for line in output if line.startswith("{")]
    denied = [line for line in lines if line["event"] == "access_denied"]
    assert denied == [
        {
            **denied[0],
            "logger": "security",
            "action": "admin_reindex",
            "role": "viewer",
            "user": "u_1234",
            "level": "warning",
        }
    ]


def test_the_matrix_lists_every_action_for_every_role() -> None:
    table = matrix()
    assert set(table) == {action.value for action in Action}
    assert all(set(row) == {role.value for role in Role} for row in table.values())
    assert table["admin_reindex"] == {"viewer": False, "analyst": False, "admin": True}
