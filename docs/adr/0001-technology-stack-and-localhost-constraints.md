# ADR-0001: Technology stack under the localhost and zero-cost constraints

- **Status:** Accepted
- **Date:** 2026-11-20

## Context

The assistant must be enterprise-grade, but it runs on one developer machine (Apple M1 Pro, 16 GB RAM) and every tool must be free. The assignment requires Streamlit, FastAPI, LangGraph, Pinecone and LangSmith. Pinecone and LangSmith exist only as cloud services. The default `python3` on the machine is 3.14, which several AI libraries do not support yet.

## Decision

| Layer | Choice |
|---|---|
| Language and packaging | Python **3.12**, managed with `uv` (`uv.lock` committed) |
| Frontend | Streamlit, reading server-sent events |
| Backend | FastAPI (async), Pydantic v2, `structlog`, `tenacity` |
| Orchestration | LangGraph `StateGraph` with sub-graphs ([ADR-0006](0006-langgraph-agents-and-deep-agent-patterns.md)) |
| LLMs | Claude Haiku (paid, low cost) → Gemini Flash (free tier) ([ADR-0017](0017-claude-haiku-primary-gemini-backup.md), which replaced [ADR-0002](0002-llm-selection-gemini-flash-with-openrouter-fallback.md)) |
| Retrieval | Pinecone Starter, hosted embeddings and reranker ([ADR-0003](0003-pinecone-single-hybrid-index.md), [0004](0004-embeddings-and-bm25.md), [0010](0010-reranking.md)) |
| Identity | Keycloak in Docker ([ADR-0007](0007-keycloak-rbac-and-data-access.md)) |
| State | Postgres (checkpoints, memory, feedback, audit), Redis (rate limiting) |
| Tools | FastMCP server with dummy enterprise data; Python sandbox container |
| Guardrails | Custom checks, LLM Guard, Microsoft Presidio |
| Observability | LangSmith, `structlog` JSON logs, Prometheus metrics, optional Grafana |
| Quality | `ruff`, `mypy`, `pytest`, `pre-commit`, GitHub Actions, `bandit`, `pip-audit`, `gitleaks`, Trivy |
| Runtime | Docker Compose ([ADR-0009](0009-docker-compose-runtime.md)) |

No model runs on the laptop: the LLMs, embeddings and reranker are all hosted APIs. That leaves the machine's memory for the Docker stack.

## Consequences

- **Positive:** every component is open source or has a free tier; the stack matches what a bank would run (OIDC identity, Postgres, Redis, containers, CI security scanning).
- **Positive:** pinning Python 3.12 avoids wheel and compatibility problems with LangChain, Presidio and LLM Guard.
- **Negative:** free tiers have rate and quota limits. This is handled with retries, back-off, fallbacks and the circuit breaker, and is documented in [ASSUMPTIONS.md](../ASSUMPTIONS.md).
- **Negative:** the app needs internet access for the LLMs, Pinecone and LangSmith. Tests replace all of these, so CI and `make test` run offline.

## Alternatives considered

| Option | Why not |
|---|---|
| Local LLMs with Ollama | Uses most of the 16 GB RAM next to the Docker stack, slow tool calling on a laptop. Dropped. |
| Paid commercial LLM APIs | Chosen later for the main model only, see the change below |
| Chroma / Qdrant instead of Pinecone | The assignment requires Pinecone |
| Hard-coded users (Option A) | Less realistic for a bank; Keycloak is free and gives real OIDC |

## Change after implementation (2026-11-21)

The zero-cost rule was relaxed for one component: the main language model. One day of testing used up the free daily limits of both free model providers, after which every answer fell back to the reduced mode. The main model is now Claude Haiku, which is paid but set up to cost about a tenth of a US cent per question; Gemini's free tier stays as the backup. Everything else is unchanged and still free. See [ADR-0017](0017-claude-haiku-primary-gemini-backup.md).

## Related

[02-containers](../architecture/02-containers.drawio.svg) · [ASSUMPTIONS.md](../ASSUMPTIONS.md)
