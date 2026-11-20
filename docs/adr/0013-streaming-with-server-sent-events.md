# ADR-0013: Streaming answers and agent activity with server-sent events

- **Status:** Accepted
- **Date:** 2026-11-21

## Context

The assignment asks for a chat window that streams the answer and an activity panel that shows, live, what the agents are doing: the active node, the agent state, retrieval status, validation results and the final response. A question takes 10 to 40 seconds on the free model tiers, so a user who sees nothing until the end will think the app has hung.

The stream must:

- carry typed events, not only text, so the panel can show each step;
- work through the Streamlit server and ordinary HTTP proxies;
- stop the run when the user leaves, so free-tier quota is not spent on answers nobody reads;
- report failures without breaking the client or leaking internals.

## Decision

- `POST /chat` answers with **server-sent events** (`text/event-stream`). Each event has a type (`event:` line), a sequence number (`id:` line) and one line of JSON (`data:`). The event types and the rules for clients are in [09-streaming-events](../architecture/09-streaming-events.md).
- Events are **built from LangGraph's own stream modes** (`tasks`, `messages`, `values`), not from callbacks inside the nodes. The nodes stay unaware of streaming, and the CLI (`agents.ask`) runs exactly the same code: `ask()` is built on `stream()`.
- **Tokens are a preview; `response_end` is the answer.** The validator can reject a draft and a model can fail half-way, so the client clears the text on each `response_start` and shows the final answer from `response_end`.
- The run works in **its own task**, with a time limit (`CHAT_TIMEOUT_SECONDS`), keep-alive comments when quiet (`CHAT_KEEPALIVE_SECONDS`), and **cancellation when the client disconnects**.
- Everything wrong with the request is answered **before** the stream starts, with a normal status code (422, 404, 409, 503). After that, problems arrive as an `error` event.
- One `run_id`, chosen by the API, is the LangGraph run ID, the LangSmith trace ID, the `X-Run-ID` header and a field of the first event.

## Consequences

- **Positive:** plain HTTP. It works with `curl` (`make stream`), `httpx` in the UI, and any proxy that does not buffer (the `X-Accel-Buffering: no` header asks nginx not to).
- **Positive:** the event types are Pydantic models, so the panel, the tests and the docs share one definition.
- **Positive:** a user who closes the tab stops the run within milliseconds. This was checked against the real models, and the cancelled run shows in LangSmith.
- **Negative:** SSE is one-way. Anything the client sends during a run (for example, approving a tool call in `feature/hitl-feedback`) is a separate request. That suits this app: approval resumes the graph from its checkpoint anyway.
- **Negative:** a browser's built-in `EventSource` only supports GET and cannot send an `Authorization` header. The Streamlit UI reads the stream with `httpx` on the server side, so this does not apply; a future browser client would use `fetch` with a stream reader.
- **Negative:** the token preview can differ from the final answer (after a rewrite or a model switch). The client rules above make this visible rather than confusing.

## Alternatives considered

| Option | Why not |
|---|---|
| WebSockets | Two-way, but needs its own auth handshake, reconnection logic and proxy settings. Nothing in this app needs the client to talk during a run |
| Polling a run-status endpoint | Slow to show progress, and many extra requests per question |
| LangGraph's `astream_events` (v2) | Sends every internal callback; filtering it down is more code than reading the `tasks` and `messages` modes, and its format is LangChain's, not ours |
| `sse-starlette` library | Adds a dependency for about 20 lines of formatting; Starlette's `StreamingResponse` already handles disconnects |
| Streaming only the final text | Leaves the activity panel empty, which the assignment asks for |

## Related

[09-streaming-events](../architecture/09-streaming-events.md) · [api/README.md](../../api/README.md) · [ADR-0006](0006-langgraph-agents-and-deep-agent-patterns.md) · streaming tests in [TESTING.md](../TESTING.md)
