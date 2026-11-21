"""Research agent: recursive research across many documents (the RLM steps).

For questions such as "summarize payment-failure outages over the last
year", one search is not enough. The research agent follows the six steps
in docs/architecture/05-rlm-research.md:

1. **Explore the collections.** Read the document catalog: how many
   documents each collection holds, which departments, which dates. Only
   the access levels the user's role may read are counted.
2. **Write a Python search plan.** One model call writes a short plan that
   may only call `search`, `filter_by`, `batch` and `fetch_section`.
3. **Decompose.** The sandbox checks and runs the plan (ADR-0011). The plan
   functions only describe searches; the result is a list of batches.
4. **Retrieve targeted sections.** Each batch is one search, run here with
   the user's role filter, so a plan can never widen what the user reads.
5. **Sub-agents, recursively.** One sub-agent per batch, in parallel (at
   most `fan-out` of them). A sub-agent whose search comes back full and
   covers a long date range splits it in two and recurses, down to the
   depth limit. Each sub-agent then asks the model for the batch's
   findings (date, cause, impact), while the run's token budget lasts.
6. **Aggregate.** The sources of every batch are collected (no repeats, up
   to a limit) and the findings are passed on, as data, to the response
   agent, which writes the summary with citations. The validator checks it
   like any other answer.

Failure containment: a plan the sandbox rejects is written again once with
the reason; if it fails again, or there is no sandbox, a fixed plan is used
(the last twelve months by quarter). A batch that fails is reported and the
others still count. Hitting the depth, fan-out or token limit stops further
work, never the run. If the whole agent fails, the supervisor falls back
to one wide search.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from agents.context import prompt
from agents.nodes.base import AgentNode, StateUpdate
from agents.nodes.retrieval import screen
from agents.state import (
    AgentName,
    AgentState,
    BatchReport,
    ErrorKind,
    LLMCall,
    ResearchSummary,
    Step,
)
from core.errors import LLMUnavailableError, RetrievalError, ToolError
from core.logging import SECURITY_LOGGER, get_logger
from rag.catalog import Catalog
from rag.filters import SearchFilters
from rag.retrieval import SearchHit, SearchRequest
from rag.schema import Collection, Department
from security.guardrails.injection import scan
from security.roles import Role, allowed_access_levels

log = get_logger(__name__)
security_log = get_logger(SECURITY_LOGGER)

# Hits per batch search. A batch that comes back full may hold more.
BATCH_TOP_K = 8
# Only a batch covering more than this many days is split further.
MIN_SPLIT_DAYS = 45
MAX_FINDINGS_PER_BATCH = 8


class ResearchCode(BaseModel):
    """What the model returns for step 2."""

    code: str = Field(description="The Python search plan. Python only, no Markdown fences.")
    reason: str = Field(default="", description="One short sentence on how the work is split.")


class Finding(BaseModel):
    summary: str = Field(description="One plain sentence.")
    date: str | None = Field(default=None, description="YYYY-MM-DD, if the source gives one.")
    cause: str | None = Field(default=None, description="The root cause, if stated.")
    impact: str | None = Field(default=None, description="The impact, if stated.")
    source: int = Field(description="The number of the source it comes from.")


class BatchFindings(BaseModel):
    """What the model returns for one batch in step 5."""

    findings: list[Finding] = Field(default_factory=list, max_length=MAX_FINDINGS_PER_BATCH)


# ---------------------------------------------------------------
# Step 1: what the collections hold, for this role
# ---------------------------------------------------------------


def describe_catalog(catalog: Catalog | None, role: Role) -> str:
    """One line per collection: documents, departments and dates the role may read."""
    if catalog is None:
        return ""
    levels = allowed_access_levels(role)
    lines = []
    for collection, summary in sorted(catalog.collections.items()):
        readable = [s for level, s in summary.by_access_level.items() if level in levels]
        if not readable:
            continue
        documents = sum(s.documents for s in readable)
        departments = sorted({d.value for s in readable for d in s.departments})
        earliest = min(s.earliest for s in readable)
        latest = max(s.latest for s in readable)
        lines.append(
            f"{collection.value}: {documents} documents, {earliest} to {latest}, "
            f"departments: {', '.join(departments)}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------
# Step 3: from the plan's output to batches
# ---------------------------------------------------------------


@dataclass(frozen=True)
class Batch:
    label: str
    request: SearchRequest
    depth: int = 1


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def to_batches(ops: Any, *, limit: int) -> tuple[list[Batch], int]:
    """Turn the plan's result into batches. Returns them and how many were dropped.

    Anything the plan returns is checked here, as it comes from model-written
    code: unknown collections and departments are ignored, bad dates are
    dropped, and there is no access-level field to set.
    """
    batches: list[Batch] = []
    dropped = 0
    for op in ops if isinstance(ops, list) else []:
        batch = _to_batch(op)
        if batch is None:
            dropped += 1
        elif len(batches) < limit:
            batches.append(batch)
        else:
            dropped += 1
    return batches, dropped


def _to_batch(op: Any) -> Batch | None:
    if not isinstance(op, dict):
        return None
    try:
        if op.get("op") == "search":
            raw_filters = op.get("filters")
            filters: dict[str, Any] = raw_filters if isinstance(raw_filters, dict) else {}
            departments = tuple(
                Department(d) for d in [filters.get("department")] if d in set(Department)
            )
            collections = tuple(
                Collection(c) for c in op.get("collections") or [] if c in set(Collection)
            )
            request = SearchRequest(
                query=str(op.get("query", ""))[:300],
                collections=collections or tuple(Collection),
                filters=SearchFilters(
                    departments=departments,
                    date_from=_date(filters.get("date_from")),
                    date_to=_date(filters.get("date_to")),
                ),
                top_k=BATCH_TOP_K,
            )
            return Batch(label=str(op.get("label") or request.query)[:80], request=request)
        if op.get("op") == "fetch_section":
            doc_id = str(op.get("doc_id", ""))
            section = op.get("section")
            request = SearchRequest(
                query=str(section or doc_id)[:300],
                filters=SearchFilters(doc_ids=(doc_id,)),
                top_k=BATCH_TOP_K,
            )
            return Batch(label=f"{doc_id} {section or ''}".strip()[:80], request=request)
    except (ValidationError, ValueError):
        return None
    return None


def fallback_ops(question: str, today: date) -> list[dict[str, Any]]:
    """The fixed plan: the question, over the last twelve months, by quarter."""
    ops = []
    end = today
    for _ in range(4):
        start = end - timedelta(days=91)
        ops.append(
            {
                "op": "search",
                "query": question[:300],
                "collections": [],
                "filters": {"date_from": start.isoformat(), "date_to": end.isoformat()},
                "label": f"{start} to {end}",
            }
        )
        end = start - timedelta(days=1)
    return list(reversed(ops))


def split(batch: Batch) -> list[Batch] | None:
    """Two halves of the batch's date range, or None if it cannot be split."""
    filters = batch.request.filters
    if filters.date_from is None or filters.date_to is None:
        return None
    days = (filters.date_to - filters.date_from).days
    if days <= MIN_SPLIT_DAYS:
        return None
    middle = filters.date_from + timedelta(days=days // 2)
    halves = (
        (filters.date_from, middle),
        (middle + timedelta(days=1), filters.date_to),
    )
    return [
        Batch(
            label=f"{batch.label} ({start} to {end})",
            request=batch.request.model_copy(
                update={"filters": filters.model_copy(update={"date_from": start, "date_to": end})}
            ),
            depth=batch.depth + 1,
        )
        for start, end in halves
    ]


# ---------------------------------------------------------------
# Steps 4 to 6
# ---------------------------------------------------------------


@dataclass
class Budget:
    """The run's model tokens and calls. Shared by every sub-agent of one question.

    Free model tiers allow only a few requests a minute, so the sub-agents
    also share a limit on calls at the same time (`slots`) and on how many
    batches get model-written findings (`extractions_left`).
    """

    left: int
    extractions_left: int = 4
    slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(2))
    calls: list[LLMCall] = field(default_factory=list)
    stopped: bool = False
    skipped: int = 0

    def spend(self, call: LLMCall) -> None:
        self.calls.append(call)
        self.left -= call.input_tokens + call.output_tokens
        if self.left <= 0:
            self.stopped = True


