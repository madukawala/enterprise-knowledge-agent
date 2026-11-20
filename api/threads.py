"""Conversation threads: who owns each one, and which are answering right now.

Two simple rules, checked before a chat stream starts:

1. A thread belongs to the caller who started it. Anyone else using its ID
   gets "not found", which says nothing about whose it is or what is in
   it. Without this, a viewer who learned an admin's thread ID would get
   the admin's earlier answers as context for their own question.
2. One question at a time per thread. A second question while the first is
   still being answered gets 409, because two runs writing to the same
   conversation would mix up its history.

Both live in memory, like the conversations themselves (the in-memory
checkpointer). `feature/memory` moves conversations to Postgres, and the
owner moves with them; `feature/auth-rbac` makes the owner the user ID
from the login token instead of the role.
"""

import time

from core.errors import ConflictError, NotFoundError

THREAD_NOT_FOUND = "This conversation was not found."
THREAD_BUSY = "This conversation is still answering the previous question. Please wait for it."


class ThreadRegistry:
    def __init__(self, *, busy_for_at_most: float) -> None:
        # A thread counts as busy for at most this long, even if its stream
        # never got to say it had finished (for example, the client left
        # before the first byte was sent).
        self.busy_for_at_most = busy_for_at_most
        self._owners: dict[str, str] = {}
        self._busy_until: dict[str, float] = {}

    def start(self, thread_id: str, *, owner: str) -> None:
        """Claim the thread for one run. Raises NotFoundError or ConflictError."""
        if self._owners.setdefault(thread_id, owner) != owner:
            raise NotFoundError(THREAD_NOT_FOUND)
        now = time.monotonic()
        if self._busy_until.get(thread_id, 0.0) > now:
            raise ConflictError(THREAD_BUSY)
        self._busy_until[thread_id] = now + self.busy_for_at_most

    def finish(self, thread_id: str) -> None:
        self._busy_until.pop(thread_id, None)
