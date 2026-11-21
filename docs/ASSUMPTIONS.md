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
| I5 | A user with more than one of our realm roles gets the highest one. A user with none of them can sign in to Keycloak but gets 403 from the API. | Roles are nested (each one can do everything the one below can), so the highest role is the user's real access. A Keycloak account alone should not open the app. |
| I6 | Conversations belong to the user ID (`sub`) in the token, not to the role. Two analysts never share a conversation. | `sub` is stable and never reused in Keycloak, unlike usernames, which can be renamed. |
| I7 | The API checks tokens itself with Keycloak's public keys and does not ask Keycloak about each request. A token stays usable until it expires (at most 5 minutes) even after sign-out. | No call to Keycloak per request, and the API keeps working through a short Keycloak restart. Short tokens keep the window small. |
| I8 | Logs and traces name the user by a pseudonym: `u_` plus 16 hex characters of a SHA-256 of the user ID. | Requests from one user can be followed without writing who they are. A real deployment would use a keyed hash (HMAC) with a secret, so the pseudonym cannot be checked against a list of known user IDs. |
| I9 | Admin actions in the matrix (audit log, rate limits, all feedback, re-index) are refused for everyone but admins from day one, whether or not they have an endpoint. Built: the access matrix, rate limits, all feedback, the audit log and the memory clean-up; re-indexing stays `make ingest`, run by a person with the Pinecone key. | Deny by default: each later branch adds its route with `requires(Action.X)` and does not touch the matrix. |

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
| AG4 | The agents see the last 6 messages of the conversation (`AGENT_HISTORY_MESSAGES`). Longer threads are summarised by the memory writer (M1). | Enough for follow-up questions, small enough for free-tier limits. |
| AG5 | Conversations are kept by LangGraph's Postgres checkpointer in Docker Compose, and in memory when the API runs without `POSTGRES_URL`. | The same graph code works in both; tests use the in-memory one. |
| AG6 | "The last year" and similar phrases are resolved against the server's date, which is given to the model in the prompt. | Date filters need real dates; the model has no clock. |

## Chat stream

| # | Assumption | Reason |
|---|---|---|
| ST1 | Streamed tokens are a preview. The final answer is the `response_end` event, and a `response_start` tells the client to clear the text (a rewrite, or a model failing half-way). | The validator checks the whole answer, so it can only approve it at the end. Holding every token until then would make the user wait 10 to 40 seconds for the first word ([ADR-0013](adr/0013-streaming-with-server-sent-events.md)). |
| ST2 | The role and the conversation owner come from the Keycloak access token in the `Authorization` header. (Before `feature/auth-rbac`, an `X-Dev-Role` header stood in for login; it has been removed.) | The role is never accepted from the request body, a header the client chooses, or the conversation. |
| ST3 | One whole answer may take at most 120 seconds (`CHAT_TIMEOUT_SECONDS`); a keep-alive line is sent after 15 quiet seconds. | A normal question takes 10 to 40 seconds on the free tiers, with a 30-second limit per model call. Anything slower is stuck. |
| ST4 | A conversation answers one question at a time; a second one gets 409. | Two runs writing to one conversation would mix up its history. The chat UI turns its input into a Stop button while an answer streams, so it never sends a second question. |
| ST5 | When the client disconnects, the run is cancelled at once, even mid-answer. Nothing is saved for the half-finished question. | No quota is spent on answers nobody reads. Resuming abandoned answers is not in scope. |

## Chat UI

