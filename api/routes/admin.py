"""Admin endpoints. Every route here needs an admin permission.

- `GET /admin/access-matrix`: the role matrix, so an admin can check who
  may do what without reading the code.
- `GET /admin/rate-limits`: the rate limits in force for each role, and
  whether Redis is holding the counts.
- `PUT /admin/rate-limits/{role}`: change a role's questions per minute
  and daily token budget, for every API worker, without a restart.
- `DELETE /admin/rate-limits/{role}`: go back to the values in the settings.
- `GET /admin/feedback`: every user's thumbs up and down, newest first.
- `GET /admin/audit-log?day=YYYY-MM-DD`: one record per question that day.
- `POST /admin/memory/cleanup`: the retention clean-up. Deletes threads
  not used for `MEMORY_RETENTION_DAYS` days (and their saved steps), and
  long-term memory items older than that, for every user.

The audit log, all feedback and re-indexing are added here by their own
branches. Each route depends on `requires(...)` with its own action, never
on a role name.
"""

from datetime import UTC, date, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from agents.service import AgentService
from api.audit import AuditLog
from api.identity import Caller, requires
from api.threads import ThreadRegistry
from core.config import Settings
from core.logging import SECURITY_LOGGER, get_logger
from security.policies import Action, matrix
from security.rate_limit import RateLimiter, RoleLimits
from security.roles import AccessLevel, Role, allowed_access_levels

security_log = get_logger(SECURITY_LOGGER)

router = APIRouter(prefix="/admin", tags=["admin"])

_FORBIDDEN: dict[int | str, dict[str, object]] = {
    status.HTTP_403_FORBIDDEN: {"description": "The caller's role does not allow this."},
}


class AccessMatrixResponse(BaseModel):
    # {action: {role: allowed}}
    actions: dict[str, dict[str, bool]]
    # {role: [access levels it may read]}
    access_levels: dict[Role, list[AccessLevel]]


@router.get("/access-matrix", responses=_FORBIDDEN)
async def access_matrix(
    _caller: Annotated[Caller, Depends(requires(Action.VIEW_ACCESS_MATRIX))],
) -> AccessMatrixResponse:
    return AccessMatrixResponse(
        actions=matrix(),
        access_levels={
            role: [level for level in AccessLevel if level in allowed_access_levels(role)]
            for role in Role
        },
    )


# ---------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------


class LimitsBody(BaseModel):
    """One role's limits. Each user with the role gets these."""

    model_config = ConfigDict(extra="forbid")

    per_minute: int = Field(ge=1, le=600, description="Questions per minute for each user.")
    daily_tokens: int = Field(
        ge=1_000, le=100_000_000, description="Language model tokens per user per day (UTC)."
    )

    @classmethod
    def of(cls, limits: RoleLimits) -> "LimitsBody":
        return cls(per_minute=limits.per_minute, daily_tokens=limits.daily_tokens)


class RateLimitsResponse(BaseModel):
    limits: dict[Role, LimitsBody]
    # The values from the settings, which DELETE goes back to.
    defaults: dict[Role, LimitsBody]
    burst: int
    role_per_minute: int
    tokens_per_question: int
    enabled: bool
    # "redis", or "memory" when the API runs without Redis.
    store: str
    # True while Redis is down and each worker uses stricter, separate counts.
    degraded: bool


async def _rate_limits(limiter: RateLimiter) -> RateLimitsResponse:
    limits = await limiter.limits()
    return RateLimitsResponse(
        limits={role: LimitsBody.of(limits[role]) for role in Role},
        defaults={role: LimitsBody.of(limiter.defaults[role]) for role in Role},
        burst=limiter.burst,
        role_per_minute=limiter.role_per_minute,
        tokens_per_question=limiter.tokens_per_question,
        enabled=limiter.enabled,
        store=limiter.store.name,
        degraded=limiter.degraded,
    )


ManageRateLimits = Annotated[Caller, Depends(requires(Action.MANAGE_RATE_LIMITS))]
_CHANGE_ERRORS: dict[int | str, dict[str, object]] = {
    **_FORBIDDEN,
    status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Redis is down; nothing was changed."},
}


