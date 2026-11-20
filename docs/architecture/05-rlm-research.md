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

The plan is data, not a general program. The sandbox exposes only these four functions, and their results are plain dictionaries.

```python
incidents = filter_by(
    collection="incidents",
    department="payments",
    created_after="2025-11-20",
)
quarters = batch(incidents, by="quarter")
plan = [
    fetch_section(q, query="payment failure outage root cause impact", top_k=8)
    for q in quarters
]
```

## Failure handling inside the sub-graph

- A sub-agent that fails or times out writes an error for its batch; the other batches still finish.
- The aggregator states which batches are missing, and the answer says so.
- Reaching the depth, fan-out or token limit stops further splitting and the agent works with what it has. It does not fail the run.
- Viewer users never reach this sub-graph: it is filtered out of their tool list and the supervisor refuses the route ([RBAC](../adr/0007-keycloak-rbac-and-data-access.md)).
