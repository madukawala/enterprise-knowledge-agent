# 5. Recursive research (RLM)

How the research sub-graph answers the assignment's example: *"Summarize payment-failure outages over the last year."* Decisions: [ADR-0006](../adr/0006-langgraph-agents-and-deep-agent-patterns.md), [ADR-0011](../adr/0011-rlm-python-search-plans-in-sandbox.md).

## The six steps

| RLM step | What happens |
|---|---|
| 1. Explore document collections | Read the document catalog: namespaces, document counts, departments, date ranges. No documents are loaded yet. |
| 2. Generate a Python search plan | The LLM writes a short plan that may only call `search`, `filter_by`, `batch` and `fetch_section`. An AST validator rejects anything else (imports, attributes, loops without a bound, file or network access). |
| 3. Decompose the task | The plan runs in the sandbox and returns batches, e.g. one per quarter or per payment service. |
| 4. Retrieve targeted sections | Each batch calls `fetch_section` with metadata filters (`document_type=incident`, `department=payments`, date in range), so only matching chunks are loaded. |
| 5. Call sub-agents recursively | One sub-agent per batch, run in parallel with LangGraph's `Send`. A sub-agent with too much material splits its batch again. Limits: depth 3, fan-out 5, a token budget per run. |
| 6. Aggregate the results | Map-reduce: each sub-agent returns structured findings; the aggregator groups recurring root causes and writes the summary with citations. |

## Sequence

```mermaid
sequenceDiagram
    actor A as Analyst
    participant S as Supervisor
    participant R as Research agent
    participant L as LLM (Gemini; OpenRouter fallback)
    participant X as Python sandbox
    participant C as Document catalog
    participant P as Pinecone
    participant B as Batch sub-agents (max 5, parallel)

    A->>S: Summarize payment-failure outages over the last year
    S->>S: intent = deep research, role analyst allowed
    S->>R: delegate (limits: depth 3, fan-out 5, token budget)
    R->>C: 1. explore collections and metadata values
    C-->>R: incidents: 18 docs, departments, date range
    R->>L: 2. write a search plan (allow-listed API only)
    L-->>R: Python plan
    R->>R: AST validation
    R->>X: 3. run the plan
    X-->>R: 4 batches (one per quarter) with filters
    par one sub-agent per batch
        R->>B: 5. investigate batch
        B->>P: 4. fetch_section(incident, payments, date range)
        P-->>B: matching chunks with sources
        B->>L: extract incident, date, root cause, impact
        opt batch still too large
            B->>B: split again (depth + 1, max 3)
        end
        B-->>R: findings + citations (or an error for this batch)
    end
    R->>L: 6. aggregate: group recurring root causes
    L-->>R: summary with citations
    R-->>S: summary, gaps, per-batch errors
    S-->>A: streamed answer + "How I got this answer"
```

## Example search plan

The plan is data, not a general program. The sandbox offers only four functions, and they only describe work: `search()` returns a dictionary describing one search, which the research agent then runs itself with the user's role filter. This is the plan the model writes for the example question (asked on 2026-11-21):

```python
quarters = [
    ("2025-11-21", "2026-02-20", "Q1"),
    ("2026-02-21", "2026-05-20", "Q2"),
    ("2026-05-21", "2026-08-20", "Q3"),
    ("2026-08-21", "2026-11-21", "Q4"),
]
result = [
    search(
        "payment failure outage root cause impact",
        ["incidents"],
        filter_by(department="payments", date_from=start, date_to=end),
        label=name,
    )
    for start, end, name in quarters
]
```

The functions are listed in [sandbox/README.md](../../sandbox/README.md#using-it). A batch whose search comes back full (8 results) and covers more than 45 days is split into two halves of its date range, one level deeper.

## As built

| Piece | Where |
|---|---|
| The research agent (all six steps) | [agents/nodes/research.py](../../agents/nodes/research.py) |
| Plan and batch prompts | [agents/prompts/research_plan.md](../../agents/prompts/research_plan.md), [research_batch.md](../../agents/prompts/research_batch.md) |
| Plan validation and execution | [sandbox/validator.py](../../sandbox/validator.py) (plan mode), [sandbox/child.py](../../sandbox/child.py) |
| Limits | `RESEARCH_MAX_DEPTH` (3), `RESEARCH_MAX_FANOUT` (5), `RESEARCH_TOKEN_BUDGET` (40,000), `RESEARCH_MAX_SOURCES` (20), `RESEARCH_PARALLEL_CALLS` (2 model calls at a time) and `RESEARCH_MAX_EXTRACTIONS` (4 batches get model-written findings), so a research question stays within the free tiers' per-minute limits |
| Activity panel | the `research` event: plan source, the plan's code, every batch with its depth, sources and findings |

The batch sub-agents run in parallel inside the research node (`asyncio.gather`), not as separate graph nodes: the recursion depth changes per run, and keeping it inside one node keeps the graph and its streamed events simple. Each sub-agent's search and model call still appear in the LangSmith trace.

Every chunk a batch retrieves goes through the same cleaning and injection screening as the retrieval agent's, and every finding the model writes is scanned too. Findings reach the answer prompt inside `<findings>` tags, which the system prompt treats as data.

## Failure handling inside the sub-graph

- A sub-agent that fails or times out writes an error for its batch; the other batches still finish.
- The aggregator states which batches are missing, and the answer says so.
- Reaching the depth, fan-out or token limit stops further splitting and the agent works with what it has. It does not fail the run.
- A plan the sandbox refuses is written again once, with the reason. If it is refused again, or there is no sandbox, the fixed plan is used: the question over the last twelve months, by quarter.
- If every batch fails, the supervisor falls back to one wide search with the retrieval agent.
- Viewer users never reach this agent: the supervisor checks the role matrix after the model picks the intent, and a viewer's research question runs as a plain document question ([RBAC](../adr/0007-keycloak-rbac-and-data-access.md)).
