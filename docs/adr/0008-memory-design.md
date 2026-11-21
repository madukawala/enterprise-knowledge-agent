# ADR-0008: Postgres checkpointer for threads, Postgres store for long-term memory

- **Status:** Accepted
- **Date:** 2026-11-20

## Context

The assignment asks the system to remember user context, previous questions and relevant past interactions, to work across multiple turns, and to **explain the memory design**. Long-term memory is a bonus. Free-tier LLMs have limited context and tokens, so prompts must stay small.

## Decision

| Memory | What is kept | Where |
|---|---|---|
| Short-term | Every turn and the agent state of a thread | LangGraph **Postgres checkpointer**, keyed by `thread_id` |
| Conversation summary | A running summary that replaces older turns in the prompt once a thread is long | Same checkpoint state |
| Long-term profile | Department, preferred level of detail, role | LangGraph **Postgres store**, namespace `(user_id, "profile")` |
| Long-term interactions | One-line summaries of notable past questions | Store, namespace `(user_id, "interactions")`, searched by similarity |

- A dedicated **memory writer** node at the end of the graph decides what is worth saving, so memory writes are visible (an SSE `memory` event) and testable.
- `user_id` comes from the token only. A thread belongs to its creator; other users get 404.
- **PII is masked with Presidio** before anything goes into long-term memory.
- Retention is configurable (default 30 days) and enforced by an admin clean-up task.

## Changes during implementation (`feature/memory`)

- **The memory writer decides in code, not with a model call.** An answered question is saved as one masked line with the documents it cited; refusals, small talk and failed answers are not. The departments the user searched are counted into the profile. This costs no model quota and is easy to test. "Preferred level of detail" is not tracked.
- **Recall is by shared words**, not similarity search. The store holds one-line items per user; the ones sharing at least two words with the new question are recalled (up to three), with the profile. No embedding model or index is needed.
- **Keyed by the pseudonym** (`u_...`), not the user ID, like every other per-user record (rate limits, logs, traces).
- **Masking uses core/pii.py**, not Presidio (ADR-0016).
- **Thread owners are kept in the same store** (namespace `("threads",)`), with each thread's title and dates. Owners kept only in memory would let anyone claim a thread after a restart, now that the conversations themselves survive it. This also gives `GET /threads`.
- **The summary** is written by one model call when a thread has more than 12 messages and at least four new ones since the last summary; the newest six messages always stay in full.
- **Retention** is an admin endpoint (`POST /admin/memory/cleanup`, permission `admin_memory_cleanup`) rather than a scheduled task; a scheduler can call it.
- Users see and clear their own memory with `GET /me/memory` and `DELETE /me/memory`, and delete a conversation with `DELETE /threads/{id}`.

## Consequences

- **Positive:** one database for checkpoints, memory, feedback and the audit log; LangGraph supports both natively; threads survive API restarts.
- **Positive:** the checkpointer also provides human-approval interrupt and resume.
- **Positive:** summaries and short memory items keep prompts within free-tier limits.
- **Negative:** what the memory writer saves depends on an LLM decision. Saved items are short and shown in the activity panel, so a bad one is visible, and users can clear their memory.

## Alternatives considered

| Option | Why not |
|---|---|
| In-memory checkpointer | Lost on restart; not enterprise-grade |
| Redis for conversation state | Already used for rate limiting, but adds durability concerns; Postgres is the system of record |
| Storing full past answers as memory | Large prompts, more sensitive data kept |
| A vector DB namespace for memory | Mixes user data with the knowledge base and its access rules |

## Related

[08-memory](../architecture/08-memory.md) · memory isolation tests in [TESTING.md](../TESTING.md)
