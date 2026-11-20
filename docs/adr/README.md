# Architecture decision records

An architecture decision record (ADR) is a short document about one important design decision. Each record explains:

- the situation that made a decision necessary;
- what was decided;
- what the decision makes better and what it costs;
- which other options were considered and why they were not chosen.

Together with [ASSUMPTIONS.md](../ASSUMPTIONS.md), these records explain the assumptions and trade-offs behind the system.

## Records

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-technology-stack-and-localhost-constraints.md) | The technology stack, chosen to run on one laptop at no cost | Accepted |
| [0002](0002-llm-selection-gemini-flash-with-openrouter-fallback.md) | Gemini Flash as the main language model, OpenRouter as the backup, with a circuit breaker | Accepted |
| [0003](0003-pinecone-single-hybrid-index.md) | One Pinecone hybrid index that combines the meaning-based and keyword scores | Accepted |
| [0004](0004-embeddings-and-bm25.md) | Pinecone-hosted `multilingual-e5-large` embeddings and BM25 fitted on our own documents | Accepted |
| [0005](0005-pinecone-namespaces.md) | One namespace per document collection; filters for everything else | Accepted |
| [0006](0006-langgraph-agents-and-deep-agent-patterns.md) | Agents built directly with LangGraph; the research agent is a sub-graph | Accepted |
| [0007](0007-keycloak-rbac-and-data-access.md) | Keycloak for login, one deny-by-default role matrix, document access by role | Accepted |
| [0008](0008-memory-design.md) | PostgreSQL for conversation memory and long-term memory | Accepted |
| [0009](0009-docker-compose-runtime.md) | Docker Compose as the only way to run the system | Accepted |
| [0010](0010-reranking.md) | Pinecone-hosted `bge-reranker-v2-m3`, skipped if it fails | Accepted |
| [0011](0011-rlm-python-search-plans-in-sandbox.md) | Research search plans written in restricted Python and run in the sandbox | Accepted |
| [0012](0012-architecture-diagrams.md) | draw.io for architecture pictures, Mermaid for flows, agent graph generated from code | Accepted |

## Adding a record

1. Copy [template.md](template.md) to a new file with the next number, for example `0013-short-title.md`.
2. Fill in each section in plain language.
3. Add a row to the table above.

Records are never deleted. If a later decision replaces one, change the old record's status to "Superseded by ADR-NNNN" and link to the new one.
