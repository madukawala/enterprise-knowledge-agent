"""`GET /me`: who the signed-in user is and what they may do.

The UI calls this right after login, to show the user's name and role and
to hide the tools the role cannot use. Hiding them is only a courtesy: the
API checks the role again on every request, whatever the UI shows.

It also says how much of today's token budget the user has used, so the
UI can show it before the budget runs out.
"""

from typing import cast

from fastapi import APIRouter, Request
from pydantic import BaseModel

from api.identity import CurrentCaller
from security.policies import ACTION_LABELS, Action, allowed_actions
from security.rate_limit import RateLimiter
from security.roles import AccessLevel, Role, allowed_access_levels

router = APIRouter(tags=["account"])


class Permission(BaseModel):
    action: Action
    label: str
    allowed: bool


class Budget(BaseModel):
    """Today's language model tokens (UTC day)."""

    used: int
    limit: int
    resets_in_seconds: int


class MeResponse(BaseModel):
    username: str
    name: str
    role: Role
    # Every action, allowed or not, so the UI can show what the role lacks.
    permissions: list[Permission]
    access_levels: list[AccessLevel]
    # None when rate limiting is switched off.
    budget: Budget | None


@router.get("/me")
async def me(caller: CurrentCaller, request: Request) -> MeResponse:
    limiter = cast(RateLimiter, request.app.state.rate_limiter)
    budget = None
    if limiter.enabled:
        usage = await limiter.usage_today(caller.pseudonym, caller.role)
        budget = Budget(used=usage.used, limit=usage.limit, resets_in_seconds=usage.resets_in)
    allowed = allowed_actions(caller.role)
    levels = allowed_access_levels(caller.role)
    return MeResponse(
        username=caller.username,
        name=caller.name,
        role=caller.role,
        permissions=[
            Permission(action=action, label=ACTION_LABELS[action], allowed=action in allowed)
            for action in Action
        ],
        # In the order they are defined: public first, restricted last.
        access_levels=[level for level in AccessLevel if level in levels],
        budget=budget,
    )
