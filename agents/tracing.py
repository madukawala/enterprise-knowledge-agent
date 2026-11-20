"""LangSmith tracing for agent runs.

Tracing is on only when `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` is
set. Without them the agents run exactly the same and send nothing.

The key is read from our settings and given to a LangSmith client of our
own, instead of relying on environment variables that LangChain reads on
its own. The tracer is passed to each run as a callback (`callbacks()`),
so only runs started by `AgentService` are traced, never a stray library
call, and a test can never send a trace by accident. A callback travels
with the run, so it also works for streamed runs, where the work is split
across many steps of an async generator.

Each run is tagged with the role, and carries the request ID and thread ID
as metadata, so a trace can be found from a log line and the other way
round. The run's ID is chosen by us and sent to the client in the first
streamed event, so the UI can link feedback to the trace later. LangGraph
adds the node name to every step by itself. The user's pseudonymised ID
is added with `feature/auth-rbac`, and personal data is masked before
sending with `feature/guardrails` (ASSUMPTIONS O2). Until then, only the
fictional Helix Bank data should be used with tracing on.
"""

from collections.abc import Sequence

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tracers.langchain import LangChainTracer
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

    def callbacks(self, *, tags: Sequence[str]) -> list[BaseCallbackHandler]:
        """The callbacks that send one run to LangSmith (none when tracing is off)."""
        if self._client is None:
            return []
        return [LangChainTracer(project_name=self.project, client=self._client, tags=list(tags))]

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
