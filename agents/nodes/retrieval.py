"""Retrieval agent: picks where to search, rewrites the question, and searches.

1. **Plan the search (one model call).** The model returns a `SearchPlan`:
   a standalone query (follow-ups such as "and for cards?" are resolved
   from the conversation), the collections that fit, and optional
   department and date filters. "The last year" becomes real dates, because
   the prompt includes today's date.
2. **Check the plan in code.** Unknown departments and bad dates are
   dropped, the query is cut to the search limit, and the filters go
   through `SearchFilters`, which has no access-level field. The model
   cannot widen what the user may read.
3. **Search** with `HybridRetriever.search(request, role=...)`. The role
   comes from the state, which the caller set from the login token. The
   permission filter is added inside the search (rag/filters.py).
4. **Screen what came back.** Every chunk is cleaned (HTML, hidden text,
   invisible characters, images removed) and scanned with the same
   injection rules as the question. A chunk that carries instructions for
   the assistant (indirect prompt injection) is left out, and the answer
   says a source was left out. The model only ever sees cleaned text.

Failure containment:

- The model cannot be reached: search with the question as written, in
  every collection, and say so in the steps.
- Search is down (`RetrievalError`): write a retryable error to
  `errors["retrieval"]`. The supervisor tries once more, then the answer
  says document search is unavailable.
- Some collections fail: keep what the others found, and add a limit
  naming the missed collections.

The search plan is made once per question. A retry reuses it, so a retry
costs no model quota.
"""

from collections.abc import Sequence
from datetime import date

from pydantic import ValidationError

from agents.context import prompt
from agents.nodes.base import AgentNode, StateUpdate
from agents.state import (
    AgentError,
    AgentName,
    AgentState,
    ErrorKind,
    Intent,
    LLMCall,
    SearchPlan,
    SearchSummary,
)
from core.errors import LLMUnavailableError, RetrievalError
from core.logging import SECURITY_LOGGER, get_logger
from rag.filters import SearchFilters
from rag.retrieval import MAX_QUERY_CHARS, RerankStatus, SearchHit, SearchRequest
from rag.schema import Collection, Department
from security.guardrails.injection import Finding, scan
from security.guardrails.text import clean_retrieved

log = get_logger(__name__)

# Research questions need more material than a normal question. The full
# research agent (feature/rlm-research) replaces this wider search.
RESEARCH_TOP_K = 20

UNAVAILABLE_MESSAGE = "Document search is unavailable right now."


def fallback_search_plan(question: str) -> SearchPlan:
    """Used when the model cannot plan: the question as written, everywhere."""
    return SearchPlan(query=question[:MAX_QUERY_CHARS], collections=list(Collection))


def to_request(
    plan: SearchPlan, *, intent: Intent | None, relax: bool
) -> tuple[SearchRequest, list[str]]:
    """Turn the model's plan into a checked search request.

    Returns the request and notes about anything that was dropped.
    """
    notes: list[str] = []
    departments: list[Department] = []
    for name in plan.departments:
        try:
            departments.append(Department(name.strip().lower()))
        except ValueError:
            notes.append(f"Ignored an unknown department: {name[:40]!r}.")

    date_from, date_to = _parse_date(plan.date_from), _parse_date(plan.date_to)
    if (plan.date_from and date_from is None) or (plan.date_to and date_to is None):
        notes.append("Ignored a date the search could not read.")
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from

    filters = (
        SearchFilters()
        if relax
        else SearchFilters(departments=tuple(departments), date_from=date_from, date_to=date_to)
    )
    query = plan.query.strip()[:MAX_QUERY_CHARS]
    request = SearchRequest(
        query=query,
        collections=tuple(plan.collections) or tuple(Collection),
        filters=filters,
        top_k=RESEARCH_TOP_K if intent is Intent.RESEARCH else None,
    )
    return request, notes


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def describe(summary: SearchSummary) -> str:
    """One readable line for "How I got this answer"."""
    collections = ", ".join(c.value for c in summary.collections)
    filters = []
    if summary.filters.departments:
        filters.append("department " + ", ".join(d.value for d in summary.filters.departments))
    if summary.filters.date_from or summary.filters.date_to:
        start = summary.filters.date_from.isoformat() if summary.filters.date_from else "any"
        end = summary.filters.date_to.isoformat() if summary.filters.date_to else "today"
        filters.append(f"dates {start} to {end}")
    filter_text = f" with filters: {'; '.join(filters)}" if filters else ""
    reranked = " after re-ranking" if summary.rerank is RerankStatus.APPLIED else ""
    return (
        f'Searched {collections} for "{summary.query}"{filter_text}. '
        f"Found {summary.candidates} matches and kept {summary.hits}{reranked}."
    )


_security_log = get_logger(SECURITY_LOGGER)


