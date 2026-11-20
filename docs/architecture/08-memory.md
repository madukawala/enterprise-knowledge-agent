# 8. Memory

Short-term memory per conversation thread, a running summary for long threads, and long-term memory per user. Design reasons: [ADR-0008](../adr/0008-memory-design.md).

```mermaid
flowchart TB
    subgraph Turn["Each turn"]
        Q["New question<br/>thread_id from the request<br/>user_id from the JWT"] --> L["Load thread checkpoint"]
        L --> RC["Recall: search this user's<br/>long-term memory for<br/>related past interactions"]
        RC --> G["Agent graph run"]
        G --> W["Memory writer node"]
    end

    subgraph ST["Short-term (per thread)"]
        CP[("LangGraph Postgres checkpointer<br/>key: thread_id<br/>messages + agent state")]
        SM["Running summary<br/>when the thread is long,<br/>older turns are summarised"]
    end

    subgraph LT["Long-term (per user)"]
        PR[("LangGraph Postgres store<br/>namespace: (user_id, profile)<br/>department, preferred detail")]
        HI[("namespace: (user_id, interactions)<br/>notable past questions<br/>and short outcomes")]
    end

    L --- CP
    W --> CP
    W --> SM
    SM --> CP
    W -- "PII masked first<br/>(Presidio)" --> PR
    W -- "PII masked first" --> HI
    RC --- HI
    RC --- PR
```

## Rules

| Rule | Why |
|---|---|
| `user_id` always comes from the token, never from the request body | One user can't read another user's threads or memory |
| A thread belongs to the user who created it; other users get 404 | Thread isolation |
| PII is masked before anything is saved to long-term memory | Data protection; memory must be safe to show back to the user |
| Only short facts are saved (preferences, department, a one-line summary of notable questions), not whole answers | Small prompts, less sensitive data stored |
| Older turns of a long thread are replaced by a summary in the prompt (the full history stays in the checkpoint) | Keeps prompts within free-tier token limits |
| Each memory update is streamed to the activity panel as a `memory` event | The assignment asks for memory updates to be visible |
| Retention: threads and long-term items older than a configurable number of days (default 30) are deleted by an admin task | Data minimisation; documented in [ASSUMPTIONS.md](../ASSUMPTIONS.md) |
