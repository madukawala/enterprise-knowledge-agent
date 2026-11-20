"""Sends an agent run to the client as server-sent events (SSE).

Each event is written as

    id: 7
    event: retrieval
    data: {"seq": 7, "request_id": "...", "type": "retrieval", ...}

`seq` counts up from 1 within one stream, so the client can keep events in
order and tests can check the order. `data` is always one line of JSON.

How the stream is run (`event_stream`):

- The agent run works in its own task and puts its events on a queue. The
  response reads from the queue. When nothing has come for
  `CHAT_KEEPALIVE_SECONDS`, it sends a comment line (`: keep-alive`), which
  clients ignore. It keeps proxies from closing a quiet connection while
  the agents think.
- If the client disconnects, Starlette stops the response, and the run's
  task is cancelled right away. Cancellation reaches the model or search
  call in progress, so no quota is spent on an answer nobody will read.
- The whole run has a time limit (`CHAT_TIMEOUT_SECONDS`). When it runs
  out, or something fails unexpectedly, the client gets an `error` event
  with a safe message and the request ID. The details go to the log.
"""

import asyncio
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import aclosing

import structlog

from agents.events import AgentEvent, ErrorEvent, EventType
from api.telemetry import ChatMetrics
from core.errors import AppError
from core.logging import get_logger

log = get_logger(__name__)

KEEPALIVE = ": keep-alive\n\n"
TIMEOUT_MESSAGE = "The answer took too long, so it was stopped. Please try again."


def encode(event: AgentEvent, *, seq: int, request_id: str) -> str:
    """One event in SSE format."""
    payload = {"seq": seq, "request_id": request_id, **event.model_dump(mode="json")}
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"id: {seq}\nevent: {event.type}\ndata: {data}\n\n"


class _Done:
    """Put on the queue when the run has finished, however it finished."""


async def event_stream(
    events: AsyncGenerator[AgentEvent],
    *,
    request_id: str,
    timeout_seconds: float,
    keepalive_seconds: float,
    metrics: ChatMetrics,
    on_finish: Callable[[], None] = lambda: None,
    log_context: Mapping[str, str] | None = None,
) -> AsyncIterator[str]:
    """The run's events as SSE text, with keep-alives, a time limit and cancellation."""
    queue: asyncio.Queue[AgentEvent | _Done] = asyncio.Queue()
    started = time.perf_counter()
    metrics.active.inc()

    async def run() -> None:
        # The task has its own copy of the logging context (with the request
        # ID), so these IDs are added to its log lines only.
        structlog.contextvars.bind_contextvars(**(log_context or {}))
        outcome = "error"
        try:
            async with asyncio.timeout(timeout_seconds), aclosing(events) as running:
                async for event in running:
                    queue.put_nowait(event)
            outcome = "completed"
        except TimeoutError:
            outcome = "timeout"
            log.warning("chat_timeout", timeout_seconds=timeout_seconds)
            queue.put_nowait(ErrorEvent(code="timeout", message=TIMEOUT_MESSAGE))
        except asyncio.CancelledError:
            outcome = "cancelled"
            log.info("chat_cancelled")
            raise
        except Exception:
            log.exception("chat_stream_failed")
            queue.put_nowait(ErrorEvent(code=AppError.code, message=AppError.default_message))
        finally:
            metrics.streams.labels(outcome=outcome).inc()
            metrics.active.dec()
            on_finish()
            queue.put_nowait(_Done())

    task = asyncio.create_task(run(), name=f"chat-{request_id}")
    seq = 0
    first_token = True
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), keepalive_seconds)
            except TimeoutError:
                yield KEEPALIVE
                continue
            if isinstance(item, _Done):
                return
            seq += 1
            metrics.events.labels(type=item.type.value).inc()
            if item.type is EventType.TOKEN and first_token:
                first_token = False
                metrics.first_token.observe(time.perf_counter() - started)
            yield encode(item, seq=seq, request_id=request_id)
    finally:
        # The client left (or the response failed): stop the run. Not
        # awaited, because this code may itself be in the middle of being
        # cancelled; the task cleans up and counts itself.
        if not task.done():
            task.cancel()