def screen(hits: Sequence[SearchHit]) -> tuple[list[SearchHit], list[tuple[str, Finding]]]:
    """Clean each retrieved chunk, and leave out the ones carrying injected instructions.

    Returns the hits to use (with cleaned text) and, for each one left out,
    its document ID and what was found.
    """
    kept: list[SearchHit] = []
    dropped: list[tuple[str, Finding]] = []
    for hit in hits:
        text = clean_retrieved(hit.chunk.text)
        finding = scan(text)
        if finding.flagged:
            dropped.append((hit.chunk.doc_id, finding))
            continue
        if text != hit.chunk.text:
            hit = hit.model_copy(update={"chunk": hit.chunk.model_copy(update={"text": text})})
        kept.append(hit)
    return kept, dropped


class RetrievalAgent(AgentNode):
    name = AgentName.RETRIEVAL

    async def run(self, state: AgentState) -> StateUpdate:
        update: StateUpdate = {"retrieval_attempts": state.get("retrieval_attempts", 0) + 1}
        steps = []
        calls: list[LLMCall] = []

        search_plan = state.get("search_plan")
        if search_plan is None:
            search_plan, call = await self._plan_search(state)
            if call is None:
                steps.append(
                    self.step(
                        "Could not reach the language model to plan the search, so "
                        "searched every collection with the question as written."
                    )
                )
            else:
                calls.append(call)
            update["search_plan"] = search_plan

        plan = state.get("plan")
        relax = state.get("relax_filters", False)
        try:
            request, notes = to_request(
                search_plan, intent=plan.intent if plan else None, relax=relax
            )
        except ValidationError:
            # The model's plan made an invalid request (e.g. an empty query).
            request, notes = to_request(
                fallback_search_plan(state["question"]), intent=None, relax=relax
            )
            notes.append("The search plan was not usable, so the question was used as written.")

        try:
            result = await self.context.searcher.search(request, role=state["role"])
        except RetrievalError:
            update["errors"] = {
                self.name.value: self.failure(
                    ErrorKind.UNAVAILABLE, UNAVAILABLE_MESSAGE, retryable=True
                )
            }
            update["steps"] = [*steps, self.step("Document search did not respond.")]
            update["llm_calls"] = calls
            # "Completed" means this agent has had its turn. Whether to give
            # it another one is the supervisor's decision.
            update["completed"] = [self.name]
            return update

        summary = SearchSummary(
            query=request.query,
            collections=result.collections_searched,
            filters=request.filters,
            failed_collections=result.failed_collections,
            candidates=result.candidates,
            hits=len(result.hits),
            rerank=result.rerank,
            alpha=result.alpha,
            took_ms=result.took_ms,
            relaxed=relax,
        )
        hits, dropped = screen(result.hits)
        limits = []
        if dropped:
            for doc_id, finding in dropped:
                for category in finding.categories:
                    self.context.metrics.guardrail_blocks.labels(
                        guard="retrieved", category=category.value
                    ).inc()
                _security_log.warning(
                    "indirect_injection_dropped",
                    doc_id=doc_id,
                    categories=[c.value for c in finding.categories],
                )
            documents = ", ".join(sorted({doc_id for doc_id, _ in dropped}))
            limits.append(
                f"{len(dropped)} search result{'s were' if len(dropped) != 1 else ' was'} left "
                f"out because it contained instructions aimed at the assistant ({documents})."
            )
        if result.failed_collections:
            missed = ", ".join(c.value for c in result.failed_collections)
            limits.append(
                f"These collections did not answer in time, so results from them "
                f"may be missing: {missed}."
            )
        if result.rerank is RerankStatus.FAILED:
            limits.append("The re-ranker was unavailable, so results are in search-score order.")

        update.update(
            {
                "search": summary,
                "hits": hits,
                # A successful search clears an error from an earlier attempt.
                "errors": {self.name.value: None} if self._had_error(state) else {},
                "completed": [self.name],
                "steps": [*steps, self.step(describe(summary)), *(self.step(n) for n in notes)],
                "limits": limits,
                "llm_calls": calls,
            }
        )
        return update

    async def _plan_search(self, state: AgentState) -> tuple[SearchPlan, LLMCall | None]:
        context = self.context
        try:
            return await context.gateway.structured(
                SearchPlan,
                prompt(
                    "search_plan",
                    state["question"],
                    state.get("messages", []),
                    today=context.today(),
                    history=context.history_messages,
                    summary=state.get("summary") or "",
                ),
                purpose="search_plan",
            )
        except LLMUnavailableError:
            self.context.metrics.node_error(self.name, ErrorKind.UNAVAILABLE)
            return fallback_search_plan(state["question"]), None

    def _had_error(self, state: AgentState) -> bool:
        return self.name.value in state.get("errors", {})

    def on_error(self, state: AgentState, error: AgentError) -> StateUpdate:
        # A bug in this agent: count the attempt so the supervisor does not
        # loop, and answer without documents.
        return {
            "retrieval_attempts": state.get("retrieval_attempts", 0) + 1,
            "completed": [self.name],
        }
