# api

The FastAPI service. It checks each request, runs the agents and streams what they do back to the client. The agents themselves live in [agents/](../agents/README.md); this package only deals with HTTP.

| Module | What it does |
|---|---|
| [main.py](main.py) | Builds the app: middleware, error handlers, routes, one metrics registry, and the agents opened once on startup |
| [middleware.py](middleware.py) | A request ID on every request, response and log line; one log line per request. Plain ASGI, so it never buffers a stream |
| [errors.py](errors.py) | Turns errors into the same safe JSON shape: `{"error": {"code", "message", "request_id"}}` |
| [routes/health.py](routes/health.py) | `/healthz` (the process is up) and `/readyz` (the services it needs are up) |
| [routes/chat.py](routes/chat.py) | `POST /chat`: checks the request, then streams the run as server-sent events |
| [routes/me.py](routes/me.py) | `GET /me`: the signed-in user, their role and what they may use |
| [routes/tools.py](routes/tools.py) | `POST /tools/python`: the Python analysis tool, run in the sandbox container |
| [routes/admin.py](routes/admin.py) | Admin-only routes. For now `GET /admin/access-matrix` |
| [sse.py](sse.py) | Runs one agent stream in its own task, with a time limit, keep-alives and cancellation; formats each event |
| [identity.py](identity.py) | Who is asking: checks the Keycloak token and returns a `Caller`; `requires(Action.X)` for routes that need a permission |
| [threads.py](threads.py) | Who owns each conversation, and one question at a time per conversation |
| [telemetry.py](telemetry.py) | Prometheus metrics for the chat stream |
| [readiness.py](readiness.py) | The checks behind `/readyz` |

## Endpoints

| Method and path | What it does | Built in |
|---|---|---|
| `GET /healthz` | The process is running | `feature/project-scaffold` |
| `GET /readyz` | Postgres, Redis and the other required services answer | `feature/docker-infra` |
| `GET /metrics` | Prometheus metrics: requests, agents, models, search and streams | `feature/project-scaffold`, extended since |
| `POST /chat` | Ask a question; the answer and the agents' activity stream back | `feature/streaming-api` |
| `GET /me` | The signed-in user's name, role, permissions and document access levels | `feature/auth-rbac` |
| `GET /admin/access-matrix` | The whole role matrix (admin only) | `feature/auth-rbac` |
| `POST /tools/python` | Run Python on data in the sandbox (analysts and admins) | `feature/python-sandbox` |
| `GET /admin/rate-limits` | The rate limits in force, and whether Redis holds the counts (admin only) | `feature/rate-limiting` |
| `PUT /admin/rate-limits/{role}`, `DELETE /admin/rate-limits/{role}` | Change a role's questions per minute and daily token budget, or go back to the settings (admin only) | `feature/rate-limiting` |
| `GET /threads`, `GET /threads/{id}`, `DELETE /threads/{id}` | The caller's saved conversations: list, read, delete | `feature/memory` |
| `GET /me/memory`, `DELETE /me/memory` | What the assistant remembers about the caller, and forgetting it | `feature/memory` |
| `POST /admin/memory/cleanup` | Delete threads and memory older than the retention period (admin only) | `feature/memory` |
| `POST /approvals/{thread_id}` | Approve or reject a run that stopped to ask (`approval_required`); streams the rest of the run | `feature/hitl-feedback` |
| `POST /feedback` | Thumbs up or down (and a comment) on one answer; also sent to LangSmith | `feature/hitl-feedback` |
| `GET /admin/feedback` | Every user's feedback, newest first (admin only) | `feature/hitl-feedback` |
| `GET /admin/audit-log?day=YYYY-MM-DD` | One record per question that day (admin only) | `feature/observability-evals` |

The interactive API docs are at http://localhost:8000/docs outside production.

Who may call what:

| Route | No token | Viewer | Analyst | Admin |
|---|---|---|---|---|
| `/healthz`, `/readyz`, `/metrics` | Yes | Yes | Yes | Yes |
| `GET /me` | No (401) | Yes | Yes | Yes |
| `POST /chat` | No (401) | Yes | Yes | Yes |
| `POST /tools/python` | No (401) | No (403) | Yes | Yes |
| `/threads`, `/me/memory` | No (401) | Yes, own only | Yes, own only | Yes, own only |
| `POST /admin/memory/cleanup`, `GET /admin/feedback`, `GET /admin/audit-log` | No (401) | No (403) | No (403) | Yes |
| `POST /approvals/{id}`, `POST /feedback` | No (401) | Yes, own threads | Yes, own threads | Yes, own threads |
| `GET /admin/access-matrix`, `/admin/rate-limits` (all methods) | No (401) | No (403) | No (403) | Yes |

`tests/security/test_rbac_matrix.py` checks this table, and fails if a route is added without a line in it.

