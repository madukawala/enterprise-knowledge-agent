# Assumptions and trade-offs

Where the assignment leaves something open, this file records the choice made and why. The larger decisions have their own records in [docs/adr](adr/README.md). This is a living document: each feature branch adds to it.

## Scope and business context

| # | Assumption | Reason |
|---|---|---|
| A1 | The organisation is a fictional commercial bank, **Helix Commercial Bank**. All documents, people, systems and incidents are invented. | The assignment asks for brand-safe behaviour for a commercial bank; fictional data keeps the demo free of real customer or employee information. |
| A2 | Users are internal staff (operations, engineering, risk, support), not bank customers. | The scenario is an internal knowledge assistant. Customer-facing answers would need different regulation and tone. |
| A3 | The system is a **proof of concept that runs on localhost**, but is built the way a production system would be (identity, RBAC, audit, observability, tests). | The assignment asks for an enterprise-grade POC and says to prioritise what matters for a POC. |
| A4 | POC priority: core flow first (retrieval, agents, streaming, RBAC, guardrails, RLM, memory, error handling), then bonus items (MCP server, human approval, feedback, long-term memory, evaluations). | The assignment marks the MCP server as lower priority; the delivery order follows that. |
| A5 | Answers are in English. The embedding model is multilingual, so other languages work for search, but prompts and guardrails are tuned for English. | Keeps evaluation and brand checks focused. |

## Data

| # | Assumption | Reason |
|---|---|---|
| D1 | The corpus is 70 Markdown documents written for the demo across six collections: policies, architecture documents, runbooks, incident reports, product specifications and meeting notes (about 560 chunks). See [data/README.md](../data/README.md). | Matches the business scenario; small enough to index on the free Pinecone plan. |
| D2 | Each document has front matter: `doc_id`, `title`, `department`, `document_type`, `access_level`, `created_date`. | Metadata filters and citations need it. Real document stores would supply it through connectors. |
| D3 | 16 incident reports describe payment failures in the 12 months to 20 November 2026, with five repeated root causes; two older ones fall outside that window. | So the assignment's RLM example ("summarize payment-failure outages over the last year") has a real, checkable answer, and a date filter visibly changes it. |
| D4 | Documents change rarely; re-indexing is a manual Admin action (`make ingest` or the admin tool). | No live document feed in scope. A real system would re-index on change events. |
| D5 | The MCP server serves dummy data (employee directory, service catalog, incident records) from JSON files. | The assignment allows a dummy-data MCP server. |
| D6 | Ingestion runs on the developer's machine (`make ingest`), not as a Docker service. Its output (`data/index/`) is not committed and is rebuilt from the documents. | It needs the Pinecone key only once, and the API container stays read-only. |
| D7 | Chunk sizes use an estimated token count, not the embedding model's tokenizer. | Avoids installing the tokenizer; the estimate errs on the high side and the embedding call truncates as a safety net ([ADR-0004](adr/0004-embeddings-and-bm25.md)). |
| D8 | BM25 uses no stemming and a short English stop-word list. | Keeps codes such as `INC-2026-003` intact; the dense vector covers word forms. |

## Identity and access

| # | Assumption | Reason |
|---|---|---|
| I1 | Keycloak stands in for the bank's identity provider (Option B). Three test users, one per role, are loaded from a realm file. | Real OIDC without external dependencies. |
| I2 | **Document access by role** (not defined in the assignment): Viewer reads `public` and `internal`; Analyst adds `confidential`; Administrator adds `restricted`. | The assignment defines tool permissions only; reading access must still be controlled for a bank. |
| I3 | Recursive research is limited to Analyst and Administrator, because it uses the Python analysis tool. | The assignment puts analytics tools at Analyst level and above. |
| I4 | Department-level restrictions are not enforced by role in the POC; `department` is a search filter, not a permission. | Keeps the role model to the three roles the assignment defines. The filter design allows adding it later. |

## Models and quotas

| # | Assumption | Reason |
|---|---|---|
| M1 | Gemini Flash (free tier, `gemini-3.6-flash`) is the primary LLM, and OpenRouter with a free non-Google model (`nvidia/nemotron-3.5-super-vl:free`, about 50 requests a day) is the fallback. Model names are set in `.env` and have no default in code, because free-tier models are retired often. | [ADR-0002](adr/0002-llm-selection-gemini-flash-with-openrouter-fallback.md) |
| M2 | Free tiers have per-minute and per-day limits. Retries, back-off, the circuit breaker, small research limits and a per-user daily token budget keep the demo within them. | Zero-cost constraint. |
| M3 | Free-tier providers may use submitted data to improve their services. OpenRouter's free models are only served by providers that may log prompts, which must be allowed in the account's privacy settings. Only fictional data is ever sent. A real bank would use paid no-training tiers or a private deployment. | Data protection. |
| M4 | Embeddings (`multilingual-e5-large`) and re-ranking (`bge-reranker-v2-m3`) are Pinecone-hosted and count against the Pinecone free plan. | [ADR-0004](adr/0004-embeddings-and-bm25.md), [ADR-0010](adr/0010-reranking.md) |
| M5 | A question normally costs three model calls (plan, search plan, answer). Routing after each agent is plain code, not a model call. | Keeps the demo inside free-tier daily limits and makes routing predictable. |