def _limiter(request: Request) -> RateLimiter:
    return cast(RateLimiter, request.app.state.rate_limiter)


@router.get("/rate-limits", responses=_FORBIDDEN)
async def rate_limits(request: Request, _caller: ManageRateLimits) -> RateLimitsResponse:
    return await _rate_limits(_limiter(request))


@router.put("/rate-limits/{role}", responses=_CHANGE_ERRORS)
async def change_rate_limits(
    role: Role, body: LimitsBody, request: Request, caller: ManageRateLimits
) -> RateLimitsResponse:
    limiter = _limiter(request)
    before = (await limiter.limits())[role]
    await limiter.set_limits(role, RoleLimits(body.per_minute, body.daily_tokens))
    security_log.warning(
        "rate_limits_changed",
        changed_role=role.value,
        per_minute=[before.per_minute, body.per_minute],
        daily_tokens=[before.daily_tokens, body.daily_tokens],
        user=caller.pseudonym,
    )
    return await _rate_limits(limiter)


@router.delete("/rate-limits/{role}", responses=_CHANGE_ERRORS)
async def reset_rate_limits(
    role: Role, request: Request, caller: ManageRateLimits
) -> RateLimitsResponse:
    limiter = _limiter(request)
    await limiter.set_limits(role, None)
    security_log.warning("rate_limits_reset", changed_role=role.value, user=caller.pseudonym)
    return await _rate_limits(limiter)


# ---------------------------------------------------------------
# Memory retention
# ---------------------------------------------------------------


class CleanupResponse(BaseModel):
    days: int
    threads_deleted: int
    memories_deleted: int


@router.post("/memory/cleanup", responses=_FORBIDDEN)
async def memory_cleanup(
    request: Request,
    caller: Annotated[Caller, Depends(requires(Action.MEMORY_CLEANUP))],
) -> CleanupResponse:
    settings = cast(Settings, request.app.state.settings)
    days = settings.memory_retention_days
    threads = cast(ThreadRegistry, request.app.state.threads)
    agents = cast(AgentService | None, getattr(request.app.state, "agents", None))

    stale = await threads.older_than(days)
    for thread_id in stale:
        if threads.index is not None:
            await threads.index.adelete(("threads",), thread_id)
        if agents is not None:
            await agents.forget_thread(thread_id)
    memories = 0
    if agents is not None and agents.memory is not None:
        memories = await agents.memory.forget_older_than(days)
    security_log.info(
        "memory_cleanup",
        days=days,
        threads=len(stale),
        memories=memories,
        user=caller.pseudonym,
    )
    return CleanupResponse(days=days, threads_deleted=len(stale), memories_deleted=memories)


# ---------------------------------------------------------------
# Feedback from every user
# ---------------------------------------------------------------


class FeedbackItem(BaseModel):
    run_id: str
    thread_id: str
    score: str
    comment: str | None
    # The user's pseudonym and role, never their name.
    user: str
    role: str
    at: str


@router.get("/feedback", responses=_FORBIDDEN)
async def all_feedback(
    request: Request,
    _caller: Annotated[Caller, Depends(requires(Action.VIEW_ALL_FEEDBACK))],
) -> list[FeedbackItem]:
    agents = cast(AgentService | None, getattr(request.app.state, "agents", None))
    if agents is None or agents.store is None:
        return []
    items = await agents.store.asearch(("feedback",), limit=500)
    found = [FeedbackItem(**item.value) for item in items]
    return sorted(found, key=lambda f: f.at, reverse=True)[:200]


# ---------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------


@router.get("/audit-log", responses=_FORBIDDEN)
async def audit_log(
    request: Request,
    _caller: Annotated[Caller, Depends(requires(Action.VIEW_AUDIT_LOG))],
    day: Annotated[date | None, Query(description="Which day (UTC). Default: today.")] = None,
) -> list[dict[str, Any]]:
    audit = cast(AuditLog | None, getattr(request.app.state, "audit", None))
    if audit is None:
        return []
    return await audit.day((day or datetime.now(UTC).date()).isoformat())