## `POST /chat`

```bash
TOKEN=$(make -s token ROLE=analyst)    # signs in the demo analyst with Keycloak
curl -N -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" \
  -d '{"message": "The PayHub database connection pool is exhausted. What should I do?"}'
```

or simply `make stream Q="..." ROLE=analyst` with the stack running. Tokens last 5 minutes. The chat UI ([ui/README.md](../ui/README.md)) is the main client of this endpoint.

**Request body**

| Field | Rules |
|---|---|
| `message` | 1 to 2,000 characters, not only spaces |
| `thread_id` | Optional. Leave it out to start a conversation; send back the `X-Thread-ID` header of an earlier answer to ask a follow-up. 8 to 64 letters, digits, `-` or `_` |

Any other field is refused with 422. There is no `role` field on purpose: the role only ever comes from the caller's login.

**Before the stream starts**, problems get a normal JSON error:

| Status | When |
|---|---|
| 401 | No token, or a token that is not valid (see below) |
| 403 | The user has none of the app's roles |
| 422 | The body breaks the rules above |
| 404 | The `thread_id` belongs to someone else |
| 409 | That conversation is still answering the previous question |
| 429 | Too many questions this minute, or today's token budget is used. `Retry-After` gives the seconds to wait, `details.limit` is `requests` or `daily_tokens`, and the message says it in words |
| 503 | No language model is configured, so the agents are not running; or Keycloak's keys could not be fetched (`auth_unavailable`) |

**The stream** (status 200, `text/event-stream`) carries the events described in [docs/architecture/09-streaming-events.md](../docs/architecture/09-streaming-events.md). It always ends with `response_end` (the final, checked answer) or `error`. The response headers include `X-Thread-ID` (for follow-ups), `X-Run-ID` (the LangSmith trace ID) and `X-Request-ID` (for the logs).

## How a stream is run

```python
events = agents.stream(
    message, role=caller.role, user=caller.pseudonym, thread_id=..., request_id=..., run_id=...
)
return StreamingResponse(event_stream(events, ...), media_type="text/event-stream")
```

[sse.py](sse.py) starts the agent run in its own asyncio task, which puts events on a queue. The response reads from the queue and:

- sends each event with the next sequence number;
- sends a `: keep-alive` comment when nothing has come for `CHAT_KEEPALIVE_SECONDS` (default 15), so proxies keep a quiet connection open;
- stops the run with an `error` event (`timeout`) after `CHAT_TIMEOUT_SECONDS` (default 120);
- turns any unexpected failure into an `error` event with a fixed message, and logs the details with the request ID;
- **cancels the run task when the client disconnects.** The cancellation reaches the model or search call in progress, and no backup model is tried, so the free quota is not spent on an answer nobody reads.

## Who is asking

Every route except the health checks and `/metrics` needs a Keycloak access token in the `Authorization: Bearer <token>` header. [identity.py](identity.py) hands it to [security/auth.py](../security/auth.py), which checks:

- the algorithm is RS256 (so `alg=none` and HS256 tricks are refused before anything else);
- the key ID is one Keycloak publishes, and the signature is valid for it;
- the issuer is our realm (`AUTH_ISSUER`), the audience is this API (`AUTH_AUDIENCE`), and the token has not expired (30 seconds of clock skew allowed);
- it is an access token, not an ID or refresh token;
- it carries one of our realm roles: `viewer`, `analyst` or `admin`. The highest one wins.

Any failure, including no token at all, is a 401 with the same message, "You need to sign in again.", whatever the reason. The reason goes to the `security` log channel as a `token_rejected` line (`missing`, `no_bearer_token`, `expired`, `wrong_audience` ...), never the token. A valid user with none of our roles gets 403.

Keycloak's public keys are fetched from `KEYCLOAK_URL` on the first request that needs them, kept for 5 minutes, and fetched again early when a token names a key the API has not seen (at most every 30 seconds). If Keycloak is briefly down, the keys already in memory keep working.

The `Caller` a route gets has the user ID, username, display name, role and a pseudonym. The pseudonym (`u_` + 16 hex characters) is added to every log line of the request and to the agents' LangSmith metadata, so a user's requests can be followed without writing who they are.

A route that needs a permission depends on `requires(Action.X)`. The permissions themselves live only in [security/policies.py](../security/policies.py):

```python
@router.get("/admin/audit-log")
async def audit_log(caller: Annotated[Caller, Depends(requires(Action.VIEW_AUDIT_LOG))]): ...
```

