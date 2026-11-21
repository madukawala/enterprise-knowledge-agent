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

Each run is tagged with the role, and carries the request ID, the thread
ID and the user's pseudonym (never their name or username) as metadata,
so a trace can be found from a log line and the other way round. The
run's ID is chosen by us and sent to the client in the first streamed
event, so the UI can link feedback to the trace later. LangGraph adds the
node name to every step by itself.

Personal data is masked before anything leaves the machine: the LangSmith
client is given `mask_for_trace` for the inputs, outputs and metadata of
every run, which passes every text through the masker (core/pii.py). The
question, the retrieved text and the answer reach LangSmith with e-mail
addresses, card numbers, phone numbers and secrets replaced by their kind.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tracers.langchain import LangChainTracer
from langsmith import Client

from core.config import Settings
from core.logging import get_logger
from core.pii import mask_text

log = get_logger(__name__)


def mask_for_trace(data: Any) -> Any:
    """Mask personal data and secrets in a run's inputs, outputs or metadata.

    Walks dicts and lists, and masks every string, including the text of
    chat messages, which LangSmith receives as nested dicts.
    """
    if isinstance(data, str):
        return mask_text(data)
    if isinstance(data, Mapping):
        return {key: mask_for_trace(value) for key, value in data.items()}
    if isinstance(data, list | tuple):
        return [mask_for_trace(item) for item in data]
    return data


class Tracing:
    def __init__(self, settings: Settings) -> None:
        self.project = settings.langsmith_project
        self._client: Client | None = None
        if settings.langsmith_tracing and settings.langsmith_api_key is not None:
            self._client = Client(
                api_key=settings.langsmith_api_key.get_secret_value(),
                api_url=settings.langsmith_endpoint,
                hide_inputs=mask_for_trace,
                hide_outputs=mask_for_trace,
                hide_metadata=mask_for_trace,
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

    def send_feedback(self, run_id: str, *, score: float, comment: str | None) -> bool:
        """Attach a user's thumbs up (1) or down (0) to the run's trace in LangSmith.

        Returns whether it was sent. Like every tracing call, a failure is
        logged and never reaches the user.
        """
        if self._client is None:
            return False
        try:
            self._client.create_feedback(
                run_id=run_id,
                key="user_score",
                score=score,
                comment=mask_text(comment) if comment else None,
            )
        except Exception as error:  # tracing must never break the app
            log.warning("langsmith_feedback_failed", error_type=type(error).__name__)
            return False
        return True

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