| # | Assumption | Reason |
|---|---|---|
| U1 | The sidebar lists the conversations open in this tab, and below them the user's earlier conversations saved on the server (`GET /threads`). Opening an earlier one loads its questions and answers; the activity panel of an earlier answer is not kept. | The checkpointer keeps the conversation, not every streamed event; storing all events would double what is saved per question. |
| U2 | The UI signs users in with a username and password form, and sends them to Keycloak with the OAuth password grant from the UI server. Tokens are kept in the Streamlit session on the server, refreshed 30 seconds before they expire, and forgotten on sign-out together with the tab's conversations. | One small page, no callback route, and it works the same in Docker and with `make run-ui`. The trade-off is that the password passes through the UI server; see S7. |
| U7 | The sidebar lists only the tools the user's role may use (from `GET /me`); the others are not shown at all. | Showing a tool and then refusing it is confusing. The API enforces the role either way (U5). |
| U3 | Sources show the document ID, title and section, without a link to the document. | There is no document endpoint yet; one would need its own role check. |
| U4 | While an answer streams, any click (Stop, another conversation, an example question) stops it first. | That is how Streamlit runs a page; stopping also cancels the agents, so no quota is wasted. |
| U5 | The UI shows what the API sends and does not re-check access. Every rule (roles, thread owners, limits) is enforced by the API. | The UI is a thin client; the browser and the UI server are not trusted to enforce anything. |
| U6 | Each finished answer has thumbs up and down buttons; a run waiting for approval shows Approve and Reject buttons. The rest of an approved run is shown when it has finished, with a spinner while it works. | Keeps the approval flow to one request and one redraw. |

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
| S3 | Rate limits are set in `.env` and changed by admins while the API runs ([ADR-0015](adr/0015-rate-limiting.md)): questions per minute per user (viewer 10, analyst 20, admin 40, with a burst of 5), 120 per minute for all users of a role together, and a daily budget of model tokens per user (100,000 / 250,000 / 500,000, reset at 00:00 UTC). Only `POST /chat` is limited. The values suit a demo on free model tiers, not production traffic. | The assignment asks for configurable, per-user limits. The model quota is the scarce resource, so the budget counts model tokens. |
| S4 | If Redis is unavailable, each API worker falls back to its own counts in memory with every limit halved, logs one warning a minute and sets `rate_limit_degraded`. Admin changes to the limits are refused (503) until Redis is back. Limit changes are kept in Redis without persistence, so a Redis restart goes back to the `.env` values. | Keeps the service available while still bounding usage. |
| S10 | Prompt-injection detection is a set of rules, and personal data masking uses patterns with checksums (e-mail, card, IBAN, UK sort code and account, National Insurance number, phone, IP address, API keys), applied to answers, the streamed preview, logs and traces ([ADR-0016](adr/0016-guardrails-rules-and-pattern-masking.md)). | LLM Guard and Presidio need several GB of models; the code-level role checks are what keep data safe either way. |
| S11 | The last 48 characters of the streamed answer are held back until the response agent has finished the draft, so a value at the very end (a phone number still being written) is masked before it is shown. | The user sees the answer a moment later at the end; nothing personal ever reaches the browser unmasked. |
| S12 | The Python analysis tool is an API endpoint (`POST /tools/python`) for analysts and admins, with standard-library modules only (math, statistics, collections, datetime, json, re ...). No pandas or numpy. | Every library in the sandbox is reachable from code users and models write; the standard library covers the analysis the sample data needs. |
| R5 | Research runs at most 5 batches (fan-out), splits a batch at most down to depth 3, and stops splitting and extracting once it has used 40,000 model tokens. A batch is split only when its search comes back full and covers more than 45 days. Up to 20 sources reach the answer. Sub-agents make at most 2 model calls at a time, and at most 4 batches get model-written findings; the others still give their sources to the answer. | Fits the free model tiers, which allow only a few requests a minute: a research question costs about 4 to 7 model calls. A first live run without these two limits fired seven calls at once and hit Gemini's per-minute limit. |
| R6 | Without a sandbox, or when the model's plan is refused twice, research uses a fixed plan: the question over the last twelve months, by quarter. | The assignment's example ("over the last year") is the common case; research still works when the sandbox is down. |
| M1 | The memory writer saves in code, not with a model: one masked line per answered question, with the documents it cited, and a count of the departments searched. Refusals, small talk and failed answers are not saved. | No model quota spent on memory; the rule is easy to test and explain. |
| M2 | Recall picks past questions sharing at least two words with the new one (up to three), newest first on ties, plus the user's top departments. No embedding search. | Good enough for one user's few hundred items; no extra model or index. |
| M3 | Thread owners live in the same store as memory. Which thread is answering right now is kept per API process. | Owners must survive a restart; "busy" only matters while a stream is open. With several API workers, two could each accept a question for one thread; the checkpointer keeps the saved state consistent. |
| F1 | A model provider's circuit breaker opens after 5 failures in 60 seconds, or at once on a rate-limit reply, and stays open for 30 seconds before one probe call. The state is kept per API process. | Free tiers refuse every call for a while once their limit is hit; waiting for each call to time out would make every question slow. Per-process state is enough for one API container. |
| T1 | The enterprise data (people, services, incident records) is fictional, read-only and served from a JSON file by the MCP server. A real bank would put the server in front of its HR system, CMDB and ticketing tool. | The assignment asks for an MCP server with dummy enterprise data. |
| T2 | Facts from the enterprise tools need no source number in the answer; the answer says they come from the live records. Staff work e-mails at `helix.example` stay readable in answers; every other address is masked, and logs and traces mask all of them. | Tool results are not documents, so there is nothing to cite; the directory is for staff. |
| H1 | The approval step asks before every deep research run (when `APPROVAL_FOR_RESEARCH` is on); the user who asked approves it. Admin tools are separate API routes, already limited to admins, so they do not go through the chat approval. | Research is the costly action the agents can take on their own; a person confirms it first. |
| H2 | Feedback (thumbs up or down, optional comment, masked) is kept in the same store as memory and sent to LangSmith on the answer's trace when tracing is on. Admins see all of it at `GET /admin/feedback`. | The feedback loop needs both: a local record for admins, and the LangSmith run for evaluation. |
| O3 | The audit log keeps the question masked and cut to 200 characters, with who asked (pseudonym and role) and what the system did. It is kept in the store, one namespace per day; the API only adds records. Retention of audit records is left to the bank's records policy and is not cleaned up by the memory clean-up. | A bank must be able to say who asked what and what the assistant did; masking keeps personal data out of it. |
| O4 | Answer quality is measured with simple, cheap measures (right kind of answer, citation recall, key facts, leaks) and an optional model judge for grounding, not Ragas. Scores can be sent to LangSmith as feedback on each question's trace. | Ragas needs many model calls and an embeddings model per run, which does not fit the free tiers. |
| S9 | Each question reserves 4,000 tokens from the budget before it runs, corrected to the real count when the answer ends. A question the user stops keeps its reservation. | Counting only at the end would let a user dodge the budget by leaving early. Counting slightly too much is the safe side. |
| S5 | `make search` takes the role as an option, for local testing. | Anyone who can run it already holds the Pinecone key and could read the index directly. In the app the role always comes from the login token. |
| S6 | Search logs never contain the question text, only its length. | Questions can contain personal data. Anything sent to LangSmith is masked first (S10). |
| S7 | The password grant (U2) is fine for a localhost demo, not for production. A deployment would switch the UI to the authorization code flow with PKCE, which the realm file already enables for the UI's client, so the password is typed into Keycloak's own page only. The API would not change. | The OAuth 2.1 draft drops the password grant. Streamlit has no simple way to handle the redirect and keep the access token for API calls, so the demo uses the simpler flow. |

