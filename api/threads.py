"""Conversation threads: who owns each one, and which are answering right now.

Two simple rules, checked before a chat stream starts:

1. A thread belongs to the caller who started it. Anyone else using its ID
   gets "not found", which says nothing about whose it is or what is in
   it. Without this, a user who learned someone else's thread ID would get
   their earlier answers as context for their own question.
2. One question at a time per thread. A second question while the first is
   still being answered gets 409, because two runs writing to the same
   conversation would mix up its history.

The owner is the user ID from the login token (`Caller.owner`), so two
users with the same role never share a conversation.

Owners are kept in the same LangGraph store as long-term memory (Postgres
in Docker), under the namespace `("threads",)`, with the thread's title and
dates. Conversations survive a restart, so their owners must too: a thread
whose owner was only in memory could be claimed by anyone after a restart.
Which threads are busy is kept in this process only; it only matters while
a stream is open.
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from langgraph.store.base import BaseStore

from core.errors import ConflictError, NotFoundError
from core.pii import mask_text

THREAD_NOT_FOUND = "This conversation was not found."
THREAD_BUSY = "This conversation is still answering the previous question. Please wait for it."
NAMESPACE = ("threads",)
MAX_TITLE_CHARS = 60
MAX_LISTED = 50


@dataclass(frozen=True)
class ThreadInfo:
    thread_id: str
    title: str
    created: datetime
    updated: datetime


def _title(question: str) -> str:
    text = mask_text(" ".join(question.split()))
    return text if len(text) <= MAX_TITLE_CHARS else text[: MAX_TITLE_CHARS - 1].rstrip() + "…"


class ThreadRegistry:
    def __init__(self, *, busy_for_at_most: float, index: BaseStore | None = None) -> None:
        # A thread counts as busy for at most this long, even if its stream
        # never got to say it had finished (for example, the client left
        # before the first byte was sent).
        self.busy_for_at_most = busy_for_at_most
        # Where owners and titles are saved. Set on startup, once the
        # agents' store is open; None means owners live in memory only.
        self.index = index
        self._owners: dict[str, str] = {}
        self._busy_until: dict[str, float] = {}

    # --- one run ---

    async def claim(self, thread_id: str, *, owner: str, question: str) -> None:
        """Check the owner (saving a new thread), then mark it busy for one run."""
        if self.index is not None:
            item = await self.index.aget(NAMESPACE, thread_id)
            now = datetime.now(UTC).isoformat()
            if item is None:
                value = {"owner": owner, "title": _title(question), "created": now}
            elif item.value.get("owner") != owner:
                raise NotFoundError(THREAD_NOT_FOUND)
            else:
                value = dict(item.value)
            await self.index.aput(NAMESPACE, thread_id, {**value, "updated": now})
        self.start(thread_id, owner=owner)

    def start(self, thread_id: str, *, owner: str) -> None:
        """Claim the thread for one run, in this process. Raises NotFoundError or ConflictError."""
        if self._owners.setdefault(thread_id, owner) != owner:
            raise NotFoundError(THREAD_NOT_FOUND)
        now = time.monotonic()
        if self._busy_until.get(thread_id, 0.0) > now:
            raise ConflictError(THREAD_BUSY)
        self._busy_until[thread_id] = now + self.busy_for_at_most

    def finish(self, thread_id: str) -> None:
        self._busy_until.pop(thread_id, None)

    # --- the user's conversations ---

    async def of_owner(self, owner: str) -> list[ThreadInfo]:
        """The owner's threads, most recently used first."""
        if self.index is None:
            return []
        items = await self.index.asearch(NAMESPACE, filter={"owner": owner}, limit=500)
        threads = [_info(item.key, item.value) for item in items]
        return sorted(threads, key=lambda t: t.updated, reverse=True)[:MAX_LISTED]

    async def get(self, thread_id: str, *, owner: str) -> ThreadInfo:
        """The thread, if it belongs to `owner`. Anyone else gets NotFoundError."""
        item = await self.index.aget(NAMESPACE, thread_id) if self.index is not None else None
        if item is None or item.value.get("owner") != owner:
            raise NotFoundError(THREAD_NOT_FOUND)
        return _info(thread_id, item.value)

    async def delete(self, thread_id: str, *, owner: str) -> None:
        await self.get(thread_id, owner=owner)  # 404 unless it is theirs
        if self._busy_until.get(thread_id, 0.0) > time.monotonic():
            raise ConflictError(THREAD_BUSY)
        if self.index is not None:
            await self.index.adelete(NAMESPACE, thread_id)
        self._owners.pop(thread_id, None)

    async def older_than(self, days: int) -> list[str]:
        """Every thread not used for `days` days, for the retention clean-up."""
        if self.index is None:
            return []
        cutoff = datetime.now(UTC) - timedelta(days=days)
        items = await self.index.asearch(NAMESPACE, limit=10_000)
        return [item.key for item in items if _info(item.key, item.value).updated < cutoff]


def _info(thread_id: str, value: dict[str, object]) -> ThreadInfo:
    created = datetime.fromisoformat(str(value.get("created")))
    return ThreadInfo(
        thread_id=thread_id,
        title=str(value.get("title", "")),
        created=created,
        updated=datetime.fromisoformat(str(value.get("updated", created.isoformat()))),
    )
