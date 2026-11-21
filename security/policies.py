"""The role matrix: what each role may do. Deny by default.

This is the only place that says which role may do what. The API routes,
the supervisor, the tools agent and the Python tool all ask
`is_allowed()` or `require()`; none of them keeps its own list.

| Action                   | Viewer | Analyst | Admin |
|--------------------------|--------|---------|-------|
| Chat                     | Yes    | Yes     | Yes   |
| Knowledge search         | Yes    | Yes     | Yes   |
| Python analysis tool     | No     | Yes     | Yes   |
| MCP tools                | No     | Yes     | Yes   |
| Recursive research       | No     | Yes     | Yes   |
| Admin: access matrix     | No     | No      | Yes   |
| Admin: audit log         | No     | No      | Yes   |
| Admin: rate limits       | No     | No      | Yes   |
| Admin: all feedback      | No     | No      | Yes   |
| Admin: re-index          | No     | No      | Yes   |
| Admin: memory clean-up   | No     | No      | Yes   |

Document access levels per role are in security/roles.py, because the
search filter needs them as a set rather than as yes/no actions.

"Deny by default" means an action that is not listed for a role is
refused. A new action added to `Action` is refused for every role until
someone adds it here on purpose, and a test (test_policies.py) checks
that every action has been thought about.
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from core.errors import PermissionDeniedError
from core.logging import SECURITY_LOGGER, get_logger
from security.roles import Role

security_log = get_logger(SECURITY_LOGGER)


class Action(StrEnum):
    """Things a user can try to do. The values appear in logs and in GET /me."""

    CHAT = "chat"
    KNOWLEDGE_SEARCH = "knowledge_search"
    PYTHON_ANALYSIS = "python_analysis"
    MCP_TOOLS = "mcp_tools"
    RECURSIVE_RESEARCH = "recursive_research"
    VIEW_ACCESS_MATRIX = "admin_access_matrix"
    VIEW_AUDIT_LOG = "admin_audit_log"
    MANAGE_RATE_LIMITS = "admin_rate_limits"
    VIEW_ALL_FEEDBACK = "admin_all_feedback"
    REINDEX = "admin_reindex"
    MEMORY_CLEANUP = "admin_memory_cleanup"


# Short names for the UI and the docs.
ACTION_LABELS: Mapping[Action, str] = MappingProxyType(
    {
        Action.CHAT: "Chat",
        Action.KNOWLEDGE_SEARCH: "Knowledge search",
        Action.PYTHON_ANALYSIS: "Python analysis tool",
        Action.MCP_TOOLS: "Enterprise data tools (MCP)",
        Action.RECURSIVE_RESEARCH: "Deep research",
        Action.VIEW_ACCESS_MATRIX: "Admin: access matrix",
        Action.VIEW_AUDIT_LOG: "Admin: audit log",
        Action.MANAGE_RATE_LIMITS: "Admin: rate limits",
        Action.VIEW_ALL_FEEDBACK: "Admin: all feedback",
        Action.REINDEX: "Admin: re-index documents",
        Action.MEMORY_CLEANUP: "Admin: memory clean-up",
    }
)

_VIEWER = frozenset({Action.CHAT, Action.KNOWLEDGE_SEARCH})
_ANALYST = _VIEWER | {Action.PYTHON_ANALYSIS, Action.MCP_TOOLS, Action.RECURSIVE_RESEARCH}
_ADMIN = _ANALYST | {
    Action.VIEW_ACCESS_MATRIX,
    Action.VIEW_AUDIT_LOG,
    Action.MANAGE_RATE_LIMITS,
    Action.VIEW_ALL_FEEDBACK,
    Action.REINDEX,
    Action.MEMORY_CLEANUP,
}

_ALLOWED: Mapping[Role, frozenset[Action]] = MappingProxyType(
    {Role.VIEWER: _VIEWER, Role.ANALYST: _ANALYST, Role.ADMIN: _ADMIN}
)


def allowed_actions(role: Role) -> frozenset[Action]:
    """Everything `role` may do. Unknown roles may do nothing."""
    return _ALLOWED.get(role, frozenset())


def is_allowed(role: Role, action: Action) -> bool:
    return action in allowed_actions(role)


def require(role: Role, action: Action, *, user: str | None = None) -> None:
    """Raise PermissionDeniedError unless `role` may do `action`.

    Every refusal is written to the security log, so a user probing for
    things they may not do shows up there. `user` is the pseudonymised
    user ID (never the username or e-mail).
    """
    if is_allowed(role, action):
        return
    security_log.warning("access_denied", action=action.value, role=role.value, user=user)
    raise PermissionDeniedError()


def matrix() -> dict[str, dict[str, bool]]:
    """The whole matrix as {action: {role: allowed}}, for the admin endpoint and docs."""
    return {
        action.value: {role.value: is_allowed(role, action) for role in Role} for action in Action
    }
