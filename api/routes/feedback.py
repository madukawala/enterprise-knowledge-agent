"""Human approval and answer feedback.

    POST /approvals/{thread_id}   {"approve": true|false}
        Answer a run that stopped to ask (the `approval_required` event).
        Streams the rest of the run, exactly like `POST /chat`.

    POST /feedback   {"run_id": ..., "thread_id": ..., "score": "up"|"down", "comment": ...}
        Thumbs up or down on one answer. Saved, and sent to LangSmith on the
        answer's trace when tracing is on.

Both work on the caller's own threads only: someone else's thread gives
404, as for `POST /chat`. Approving needs the same permission the run
needs (research needs `recursive_research`), checked again here.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from agents.service import AgentService
from api.audit import AuditLog, audited
from api.identity import Caller, requires
from api.routes.chat import RUN_ID_HEADER, THREAD_ID_HEADER, charged
from api.sse import event_stream
from api.telemetry import ChatMetrics
from api.threads import ThreadRegistry
from core.config import Settings
from core.errors import AppError, ConflictError, LLMUnavailableError
from core.logging import SECURITY_LOGGER, get_logger
from core.pii import mask_text
from security.policies import Action, require
from security.rate_limit import RateLimiter

router = APIRouter(tags=["approval and feedback"])
log = get_logger(__name__)
security_log = get_logger(SECURITY_LOGGER)

NOTHING_WAITING = "This conversation is not waiting for an approval."
FEEDBACK_NAMESPACE = ("feedback",)
SignedIn = Annotated[Caller, Depends(requires(Action.CHAT))]

# The permission each kind of approval needs (agents/nodes/approval.py).
_ACTION_FOR_APPROVAL = {"research": Action.RECURSIVE_RESEARCH}


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    thread_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    score: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=500)


class FeedbackSaved(BaseModel):
    saved: bool
    sent_to_langsmith: bool


def _agents(request: Request) -> AgentService:
    agents = cast(AgentService | None, getattr(request.app.state, "agents", None))
    if agents is None:
        raise LLMUnavailableError("The assistant is not available right now.")
    return agents


@router.post(
    "/approvals/{thread_id}",
    response_class=StreamingResponse,
    responses={404: {"description": "Not the caller's."}, 409: {"description": "Nothing waits."}},
)
async def answer_approval(
    thread_id: str, body: ApprovalRequest, request: Request, caller: SignedIn
) -> StreamingResponse:
    agents = _agents(request)
    threads = cast(ThreadRegistry, request.app.state.threads)
    if threads.index is not None:
        await threads.get(thread_id, owner=caller.owner)  # 404 unless it is theirs

    waiting = await agents.pending_approval(thread_id)
    if waiting is None:
        raise ConflictError(NOTHING_WAITING)
    action = _ACTION_FOR_APPROVAL.get(str(waiting.get("action")))
    if action is not None:
        require(caller.role, action, user=caller.pseudonym)

    limiter = cast(RateLimiter, request.app.state.rate_limiter)
    admission = await limiter.admit(caller.pseudonym, caller.role)
    try:
        await threads.claim(thread_id, owner=caller.owner, question="")
    except AppError:
        await limiter.refund(admission)
        raise
    security_log.info(
        "approval_answered",
        action=waiting.get("action"),
        approved=body.approve,
        user=caller.pseudonym,
        thread_id=thread_id,
    )

    settings = cast(Settings, request.app.state.settings)
    request_id = cast(str, request.state.request_id)
    run_id = str(uuid.uuid4())
    events = audited(
        charged(
            agents.resume(
                thread_id,
                approved=body.approve,
                role=caller.role,
                user=caller.pseudonym,
                request_id=request_id,
                run_id=run_id,
            ),
            limiter,
            admission,
        ),
        cast(AuditLog | None, getattr(request.app.state, "audit", None)),
        kind="approval_approved" if body.approve else "approval_rejected",
        question=str(waiting.get("question", "")),
        user=caller.pseudonym,
        role=caller.role.value,
        request_id=request_id,
        thread_id=thread_id,
        run_id=run_id,
    )
    return StreamingResponse(
        event_stream(
            events,
            request_id=request_id,
            timeout_seconds=settings.chat_timeout_seconds,
            keepalive_seconds=settings.chat_keepalive_seconds,
            metrics=cast(ChatMetrics, request.app.state.chat_metrics),
            on_finish=lambda: threads.finish(thread_id),
            log_context={"thread_id": thread_id, "run_id": run_id, "user": caller.pseudonym},
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            THREAD_ID_HEADER: thread_id,
            RUN_ID_HEADER: run_id,
        },
    )


@router.post("/feedback", status_code=status.HTTP_201_CREATED)
async def give_feedback(body: FeedbackRequest, request: Request, caller: SignedIn) -> FeedbackSaved:
    agents = _agents(request)
    threads = cast(ThreadRegistry, request.app.state.threads)
    if threads.index is not None:
        await threads.get(body.thread_id, owner=caller.owner)  # 404 unless it is theirs

    comment = mask_text(body.comment.strip()) if body.comment else None
    if agents.store is not None:
        await agents.store.aput(
            FEEDBACK_NAMESPACE,
            f"{body.run_id}:{caller.pseudonym}",
            {
                "run_id": str(body.run_id),
                "thread_id": body.thread_id,
                "score": body.score,
                "comment": comment,
                "user": caller.pseudonym,
                "role": caller.role.value,
                "at": datetime.now(UTC).isoformat(),
            },
        )
    sent = (
        agents.tracing.send_feedback(
            str(body.run_id), score=1.0 if body.score == "up" else 0.0, comment=comment
        )
        if agents.tracing is not None
        else False
    )
    agents.metrics.feedback.labels(score=body.score).inc()
    log.info("feedback_saved", score=body.score, sent=sent)
    return FeedbackSaved(saved=agents.store is not None, sent_to_langsmith=sent)
