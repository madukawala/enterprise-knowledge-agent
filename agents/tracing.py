"""LangSmith tracing for agent runs.

Tracing is on only when `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` is
set. Without them the agents run exactly the same and send nothing.

The key is read from our settings and passed to the LangSmith client
directly, instead of relying on environment variables that LangChain
reads on its own. That way only runs started by `AgentService` are
traced, never a stray library call, and a test can never send a trace by
accident.

Each run is tagged with the role, and carries the request ID and thread ID
as metadata, so a trace can be found from a log line and the other way
round. LangGraph adds the node name to every step by itself. The user's
pseudonymised ID is added with `feature/auth-rbac`, and personal data is
masked before sending with `feature/guardrails` (ASSUMPTIONS O2). Until
then, only the fictional Helix Bank data should be used with tracing on.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from langchain_core.tracers.context import tracing_v2_enabled
from langsmith import Client

from core.config import Settings
from core.logging import get_logger

log = get_logger(__name__)


class Tracing:
    def __init__(self, settings: Settings) -> None:
        self.project = settings.langsmith_project
        self._client: Client | None = None
        if settings.langsmith_tracing and settings.langsmith_api_key is not None:
            self._client = Client(
                api_key=settings.langsmith_api_key.get_secret_value(),
                api_url=settings.langsmith_endpoint,
            )
        elif settings.langsmith_tracing:
            log.warning("langsmith_key_missing")

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @contextmanager
    def run(self, *, tags: Sequence[str]) -> Iterator[None]:
        """Trace everything inside this block as one LangSmith run tree."""
        if self._client is None:
            yield
            return
        with tracing_v2_enabled(project_name=self.project, client=self._client, tags=list(tags)):
            yield

    def flush(self) -> None:
        """Send any traces still waiting. Called on shutdown.

        A tracing problem is logged and never stops the app (failure
        table: "LangSmith down").
        """
        if self._client is None:
            return
        try:
            self._client.flush()
        except Exception as error:  # tracing must never break the app
            log.warning("langsmith_flush_failed", error_type=type(error).__name__)
