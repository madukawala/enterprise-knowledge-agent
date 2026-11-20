"""`POST /chat`: ask the assistant a question and watch the answer being made.

The response is a stream of server-sent events (see api/sse.py and
docs/architecture/09-streaming-events.md): what each agent is doing, what
the search found, the checks, the answer token by token, and finally the
validated answer with its sources.

Anything wrong with the request itself is answered before the stream
starts, with a normal JSON error and status code:

- 422: the message is empty or too long, the thread ID is malformed, or the
  body has fields it should not have (for example `role`).
- 404: the thread belongs to someone else (api/threads.py).
- 409: the thread is still answering the previous question.
- 503: no language model is configured, so the agents are not running.

Once the stream has started the status is 200, so later problems arrive
as an `error` event instead.
"""

import uuid
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agents.nodes.input_guard import MAX_QUESTION_CHARS
from agents.service import AgentService
from api.identity import Caller, current_caller
from api.sse import event_stream
from api.telemetry import ChatMetrics
from api.threads import ThreadRegistry
from core.config import Settings
from core.errors import LLMUnavailableError

router = APIRouter(tags=["chat"])

THREAD_ID_HEADER = "X-Thread-ID"
RUN_ID_HEADER = "X-Run-ID"
AGENTS_NOT_READY = "The assistant is not available right now. Please try again later."


class ChatRequest(BaseModel):
    # Unknown fields are refused, so a client cannot slip in a role or an
    # access level; those only ever come from the caller's login.
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    # Leave out to start a new conversation. Send back the ID from the
    # first answer (the X-Thread-ID header) to ask a follow-up question.
    thread_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("The message is empty.")
        return value


_ERRORS: dict[int | str, dict[str, object]] = {
    status.HTTP_404_NOT_FOUND: {"description": "The thread belongs to someone else."},
    status.HTTP_409_CONFLICT: {"description": "The thread is still answering."},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "No language model is configured."},
}


@router.post(
    "/chat",
    response_class=StreamingResponse,
    responses={
        status.HTTP_200_OK: {
            "content": {"text/event-stream": {}},
            "description": "Server-sent events, ending with response_end or error.",
        },
        **_ERRORS,
    },
)
async def chat(
    body: ChatRequest,
    request: Request,
    caller: Annotated[Caller, Depends(current_caller)],
) -> StreamingResponse:
    settings = cast(Settings, request.app.state.settings)
    agents = cast(AgentService | None, getattr(request.app.state, "agents", None))
    if agents is None:
        raise LLMUnavailableError(AGENTS_NOT_READY)

    threads = cast(ThreadRegistry, request.app.state.threads)
    thread_id = body.thread_id or uuid.uuid4().hex
    threads.start(thread_id, owner=caller.owner)

    request_id = cast(str, request.state.request_id)
    run_id = str(uuid.uuid4())
    events = agents.stream(
        body.message,
        role=caller.role,
        thread_id=thread_id,
        request_id=request_id,
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
            log_context={"thread_id": thread_id, "run_id": run_id, "role": caller.role.value},
        ),
        media_type="text/event-stream",
        headers={
            # Never cache or buffer the stream, or tokens arrive all at once.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            THREAD_ID_HEADER: thread_id,
            RUN_ID_HEADER: run_id,
        },
    )
