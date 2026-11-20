# api

The FastAPI service. It checks each request, runs the agents and streams what they do back to the client. The agents themselves live in [agents/](../agents/README.md); this package only deals with HTTP.

| Module | What it does |
|---|---|
| [main.py](main.py) | Builds the app: middleware, error handlers, routes, one metrics registry, and the agents opened once on startup |
| [middleware.py](middleware.py) | A request ID on every request, response and log line; one log line per request. Plain ASGI, so it never buffers a stream |
| [errors.py](errors.py) | Turns errors into the same safe JSON shape: `{"error": {"code", "message", "request_id"}}` |
| [routes/health.py](routes/health.py) | `/healthz` (the process is up) and `/readyz` (the services it needs are up) |
| [routes/chat.py](routes/chat.py) | `POST /chat`: checks the request, then streams the run as server-sent events |
| [sse.py](sse.py) | Runs one agent stream in its own task, with a time limit, keep-alives and cancellation; formats each event |
| [identity.py](identity.py) | The caller's role. A development stand-in until login is added (see below) |
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
| `GET /threads`, `GET /threads/{id}` | A user's past conversations | planned, `feature/memory` (needs the user ID from login) |
| `POST /feedback`, `POST /approvals/{id}` | Thumbs up/down; human approval of a tool call | planned, `feature/hitl-feedback` |

The interactive API docs are at http://localhost:8000/docs outside production.

## `POST /chat`

```bash
curl -N -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" -H "X-Dev-Role: analyst" \
  -d '{"message": "The PayHub database connection pool is exhausted. What should I do?"}'
```

or simply `make stream Q="..." ROLE=analyst` with the API running.

**Request body**

| Field | Rules |
|---|---|
| `message` | 1 to 2,000 characters, not only spaces |
| `thread_id` | Optional. Leave it out to start a conversation; send back the `X-Thread-ID` header of an earlier answer to ask a follow-up. 8 to 64 letters, digits, `-` or `_` |

Any other field is refused with 422. There is no `role` field on purpose: the role only ever comes from the caller's login.

**Before the stream starts**, problems get a normal JSON error:

| Status | When |
|---|---|
| 422 | The body breaks the rules above, or `X-Dev-Role` is not a known role |
| 401 | `X-Dev-Role` was sent to a production API (see below) |
| 404 | The `thread_id` belongs to someone else |
| 409 | That conversation is still answering the previous question |
| 503 | No language model is configured, so the agents are not running |

**The stream** (status 200, `text/event-stream`) carries the events described in [docs/architecture/09-streaming-events.md](../docs/architecture/09-streaming-events.md). It always ends with `response_end` (the final, checked answer) or `error`. The response headers include `X-Thread-ID` (for follow-ups), `X-Run-ID` (the LangSmith trace ID) and `X-Request-ID` (for the logs).

## How a stream is run

```python
events = agents.stream(message, role=caller.role, thread_id=..., request_id=..., run_id=...)
return StreamingResponse(event_stream(events, ...), media_type="text/event-stream")
```

[sse.py](sse.py) starts the agent run in its own asyncio task, which puts events on a queue. The response reads from the queue and:

- sends each event with the next sequence number;
- sends a `: keep-alive` comment when nothing has come for `CHAT_KEEPALIVE_SECONDS` (default 15), so proxies keep a quiet connection open;
- stops the run with an `error` event (`timeout`) after `CHAT_TIMEOUT_SECONDS` (default 120);
- turns any unexpected failure into an `error` event with a fixed message, and logs the details with the request ID;
- **cancels the run task when the client disconnects.** The cancellation reaches the model or search call in progress, and no backup model is tried, so the free quota is not spent on an answer nobody reads.

## Who is asking (until login is added)

There is no login yet; Keycloak arrives with `feature/auth-rbac`. Until then [identity.py](identity.py) reads the role from the `X-Dev-Role` header:

- missing means `viewer`, the lowest role;
- it is **refused with 401 when `APP_ENV=production`**, so a deployed API is never open without login;
- the owner of a conversation is the role for now, and becomes the user ID from the token.

`feature/auth-rbac` replaces `current_caller` with a version that validates the Keycloak token. The routes use the `Caller` it returns and do not change.

## Conversations

Conversations are kept in memory by the agents' checkpointer, so they last until the API restarts. [threads.py](threads.py) adds two rules: a conversation belongs to whoever started it (anyone else gets 404), and it answers one question at a time (409). `feature/memory` moves conversations to Postgres, along with their owners.

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
- `tests/security/test_chat_security.py`: no role or access level from the body, no dev header in production, other people's threads, no chunk text in the stream.
- `tests/graph/test_streaming.py`: the events of full graph runs, including rewrites and a model failing mid-answer.
- [tests/helpers/sse.py](../tests/helpers/sse.py) calls the app the way a browser does, so a test can disconnect half-way. httpx's test client waits for the whole response, so it cannot test that.
