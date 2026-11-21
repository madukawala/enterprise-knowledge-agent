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
    W -- "PII masked first<br/>(core/pii.py)" --> PR
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
| Only short facts are saved (the departments asked about, and one masked line per answered question with the documents it cited), not whole answers | Small prompts, less sensitive data stored |
| Older turns of a long thread are replaced by a summary in the prompt (the full history stays in the checkpoint) | Keeps prompts within free-tier token limits |
| Each memory update is streamed to the activity panel as a `memory` event | The assignment asks for memory updates to be visible |
| Retention: threads and long-term items older than a configurable number of days (default 30) are deleted by an admin task | Data minimisation; documented in [ASSUMPTIONS.md](../ASSUMPTIONS.md) |

## As built

| Piece | Where |
|---|---|
| Postgres checkpointer and store (in memory without `POSTGRES_URL`) | `_open_memory` in [agents/service.py](../../agents/service.py) |
| Long-term memory: profile, interactions, recall, forget, retention | [agents/memory.py](../../agents/memory.py) |
| Memory writer node (the last node) and the running summary | [agents/nodes/memory_writer.py](../../agents/nodes/memory_writer.py), [agents/prompts/summary.md](../../agents/prompts/summary.md) |
| Recall into the answer prompt, as `<memory>` data | `ResponseAgent._recall` in [agents/nodes/response.py](../../agents/nodes/response.py) |
| Thread owners and titles (namespace `("threads",)`) | [api/threads.py](../../api/threads.py) |
| `GET /threads`, `GET /threads/{id}`, `DELETE /threads/{id}`, `GET /me/memory`, `DELETE /me/memory` | [api/routes/threads.py](../../api/routes/threads.py) |
| Retention clean-up | `POST /admin/memory/cleanup` in [api/routes/admin.py](../../api/routes/admin.py) |
| Earlier conversations and "What I remember" in the sidebar | [ui/app.py](../../ui/app.py) |

| Setting | Default | What it does |
|---|---|---|
| `MEMORY_ENABLED` | `true` | Off: nothing is saved to long-term memory (conversations are still kept) |
| `MEMORY_RETENTION_DAYS` | `30` | What the clean-up deletes |
| `MEMORY_SUMMARY_AFTER_MESSAGES` | `12` | When a thread gets a running summary |

Checked in the running stack: a conversation, its owner and the remembered question survive an API restart, and a follow-up after the restart continues the same conversation.