## Observability and operations

| # | Assumption | Reason |
|---|---|---|
| O1 | Alerting is out of scope for a localhost demo. Alerts would use: 5xx rate, p95 latency per endpoint, LLM fallback count, circuit breaker open, guardrail blocks per minute, 429 rate. | The metrics exist; only the alert rules and routing are missing. |
| O2 | LangSmith tracing is on only when `LANGSMITH_TRACING=true` and a key is set. Users appear in traces only as a pseudonym (I8), and personal data is masked by the LangSmith client before sending (S10). Names are not masked, so tracing is used with the fictional sample data only. | The app must work without LangSmith, and traces must be safe to share. |
| O3 | Public LangSmith trace links in the README use dummy data only and stay shared until the review ends. | Deliverable requirement. |

## Trade-offs

| Trade-off | Chosen | Given up |
|---|---|---|
| Cloud free-tier LLMs vs local models | Better models, no laptop RAM used, real cross-provider failover | Needs internet; provider quotas |
| Plain LangGraph vs prebuilt agents | Full control of routing, role checks and streaming events | More code to write |
| One hybrid index vs two indexes + RRF | True score combination, one query per namespace | `alpha` must be tuned |
| Pinecone Local plus in-memory fakes vs real Pinecone in CI | Free, fast, no account or quota in CI; the emulator checks the real filter syntax, namespaces, the async client and re-running ingestion | The emulator ignores sparse vectors and only speaks the older control-plane API, so the hybrid score and the hosted models (embeddings, re-ranker) are checked only by the live tests |
| Keycloak vs hard-coded users | Real OIDC tokens and expiry | One more container, login step |
| Token bucket in Redis vs fixed window counter | Smooth limits that hold across workers; one atomic Lua script | A small script to maintain |
| Checking tokens locally with cached keys vs asking Keycloak every time (introspection) | No call to Keycloak per request; works through a short Keycloak restart | A signed-out user's access token works until it expires (at most 5 minutes) |
| Password grant in the UI vs browser redirect with PKCE | One simple Streamlit page | The password passes through the UI server; production should use the redirect (S7) |
| Docker Compose only | Same stack everywhere, one command | Image downloads on first start |
| Small RLM plan language | Safe, testable | Less flexible plans |
| Routing after each agent in code, not by the model | Predictable, testable, three model calls per question | The supervisor cannot improvise a new route; new routes are added in code |
| Validator regenerates once | Bounded latency and cost | Some answers end with a safe fallback message instead of a retry loop |
| SSE vs WebSockets for the chat stream | Plain HTTP, works with `curl` and any proxy; disconnects handled by Starlette | One-way only: approvals and feedback are separate requests ([ADR-0013](adr/0013-streaming-with-server-sent-events.md)) |
| Streaming tokens before the answer is validated | The first words appear in seconds | The preview can change after a rewrite; the client must follow the `response_start` rule |
