"""The audit log: one record per question, for admins (`GET /admin/audit-log`).

Each record says who asked (pseudonym and role), what (the question, masked
and cut short), what the system did (intent, tools called, documents cited,
guardrail refusals, approvals asked and answered, model calls and tokens)
and how it ended. It is written when the answer's stream ends, including
when the client leaves early, so the trail has no gaps.

Records are kept in the same store as memory and threads (Postgres in
Docker), under one namespace per day, `("audit", "2026-11-21")`, which makes
a day's records cheap to read and old days easy to delete. The API only
ever adds records; there is no endpoint that changes or removes one.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import aclosing
from datetime import UTC, datetime
from typing import Any

from langgraph.store.base import BaseStore

from agents.events import (
    AgentEvent,
    AgentStateEvent,
    ApprovalRequiredEvent,
    ResponseEndEvent,
    ToolCallEvent,
)
from core.logging import get_logger
from core.pii import mask_text

log = get_logger(__name__)

MAX_QUESTION_CHARS = 200


def _day(moment: datetime) -> str:
    return moment.date().isoformat()


class AuditLog:
    def __init__(self, store: BaseStore) -> None:
        self.store = store

    async def add(self, record: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        await self.store.aput(
            ("audit", _day(now)), uuid.uuid4().hex, {**record, "at": now.isoformat()}
        )

    async def day(self, day: str, *, limit: int = 500) -> list[dict[str, Any]]:
        items = await self.store.asearch(("audit", day), limit=limit)
        return sorted((dict(item.value) for item in items), key=lambda r: r["at"], reverse=True)


async def audited(
    events: AsyncGenerator[AgentEvent],
    audit: AuditLog | None,
    *,
    kind: str,
    question: str,
    user: str,
    role: str,
    request_id: str,
    thread_id: str,
    run_id: str,
) -> AsyncGenerator[AgentEvent]:
    """Pass the events through and write one audit record when they end.

    `kind` is "question" for POST /chat and "approval" for an approval answer.
    """
    started = time.perf_counter()
    record: dict[str, Any] = {
        "kind": kind,
        "user": user,
        "role": role,
        "request_id": request_id,
        "thread_id": thread_id,
        "run_id": run_id,
        "question": mask_text(" ".join(question.split()))[:MAX_QUESTION_CHARS],
        "intent": None,
        "outcome": "left_early",
        "answer_kind": None,
        "documents": [],
        "tools": [],
        "limits": [],
        "llm_calls": 0,
        "tokens": 0,
    }
    try:
        async with aclosing(events) as running:
            async for event in running:
                _note(record, event)
                yield event
    finally:
        record["duration_ms"] = round((time.perf_counter() - started) * 1000)
        if audit is not None:
            # Its own task: when the client leaves, this run is being
            # cancelled, and an await here would be cancelled with it.
            task = asyncio.get_running_loop().create_task(_write(audit, record))
            _pending.add(task)
            task.add_done_callback(_pending.discard)


# Audit writes still in flight; kept so they are not garbage-collected.
_pending: set[asyncio.Task[None]] = set()


async def _write(audit: AuditLog, record: dict[str, Any]) -> None:
    try:
        await audit.add(record)
    except Exception as error:  # the answer matters more than its record
        log.error("audit_write_failed", error=type(error).__name__)


def _note(record: dict[str, Any], event: AgentEvent) -> None:
    if isinstance(event, AgentStateEvent) and event.intent is not None:
        record["intent"] = event.intent.value
    elif isinstance(event, ToolCallEvent):
        record["tools"].append({"tool": event.tool, "ok": event.ok})
    elif isinstance(event, ApprovalRequiredEvent):
        record["outcome"] = "waiting_for_approval"
    elif isinstance(event, ResponseEndEvent):
        record["outcome"] = "answered"
        record["answer_kind"] = event.kind.value
        record["documents"] = sorted({c.doc_id for c in event.citations})
        record["limits"] = list(event.limits)
        record["llm_calls"] = len(event.llm_calls)
        record["tokens"] = sum(c.input_tokens + c.output_tokens for c in event.llm_calls)
