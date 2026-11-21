# ADR-0014: Streamlit chat UI as a thin client of the streaming API

- **Status:** Accepted
- **Date:** 2026-11-21

## Context

The assignment asks for a Streamlit frontend with multi-turn chat, streamed answers and an activity panel that shows the agent state, the active node, tool calls, retrieval status, memory updates, validation results and the final response. The API already streams all of this as typed server-sent events ([ADR-0013](0013-streaming-with-server-sent-events.md)).

Streamlit is unusual: the server runs the whole script again on every click, and a long-running loop in the script blocks that session until it ends. The UI must still:

- show tokens and panel updates as they arrive;
- let the user stop an answer, and have that stop the agents;
- keep conversations between reruns;
- stay separate from the backend, so the UI can never call the agents or read the vector store directly.

## Decision

- **The UI only talks to the API.** It sends `POST /chat` with `httpx` from the Streamlit server and reads the event stream line by line. It never imports backend packages (import-linter in CI), and its Docker image is built from the `ui` dependency group alone (Streamlit, httpx) plus the `ui/` folder: no backend code or libraries are inside it.
- **One loop per question, inside the script run.** The loop reads events and redraws two placeholders: the answer (on each token) and the panel (on every other event). Rendering is kept out of the state: `Turn.apply(event)` holds the client rules (tokens are a preview, `response_start` clears it, `response_end` wins), and plain functions draw a `Turn`.
- **Stop uses Streamlit's own mechanism.** The chat box uses `submit_mode="stop"`: while the script runs it becomes a Stop button. Stopping ends the script run, which closes the HTTP stream, which makes the API cancel the agents. The next run marks the half-finished turn as stopped and keeps the text that arrived.
- **State lives in `st.session_state`.** Each browser tab has its own list of conversations; each conversation keeps its thread ID (from the `X-Thread-ID` header). Signing out clears them, because the API ties a thread to the user who started it.
- **The role picker was a development stand-in.** It sent `X-Dev-Role`, which a production API refused. `feature/auth-rbac` replaced it with a Keycloak sign-in form; the sidebar now shows the signed-in user's name, role and allowed tools (see [ADR-0007](0007-keycloak-rbac-and-data-access.md) and [ui/README.md](../../ui/README.md#sign-in)).
- **Tested against the real API's output.** UI tests replay replies recorded from the real FastAPI app with scripted agents, and the page itself is run headless with Streamlit's `AppTest`.

## Consequences

- **Positive:** the browser never talks to the API; there is no CORS to configure and no token in browser JavaScript. Only the Streamlit server holds the connection.
- **Positive:** Stop, closing the tab and clicking elsewhere all end the run in the same way, and the API's cancellation (already tested) does the rest.
- **Positive:** the UI image cannot import backend code even by mistake, and a UI dependency update cannot change the API image.
- **Negative:** while an answer streams, the session is busy: other clicks stop the answer first. For a chat this is the expected behaviour; the chat box shows the Stop button to make it clear.
- **Negative:** the activity panel of an answer is kept per browser tab only; earlier conversations come back from `GET /threads` (added with `feature/memory`) as questions and answers, without their panels.
- **Negative:** the UI image is large (about 800 MB) because Streamlit brings pandas and pyarrow. Acceptable for a local demo; a slimmer frontend would need a different framework, which the assignment does not ask for.

## Alternatives considered

| Option | Why not |
|---|---|
| Import `AgentService` into Streamlit and call it directly | Simplest, but the UI would hold the model and Pinecone keys and skip the API's checks (roles, thread owners, time limits, metrics). It also breaks the boundary the assignment's architecture needs |
| `st.write_stream` for the answer | Handles text only; the panel needs to update from the same stream, so the loop draws both itself |
| A background thread per question, with `st.fragment(run_every=...)` polling its state | Lets the page stay clickable while streaming, but adds threads, locks and polling delay to a demo UI. Stop would also need its own plumbing |
| Browser `EventSource` through a custom component | `EventSource` only does GET without an `Authorization` header, and needs CORS on the API |
| Keep conversations in the API now | Needs the user ID from login and a store; that is `feature/memory`'s job |

## Related

[ui/README.md](../../ui/README.md) · [09-streaming-events](../architecture/09-streaming-events.md) · [ADR-0013](0013-streaming-with-server-sent-events.md) · [ADR-0009](0009-docker-compose-runtime.md) · UI tests in [TESTING.md](../TESTING.md)