## Retrieval and research

| # | Assumption | Reason |
|---|---|---|
| R1 | Hybrid weighting `alpha` is 0.5, checked with the evaluation set (`make eval-retrieval`, 18 questions; results in [docs/evals/retrieval.md](evals/retrieval.md)). Every tested value from 0.3 to 0.9 found a good document in the top 5 for all 18 questions; 0.9 ranked one question slightly higher. `alpha` must be above 0, so pure keyword search is not offered. | Dense and sparse scores have different scales. At 0 the dense query vector would be all zeros, which Pinecone does not accept. |
| R2 | The RLM plan language has four functions (`search`, `filter_by`, `batch`, `fetch_section`). Research depth is limited to 3 and fan-out to 5. | Safety and free-tier quotas ([ADR-0011](adr/0011-rlm-python-search-plans-in-sandbox.md)). |
| R3 | Answers cite numbered sources (`[2]`), and a citation is valid only if its number points to a chunk retrieved in the same run. Answers do not quote; checking that a claim matches its source is measured by the faithfulness evaluation (`feature/observability-evals`). | Guards against hallucinated citations. |
| R4 | A search returns at most 3 chunks from one document (8 chunks in total by default). | Keeps several sources in the answer instead of one long document filling every slot. |
| R5 | If some collections fail or time out, search returns what the others found and lists the missed collections. It fails only when every collection fails or the question cannot be embedded. | A partial answer that says what is missing is more useful than no answer. |
| R6 | The retrieval agent chooses the collections and rewrites the question with the language model. If the model is unavailable, it searches all six collections with the question as written. | A slower but complete search is better than no search. |
| R7 | Retrieval and evaluation metrics count documents, not chunks. | What an answer needs is the right document; three chunks of one runbook are one source. |
| R8 | The evaluation question set was written by the same person who wrote the documents. | It checks the search works as designed, not how it would do on real staff questions. A real project would collect questions from users. |

## Agents

| # | Assumption | Reason |
|---|---|---|
| AG1 | Until the research agent (`feature/rlm-research`) and the tool agent (`feature/python-sandbox`, `feature/mcp-server`) exist, research questions get one wider search (20 chunks) and enterprise-data questions are answered from the documents. The answer says so. | Every question type gets a useful, honest answer from the first agents branch on. |
| AG2 | Refusals, out-of-scope replies, "nothing found" and "search unavailable" are fixed texts written in code, not generated. | They must be safe and consistent, and a model adds nothing to them. |
| AG3 | A draft that fails the answer checks is rewritten once. If it fails again, the user gets the list of the most relevant sources instead. | Bounded cost and time; a bad answer is never shown. |
| AG4 | The agents see the last 6 messages of the conversation (`AGENT_HISTORY_MESSAGES`). Summaries of longer threads come with `feature/memory`. | Enough for follow-up questions, small enough for free-tier limits. |
| AG5 | Conversations are kept in memory for the life of the process until `feature/memory` adds the Postgres checkpointer. | Lets follow-ups work now, with no schema to migrate later. |
| AG6 | "The last year" and similar phrases are resolved against the server's date, which is given to the model in the prompt. | Date filters need real dates; the model has no clock. |

## Chat stream

| # | Assumption | Reason |
|---|---|---|
| ST1 | Streamed tokens are a preview. The final answer is the `response_end` event, and a `response_start` tells the client to clear the text (a rewrite, or a model failing half-way). | The validator checks the whole answer, so it can only approve it at the end. Holding every token until then would make the user wait 10 to 40 seconds for the first word ([ADR-0013](adr/0013-streaming-with-server-sent-events.md)). |
| ST2 | Until login exists (`feature/auth-rbac`), the role comes from an `X-Dev-Role` header (default `viewer`), refused when `APP_ENV=production`. Conversations belong to a role until then, and to a user after. | The streaming API and the UI can be built and tested before Keycloak is wired in, without ever accepting a role from the request body. |
| ST3 | One whole answer may take at most 120 seconds (`CHAT_TIMEOUT_SECONDS`); a keep-alive line is sent after 15 quiet seconds. | A normal question takes 10 to 40 seconds on the free tiers, with a 30-second limit per model call. Anything slower is stuck. |
| ST4 | A conversation answers one question at a time; a second one gets 409. | Two runs writing to one conversation would mix up its history. The chat UI turns its input into a Stop button while an answer streams, so it never sends a second question. |
| ST5 | When the client disconnects, the run is cancelled at once, even mid-answer. Nothing is saved for the half-finished question. | No quota is spent on answers nobody reads. Resuming abandoned answers is not in scope. |

## Chat UI

