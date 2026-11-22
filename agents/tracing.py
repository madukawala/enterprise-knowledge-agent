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

Every document search also gets a step of its own in the trace
(`TracedSearcher`): a `retriever` run named `pinecone_search`, under the
agent that searched. Its inputs are the query, the role, the collections
and the exact Pinecone filter, including the `access_level` rule the role
adds; its outputs are the chunks that came back. Without it, a trace would
show the agent's search plan but never the permission filter.
"""

from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from typing import Any, Protocol

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tracers.langchain import LangChainTracer
from langsmith import Client
from langsmith.run_trees import RunTree
from pydantic import BaseModel

from core.config import Settings
from core.logging import get_logger
from core.pii import mask_text
from rag.filters import build_filter
from rag.retrieval import SearchHit, SearchRequest, SearchResult
from security.roles import Role

log = get_logger(__name__)

SEARCH_RUN_NAME = "pinecone_search"


def project_id(client: Client, project: str) -> str | None:
    """The ID of a LangSmith project, found by its name.

    LangSmith wants it with every piece of feedback, to know which project
    the traced run is in (the SDK calls it `session_id`). Without it the
    feedback is still saved today, but the SDK warns that this will stop
    working. Returns None if the project cannot be read, so the caller can
    still send the feedback the old way.
    """
    try:
        return str(client.read_project(project_name=project).id)
    except Exception as error:  # tracing must never break the app
        log.warning("langsmith_project_lookup_failed", error_type=type(error).__name__)
        return None


def mask_for_trace(data: Any) -> Any:
    """Mask personal data and secrets in a run's inputs, outputs or metadata.

    Walks dicts and lists, and masks every string. Model calls reach
    LangSmith as nested dicts, but each agent step carries the graph state,
    which holds typed objects: chat messages, tool results, retrieved
    chunks, the answer. Those are turned into plain data first and then
    masked, so a tool result or a document passage is masked like the
    question is. LangSmith would turn them into the same plain data when it
    sends the trace, so the trace looks the same, only masked.
    """
    if isinstance(data, str):
        return mask_text(data)
    if isinstance(data, Mapping):
        return {key: mask_for_trace(value) for key, value in data.items()}
    if isinstance(data, list | tuple | set | frozenset):
        return [mask_for_trace(item) for item in data]
    if isinstance(data, BaseModel):
        return mask_for_trace(_model_data(data))
    if is_dataclass(data) and not isinstance(data, type):
        return mask_for_trace({f.name: getattr(data, f.name) for f in fields(data)})
    return data


def _model_data(model: BaseModel) -> Any:
    """A pydantic model as plain data, or as its fields if that fails."""
    try:
        return model.model_dump(mode="json")
    except Exception:  # a field JSON cannot hold: walk the fields instead
        return {name: getattr(model, name) for name in type(model).model_fields}


class Tracing:
    def __init__(self, settings: Settings) -> None:
        self.project = settings.langsmith_project
        self._client: Client | None = None
        # Looked up on the first feedback and kept: the project does not move.
        self._project_id: str | None = None
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
        if self._project_id is None:
            self._project_id = project_id(self._client, self.project)
        try:
            self._client.create_feedback(
                run_id=run_id,
                key="user_score",
                score=score,
                comment=mask_text(comment) if comment else None,
                session_id=self._project_id,
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


# ---------------------------------------------------------------
# The search step in a trace
# ---------------------------------------------------------------


class _Searcher(Protocol):
    async def search(self, request: SearchRequest, *, role: Role) -> SearchResult: ...


class TracedSearcher:
    """Wraps a searcher so each search is its own `retriever` step in the trace.

    The step is a child of the agent step that searched, and is sent through
    that trace's own client, so it is masked like every other step. When the
    search is not part of a traced run (tracing off, a test, an evaluation),
    nothing is sent: the step is never sent on its own through a default
    client, which would skip the masking.

    The filter shown is `build_filter(role, request.filters)`, the same call
    `HybridRetriever.search` makes, so it is exactly what Pinecone receives.
    A tracing problem is logged and never fails the search.
    """

    def __init__(self, searcher: _Searcher) -> None:
        self._searcher = searcher

    async def search(self, request: SearchRequest, *, role: Role) -> SearchResult:
        run = _start_search_run(request, role)
        try:
            result = await self._searcher.search(request, role=role)
        except BaseException as error:
            _end_search_run(run, error=type(error).__name__)
            raise
        _end_search_run(run, outputs=search_outputs(result))
        return result


def search_inputs(request: SearchRequest, role: Role) -> dict[str, Any]:
    """The inputs of the search step: what was asked, and the filter Pinecone gets."""
    return {
        "query": request.query,
        "role": role.value,
        "collections": [collection.value for collection in request.collections],
        "filter": build_filter(role, request.filters),
    }


def search_outputs(result: SearchResult) -> dict[str, Any]:
    """The outputs of the search step, in the document shape LangSmith shows for retrievers."""
    return {"documents": [_document(hit) for hit in result.hits]}


def _document(hit: SearchHit) -> dict[str, Any]:
    chunk = hit.chunk
    return {
        "type": "Document",
        "page_content": chunk.text,
        "metadata": {
            "record_id": hit.record_id,
            "doc_id": chunk.doc_id,
            "title": chunk.title,
            "section": chunk.section,
            "collection": hit.collection.value,
            "access_level": chunk.access_level.value,
            "department": chunk.department.value,
            "document_type": chunk.document_type.value,
            "created_date": chunk.created_date.isoformat(),
            "score": hit.score,
            "rerank_score": hit.rerank_score,
        },
    }


def _start_search_run(request: SearchRequest, role: Role) -> RunTree | None:
    try:
        # The agent step running now, if this run is traced. It carries the
        # trace's own client (the masked one) and project.
        parent = RunTree.from_runnable_config(None)
        if parent is None:
            return None
        run = parent.create_child(
            name=SEARCH_RUN_NAME, run_type="retriever", inputs=search_inputs(request, role)
        )
        run.post()
        return run
    except Exception as error:  # tracing must never break the app
        log.warning("search_trace_failed", error_type=type(error).__name__)
        return None


def _end_search_run(
    run: RunTree | None, *, outputs: dict[str, Any] | None = None, error: str | None = None
) -> None:
    if run is None:
        return
    try:
        run.end(outputs=outputs, error=error)
        run.patch()
    except Exception as failure:  # tracing must never break the app
        log.warning("search_trace_failed", error_type=type(failure).__name__)