@dataclass
class BatchResult:
    reports: list[BatchReport]
    hits: list[SearchHit]
    findings: list[str]
    # Documents left out because they carried instructions for the assistant.
    dropped: list[str] = field(default_factory=list)


class ResearchAgent(AgentNode):
    name = AgentName.RESEARCH

    async def run(self, state: AgentState) -> StateUpdate:
        context = self.context
        role = state["role"]
        question = state["question"]
        budget = Budget(
            left=context.research_token_budget,
            extractions_left=context.research_max_extractions,
            slots=asyncio.Semaphore(context.research_parallel_calls),
        )
        steps: list[Step] = []
        limits: list[str] = []

        # 1. Explore
        overview = describe_catalog(context.catalog, role)
        if overview:
            collections = len(overview.splitlines())
            steps.append(
                self.step(f"Explored the document catalog: {collections} collections you can read.")
            )

        # 2 and 3. Plan, run it in the sandbox, turn it into batches
        ops, source, code, plan_notes = await self._plan(state, overview, budget)
        steps.extend(self.step(note) for note in plan_notes)
        batches, dropped = to_batches(ops, limit=context.research_max_fanout)
        if not batches:
            source = "fallback"
            batches, dropped = to_batches(
                fallback_ops(question, context.today()), limit=context.research_max_fanout
            )
            steps.append(self.step("The plan gave no usable searches, so used the fixed plan."))
        if dropped:
            limits.append(
                f"The research plan asked for more than {context.research_max_fanout} batches "
                "or included unusable searches; only the first ones were investigated."
            )
        steps.append(
            self.step(
                f"Split the work into {len(batches)} batches: "
                + "; ".join(batch.label for batch in batches)
                + "."
            )
        )

        # 4 and 5. Sub-agents, in parallel
        results = await asyncio.gather(
            *(self._investigate(batch, state, budget) for batch in batches)
        )

        # 6. Aggregate
        reports = [report for result in results for report in result.reports]
        # Take sources from every batch in turn, so a long period is covered
        # from start to end even when the early batches found a lot.
        hits = _unique(
            _interleave([result.hits for result in results]), context.research_max_sources
        )
        findings = [finding for result in results for finding in result.findings]
        left_out = sorted({doc for result in results for doc in result.dropped})
        if left_out:
            limits.append(
                f"{len(left_out)} document{'s were' if len(left_out) != 1 else ' was'} left out "
                f"because it contained instructions aimed at the assistant ({', '.join(left_out)})."
            )
        failed = [report for report in reports if report.error]
        if failed:
            limits.append(
                f"{len(failed)} research batch{'es' if len(failed) != 1 else ''} could not be "
                f"searched ({', '.join(r.label for r in failed)}), so the summary may miss them."
            )
        if budget.skipped:
            steps.append(
                self.step(
                    f"{budget.skipped} batch{'es' if budget.skipped != 1 else ''} gave their "
                    "sources to the answer without separate findings, to stay within the "
                    "model's request limits."
                )
            )
        if budget.stopped:
            limits.append(
                "The research reached its token budget, so some batches were not analysed "
                "in detail."
            )
        depth = max((report.depth for report in reports), default=1)
        steps.append(
            self.step(
                f"Investigated {len(reports)} batches (depth {depth}): {len(hits)} sources "
                f"and {len(findings)} findings."
            )
        )
        summary = ResearchSummary(
            plan_source=source,
            plan_code=code,
            batches=tuple(reports),
            max_depth=depth,
            sources=len(hits),
            tokens_used=context.research_token_budget - budget.left,
            stopped_by_budget=budget.stopped,
        )
        log.info(
            "research_finished",
            plan_source=source,
            batches=len(reports),
            failed=len(failed),
            depth=depth,
            sources=len(hits),
            findings=len(findings),
        )
        update: StateUpdate = {
            "research": summary,
            "hits": hits,
            "findings": findings,
            "steps": steps,
            "limits": limits,
            "completed": [self.name],
            "llm_calls": budget.calls,
        }
        if not hits and len(failed) == len(reports):
            update["errors"] = {
                self.name.value: self.failure(
                    ErrorKind.UNAVAILABLE, "Document search did not respond for the research."
                )
            }
        return update

    async def _plan(
        self, state: AgentState, overview: str, budget: Budget
    ) -> tuple[Any, str, str | None, list[str]]:
        """The plan's search operations, where they came from, the code, and notes."""
        context = self.context
        if context.sandbox is None:
            return (
                fallback_ops(state["question"], context.today()),
                "fallback",
                None,
                ["No Python sandbox is available, so used the fixed plan (by quarter)."],
            )
        notes: list[str] = []
        feedback: list[str] = []
        for attempt in (1, 2):
            try:
                plan, call = await context.gateway.structured(
                    ResearchCode,
                    prompt(
                        "research_plan",
                        state["question"],
                        state.get("messages", []),
                        today=context.today(),
                        history=context.history_messages,
                        summary=state.get("summary") or "",
                        overview=overview,
                        notes=feedback,
                        values={"max_batches": context.research_max_fanout},
                    ),
                    purpose="research_plan",
                )
            except LLMUnavailableError:
                notes.append("Could not reach the model to plan, so used the fixed plan.")
                break
            budget.spend(call)
            code = _strip_fences(plan.code)
            try:
                result = await context.sandbox.run(code, mode="plan")
            except ToolError:
                notes.append("The Python sandbox did not answer, so used the fixed plan.")
                break
            context.metrics.tool_runs.labels(tool="research_plan", status=result.status).inc()
            if result.ok:
                notes.append(
                    f"Wrote a Python search plan and ran it in the sandbox ({attempt} "
                    f"attempt{'s' if attempt > 1 else ''})."
                )
                return result.result, "sandbox", code, notes
            log.info("research_plan_refused", attempt=attempt, reason=(result.error or "")[:200])
            feedback = [f"Your previous plan was refused: {result.error}"]
            notes.append(f"The sandbox refused the plan: {result.error}")
        else:
            notes.append("The plan was refused twice, so used the fixed plan.")
        return fallback_ops(state["question"], context.today()), "fallback", None, notes

    async def _investigate(self, batch: Batch, state: AgentState, budget: Budget) -> BatchResult:
        """One sub-agent: search the batch, split it if it is too big, then extract findings."""
        context = self.context
        try:
            result = await context.searcher.search(batch.request, role=state["role"])
        except RetrievalError:
            return BatchResult(
                reports=[
                    BatchReport(
                        label=batch.label,
                        depth=batch.depth,
                        hits=0,
                        findings=0,
                        error="search did not respond",
                    )
                ],
                hits=[],
                findings=[],
            )
        # The same cleaning and injection screening as the retrieval agent.
        hits, screened_out = screen(result.hits)
        dropped = [doc_id for doc_id, _ in screened_out]
        for doc_id, finding in screened_out:
            for category in finding.categories:
                context.metrics.guardrail_blocks.labels(
                    guard="retrieved", category=category.value
                ).inc()
            security_log.warning("indirect_injection_dropped", doc_id=doc_id, agent="research")

        # Too much material for one batch: split it and recurse.
        full = len(hits) >= (batch.request.top_k or BATCH_TOP_K)
        halves = split(batch) if full and batch.depth < context.research_max_depth else None
        if halves and not budget.stopped:
            children = await asyncio.gather(
                *(self._investigate(half, state, budget) for half in halves)
            )
            parent = BatchReport(
                label=batch.label, depth=batch.depth, hits=len(hits), findings=0, split=True
            )
            return BatchResult(
                reports=[parent, *(r for child in children for r in child.reports)],
                hits=[hit for child in children for hit in child.hits],
                findings=[f for child in children for f in child.findings],
                dropped=[*dropped, *(d for child in children for d in child.dropped)],
            )

        findings = await self._findings(batch, hits, state, budget) if hits else []
        return BatchResult(
            reports=[
                BatchReport(
                    label=batch.label, depth=batch.depth, hits=len(hits), findings=len(findings)
                )
            ],
            hits=hits,
            findings=findings,
            dropped=dropped,
        )

    async def _findings(
        self, batch: Batch, hits: Sequence[SearchHit], state: AgentState, budget: Budget
    ) -> list[str]:
        """Ask the model what this batch's sources say, while the budget lasts."""
        if budget.stopped:
            return []
        if budget.extractions_left <= 0:
            budget.skipped += 1
            return []
        budget.extractions_left -= 1
        try:
            async with budget.slots:
                found, call = await self.context.gateway.structured(
                    BatchFindings,
                    prompt(
                        "research_batch",
                        state["question"],
                        [],
                        today=self.context.today(),
                        history=0,
                        sources=hits,
                    ),
                    purpose="research_batch",
                )
        except LLMUnavailableError:
            return []
        budget.spend(call)
        lines = []
        for finding in found.findings[:MAX_FINDINGS_PER_BATCH]:
            if not 1 <= finding.source <= len(hits):
                continue  # a source the batch never had
            doc_id = hits[finding.source - 1].chunk.doc_id
            parts = [f"[{batch.label}] {finding.summary.strip()}"]
            parts += [f"date {finding.date}"] if finding.date else []
            parts += [f"cause: {finding.cause.strip()}"] if finding.cause else []
            parts += [f"impact: {finding.impact.strip()}"] if finding.impact else []
            line = "; ".join(parts) + f" (from {doc_id})"
            # Written by the model from document text: scanned like a document.
            if scan(line).flagged:
                security_log.warning("research_finding_dropped", doc_id=doc_id)
                continue
            lines.append(line)
        return lines


def _interleave(groups: Sequence[Sequence[SearchHit]]) -> list[SearchHit]:
    """The first hit of each group, then the second of each, and so on."""
    longest = max((len(group) for group in groups), default=0)
    return [group[i] for i in range(longest) for group in groups if i < len(group)]


def _unique(hits: Sequence[SearchHit], limit: int) -> list[SearchHit]:
    seen: set[str] = set()
    unique = []
    for hit in hits:
        if hit.record_id not in seen:
            seen.add(hit.record_id)
            unique.append(hit)
    return unique[:limit]


def _strip_fences(code: str) -> str:
    """Models sometimes wrap code in Markdown fences despite being asked not to."""
    text = code.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()