| # | Assumption | Reason |
|---|---|---|
| U1 | The conversation list in the sidebar holds the conversations of the current browser tab and is lost on reload. `feature/memory` stores threads in Postgres and adds `GET /threads`. | Keeping threads on the server needs the user ID from login and a store, which come with later branches ([ADR-0014](adr/0014-streamlit-chat-ui.md)). |
| U2 | Until sign-in exists, the sidebar has a role picker that sends `X-Dev-Role`. A conversation keeps the role it started with; changing the role starts a new conversation. | The API ties a thread to its owner (ST2). Mixing roles in one thread would give 404. |
| U3 | Sources show the document ID, title and section, without a link to the document. | There is no document endpoint yet; one would need its own role check. |
| U4 | While an answer streams, any click (Stop, another conversation, an example question) stops it first. | That is how Streamlit runs a page; stopping also cancels the agents, so no quota is wasted. |
| U5 | The UI shows what the API sends and does not re-check access. Every rule (roles, thread owners, limits) is enforced by the API. | The UI is a thin client; the browser and the UI server are not trusted to enforce anything. |
| U6 | Thumbs up/down and the approval step are not shown yet; they arrive with `feature/hitl-feedback`. | Buttons that do nothing would mislead the user. The run ID they will use is already in the panel. |

## Memory

| # | Assumption | Reason |
|---|---|---|
| ME1 | Threads and long-term memory are kept for 30 days by default (configurable) and deleted by an admin clean-up task. | Data minimisation. |
| ME2 | Long-term memory holds short facts only (profile, preferences, one-line summaries of notable questions), with PII masked. | Smaller prompts, less sensitive data stored. |

## Security

| # | Assumption | Reason |
|---|---|---|
| S1 | The app is served over plain HTTP on localhost. In production, TLS would end at an ingress or reverse proxy. | Localhost demo. |
| S2 | Secrets are kept in `.env` (git-ignored). Production would use a secrets manager (Vault or a cloud equivalent). | Localhost demo. |
| S3 | Rate limits (requests per minute per user and per role, daily token budget) are set in config and changed by Admins; values are tuned for a demo, not production traffic. | The assignment asks for configurable limits. |
| S4 | If Redis is unavailable, the API falls back to an in-process token bucket with stricter limits, and logs a warning. | Keeps the service available while still bounding usage. |
| S5 | `make search` takes the role as an option, for local testing. | Anyone who can run it already holds the Pinecone key and could read the index directly. In the app the role always comes from the login token. |
| S6 | Search logs never contain the question text, only its length. | Questions can contain personal data. The question reaches LangSmith only after PII masking (`feature/guardrails`). |

## Observability and operations

| # | Assumption | Reason |
|---|---|---|
| O1 | Alerting is out of scope for a localhost demo. Alerts would use: 5xx rate, p95 latency per endpoint, LLM fallback count, circuit breaker open, guardrail blocks per minute, 429 rate. | The metrics exist; only the alert rules and routing are missing. |
| O2 | LangSmith tracing is on only when `LANGSMITH_TRACING=true` and a key is set. User IDs in traces are pseudonymised (`feature/auth-rbac`) and PII is masked before sending (`feature/guardrails`). Until then traces hold the question as typed, so tracing is used with the fictional sample data only. | The app must work without LangSmith, and traces must be safe to share. |
| O3 | Public LangSmith trace links in the README use dummy data only and stay shared until the review ends. | Deliverable requirement. |

## Trade-offs

| Trade-off | Chosen | Given up |
|---|---|---|
| Cloud free-tier LLMs vs local models | Better models, no laptop RAM used, real cross-provider failover | Needs internet; provider quotas |
| Plain LangGraph vs prebuilt agents | Full control of routing, role checks and streaming events | More code to write |
| One hybrid index vs two indexes + RRF | True score combination, one query per namespace | `alpha` must be tuned |
| Pinecone Local plus in-memory fakes vs real Pinecone in CI | Free, fast, no account or quota in CI; the emulator checks the real filter syntax, namespaces, the async client and re-running ingestion | The emulator ignores sparse vectors and only speaks the older control-plane API, so the hybrid score and the hosted models (embeddings, re-ranker) are checked only by the live tests |
| Keycloak vs hard-coded users | Real OIDC tokens and expiry | One more container, login step |
| Docker Compose only | Same stack everywhere, one command | Image downloads on first start |
| Small RLM plan language | Safe, testable | Less flexible plans |
| Routing after each agent in code, not by the model | Predictable, testable, three model calls per question | The supervisor cannot improvise a new route; new routes are added in code |
| Validator regenerates once | Bounded latency and cost | Some answers end with a safe fallback message instead of a retry loop |
| SSE vs WebSockets for the chat stream | Plain HTTP, works with `curl` and any proxy; disconnects handled by Starlette | One-way only: approvals and feedback are separate requests ([ADR-0013](adr/0013-streaming-with-server-sent-events.md)) |
| Streaming tokens before the answer is validated | The first words appear in seconds | The preview can change after a rewrite; the client must follow the `response_start` rule |