Settings (all in `.env.example`): `KEYCLOAK_URL`, `KEYCLOAK_REALM`, `AUTH_ISSUER`, `AUTH_AUDIENCE`, `AUTH_JWKS_CACHE_SECONDS`, `AUTH_JWKS_MIN_REFRESH_SECONDS`, `AUTH_CLOCK_SKEW_SECONDS`, `AUTH_TIMEOUT_SECONDS`. Inside Docker the API fetches keys from `http://keycloak:8080`, while tokens still name `http://localhost:8080` as their issuer, which is why the two are set separately.

## Rate limits

Every question is checked by [security/rate_limit.py](../security/rate_limit.py) after the login and before anything else, so probing other people's threads uses up the caller's questions too:

- **Questions per minute**: a token bucket per user, sized by role, and one shared by every user of the role. Up to `RATE_LIMIT_BURST` questions back to back, then one each time the bucket refills.
- **Daily token budget**: each question reserves `RATE_LIMIT_TOKENS_PER_QUESTION` tokens; when `response_end` arrives, the reservation becomes the real number of model tokens. A question that never ran (404, 409) gives it back; one the user left keeps it.

Counts live in Redis, so every worker shares them. Without `REDIS_URL` (the API on its own) they are kept in memory; if Redis goes down, each worker counts on its own with halved limits. `GET /me` includes today's usage, which the UI shows. Admins change limits with `PUT /admin/rate-limits/{role}`:

```bash
curl -X PUT http://localhost:8000/admin/rate-limits/viewer \
  -H "Authorization: Bearer $(make -s token ROLE=admin)" -H "Content-Type: application/json" \
  -d '{"per_minute": 5, "daily_tokens": 50000}'
```

Metrics: `rate_limit_rejections_total{limit, role}`, `rate_limit_tokens_charged_total{role}` and `rate_limit_degraded`.

## Conversations

Conversations are kept by the agents' checkpointer, in Postgres in Docker Compose (in memory when the API runs without `POSTGRES_URL`), so they survive restarts. [threads.py](threads.py) adds two rules: a conversation belongs to the user who started it, by the user ID in their token (anyone else, even with the same role, gets 404), and it answers one question at a time (409). Owners and titles are saved next to the conversations, so they survive restarts too, and `GET /threads` lists them ([docs/architecture/08-memory.md](../docs/architecture/08-memory.md)).

## Startup

On startup the app opens the agents once: the model gateway, the Pinecone connection and the graph. They are shared by every request and closed on shutdown, which also sends any LangSmith traces still waiting.

- No model key: the API still starts (health checks and metrics work), logs `agents_not_configured` with the reason, and `POST /chat` answers 503.
- No Pinecone key or index: the agents run and answer that document search is unavailable.

## Metrics

Everything is on one `/metrics` endpoint, from one registry per app: the request metrics, the agent and model metrics ([agents/telemetry.py](../agents/telemetry.py)), the search metrics ([rag/telemetry.py](../rag/telemetry.py)) and the stream metrics:

| Metric | What it shows |
|---|---|
| `chat_streams_total{outcome}` | `completed`, `cancelled` (client left), `timeout`, `error` |
| `chat_streams_active` | Streams open right now |
| `chat_stream_events_total{type}` | Events sent, by type |
| `chat_first_token_seconds` | Time from the question to the first word of the answer |

## Tests

- `tests/integration/test_chat_api.py`: the stream format and order, headers, follow-ups, request checks, 409 and 503, metrics, startup.
- `tests/resilience/test_chat_stream.py`: client disconnects, time limit, keep-alives, unexpected failures.
- `tests/security/test_chat_security.py`: no role or access level from the body, the old dev header gives nothing, other people's threads, no chunk text in the stream.
- `tests/security/test_tokens.py`: 16 kinds of bad token (expired, forged, `alg=none`, HS256 with the public key, wrong issuer or audience, ID token, edited payload ...), each refused on its own and through the API; logs carry the pseudonym only.
- `tests/security/test_rbac_matrix.py`: role × route, role × tool and role × data, and two users with the same role never sharing a conversation.
- `tests/unit/test_jwks_client.py`: the key cache, key rotation, and Keycloak being down.
- `tests/e2e/test_keycloak_login.py` (`make test-keycloak`): the demo users' real tokens from the local Keycloak.
- `tests/security/test_rate_limit_api.py`, `tests/integration/test_rate_limit_redis.py`, `tests/unit/test_rate_limit.py`: 429s and `Retry-After`, the budget, admin changes, the Lua script under concurrency, and Redis failing.
- `tests/graph/test_streaming.py`: the events of full graph runs, including rewrites and a model failing mid-answer.
- [tests/helpers/sse.py](../tests/helpers/sse.py) calls the app the way a browser does, so a test can disconnect half-way. httpx's test client waits for the whole response, so it cannot test that. It signs in as the test viewer unless told otherwise.
- [tests/helpers/tokens.py](../tests/helpers/tokens.py) signs test tokens with a key pair made for the test run; the test apps trust its public key instead of Keycloak's.
