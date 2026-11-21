"""Long-term memory per user (ADR-0008, docs/architecture/08-memory.md).

Two kinds of item, kept in a LangGraph store (Postgres in Docker, memory in
tests and when the API runs on its own):

- **profile** (`("memory", <user>, "profile")`, one item): the departments
  the user asks about most, counted from their searches.
- **interactions** (`("memory", <user>, "interactions")`): one line per
  answered question: the question (cut short) and the documents it cited.

Rules:

- `<user>` is the pseudonym from the login token (`u_...`), never the user
  ID, name or e-mail. One user can never read another user's namespace:
  every method takes the user from the caller, who took it from the token.
- Everything is passed through the personal data masker (core/pii.py)
  before it is saved.
- Recall is by shared words with the new question, newest first on ties.
  No embedding model is needed, and the result is easy to explain.
- Items older than the retention period are deleted by `forget_older_than`
  (the admin clean-up, `POST /admin/memory/cleanup`).

Memory is a help, never a requirement: if the store fails, the error is
logged and the question is answered without it.
"""

import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from langgraph.store.base import BaseStore

from core.logging import get_logger
from core.pii import mask_text

log = get_logger(__name__)

PROFILE = "profile"
INTERACTIONS = "interactions"
MAX_INTERACTION_CHARS = 200
# Most interactions kept per user; the oldest go first.
MAX_INTERACTIONS = 200
_WORD = re.compile(r"[a-z0-9][a-z0-9-]{2,}")
_STOP_WORDS = frozenset(
    {
        "the", "and", "for", "with", "what", "when", "where", "which", "who", "how",
        "why", "does", "did", "are", "was", "were", "this", "that", "from", "into",
        "over", "about", "our", "your", "their", "have", "has", "had", "can", "could",
        "should", "would", "will",
    }
)  # fmt: skip


def _words(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.lower()) if word not in _STOP_WORDS}


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Interaction:
    key: str
    text: str
    documents: tuple[str, ...]
    at: datetime


class LongTermMemory:
    def __init__(self, store: BaseStore) -> None:
        self.store = store

    @staticmethod
    def _namespace(user: str, kind: str) -> tuple[str, ...]:
        return ("memory", user, kind)

    # --- writing ---

    async def remember(
        self, user: str, question: str, documents: list[str], departments: list[str]
    ) -> list[str]:
        """Save one answered question and update the profile. Returns what was saved."""
        text = mask_text(" ".join(question.split()))[:MAX_INTERACTION_CHARS]
        await self.store.aput(
            self._namespace(user, INTERACTIONS),
            uuid.uuid4().hex,
            {"text": text, "documents": documents[:10], "at": _now().isoformat()},
        )
        saved = [f"Remembered this question: {text}"]
        if departments:
            profile = await self.profile(user)
            counts = Counter(profile.get("departments", {}))
            counts.update(departments)
            await self.store.aput(
                self._namespace(user, PROFILE),
                PROFILE,
                {"departments": dict(counts), "updated": _now().isoformat()},
            )
            saved.append(f"Noted interest in: {', '.join(sorted(set(departments)))}")
        await self._trim(user)
        return saved

    async def _trim(self, user: str) -> None:
        items = await self.interactions(user, limit=MAX_INTERACTIONS + 50)
        for old in items[MAX_INTERACTIONS:]:
            await self.store.adelete(self._namespace(user, INTERACTIONS), old.key)

    # --- reading ---

    async def profile(self, user: str) -> dict[str, Any]:
        item = await self.store.aget(self._namespace(user, PROFILE), PROFILE)
        return dict(item.value) if item is not None else {}

    async def interactions(self, user: str, *, limit: int = 50) -> list[Interaction]:
        """The user's saved questions, newest first."""
        items = await self.store.asearch(self._namespace(user, INTERACTIONS), limit=limit)
        found = [
            Interaction(
                key=item.key,
                text=str(item.value.get("text", "")),
                documents=tuple(item.value.get("documents", [])),
                at=datetime.fromisoformat(str(item.value.get("at"))),
            )
            for item in items
        ]
        return sorted(found, key=lambda i: i.at, reverse=True)

    async def recall(self, user: str, question: str, *, limit: int = 3) -> list[str]:
        """Lines for the prompt: the profile, and past questions sharing words with this one."""
        lines = []
        departments = (await self.profile(user)).get("departments") or {}
        if departments:
            top = sorted(departments, key=lambda d: (-departments[d], d))[:3]
            lines.append(f"The user often asks about: {', '.join(top)}.")
        wanted = _words(question)
        scored = [(len(wanted & _words(item.text)), item) for item in await self.interactions(user)]
        related = [item for score, item in sorted(scored, key=lambda s: -s[0]) if score >= 2]
        for item in related[:limit]:
            docs = f" (sources: {', '.join(item.documents)})" if item.documents else ""
            lines.append(f"Earlier ({item.at.date()}) the user asked: {item.text}{docs}")
        return lines

    # --- forgetting ---

    async def forget(self, user: str) -> int:
        """Delete everything remembered about the user. Returns how many items."""
        count = 0
        for item in await self.interactions(user, limit=MAX_INTERACTIONS + 50):
            await self.store.adelete(self._namespace(user, INTERACTIONS), item.key)
            count += 1
        if await self.store.aget(self._namespace(user, PROFILE), PROFILE) is not None:
            await self.store.adelete(self._namespace(user, PROFILE), PROFILE)
            count += 1
        return count

    async def forget_older_than(self, days: int) -> int:
        """The retention clean-up: delete interactions older than `days`, for every user."""
        cutoff = _now() - timedelta(days=days)
        deleted = 0
        for namespace in await self.store.alist_namespaces(prefix=("memory",), max_depth=3):
            if namespace[-1] != INTERACTIONS:
                continue
            for item in await self.store.asearch(namespace, limit=MAX_INTERACTIONS + 50):
                if datetime.fromisoformat(str(item.value.get("at"))) < cutoff:
                    await self.store.adelete(namespace, item.key)
                    deleted += 1
        return deleted
