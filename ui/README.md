# ui

The Streamlit chat app. On the left is the conversation; on the right is the **agent activity panel**, which shows live what the agents are doing to answer: the supervisor's plan, each LangGraph node as it starts and finishes, what the search looked for and found, the citation checks, any fallback to the backup model, and finally which model wrote the answer and how many tokens it used.

The UI is a thin client. It never imports backend code (`api`, `agents`, `rag`, `core`, ...); it only calls the API over HTTP and reads the server-sent events of `POST /chat`. CI checks this with import-linter, and the UI's Docker image does not even contain the backend code or its libraries. Why it is built this way is in [ADR-0014](../docs/adr/0014-streamlit-chat-ui.md).

## Run it

```bash
make run-api      # terminal 1: the API on http://localhost:8000
make run-ui       # terminal 2: the UI on http://localhost:8501
```

or `make up` to run everything in Docker; the UI is then on <http://localhost:8501> as well. The API needs a model key (and Pinecone for document answers); see the main [README](../README.md#getting-started).

## What is on the screen

| Area | What it shows |
|---|---|
| Sidebar | New conversation, the role picker (until sign-in exists), the conversations of this tab, whether the API is reachable, and the current thread ID |
| Chat window | Your questions and the answers. Each answer has its sources (document ID, title and section), any limits (for example a collection that did not answer), and "How I got this answer" |
| Activity panel | For the selected question (the latest by default): the run ID and whether it is traced in LangSmith, the plan, every step with its time, the search details, the checks, warnings, and the model calls with their tokens. "Raw events" shows the events exactly as they arrived |

While an answer streams, the chat box turns into a **Stop** button. Stopping closes the connection; the API sees the client leave and cancels the agents at once, so no quota is spent on an answer nobody reads.

How each assignment item maps to the panel:

| Assignment item | Shown as | From event |
|---|---|---|
| Current agent state | "Agent state": intent, agents and the numbered plan; the status badge | `agent_state` |
| Active LangGraph node | "Steps": a spinner on the running node, then its time and summary | `node_start`, `node_end` |
| Retrieval status | "Retrieval": query, collections, matches, kept after re-ranking, filters, collections that failed | `retrieval` |
| Validation results | "Validation": input check and citation check, with the issues found | `validation` |
| Final response generation | The answer streaming in the chat; "Response": who wrote it, time, tokens per model call | `response_start`, `token`, `response_end` |
| Tool calls, memory updates | Listed under "Other events" until their branches add proper views | `tool_call`, `memory` (later branches) |

## How it works

| Module | What it does |
|---|---|
| [app.py](app.py) | The page: sidebar, chat, panel, and the loop that reads the stream and redraws |
| [client.py](client.py) | `ChatClient.ask()`: sends the question, yields the events, turns every failure into an `ApiError` with a message that is safe to show |
| [sse.py](sse.py) | Reads the server-sent event format line by line |
| [conversation.py](conversation.py) | `Turn` (one question and what happened) and `Conversation`. `turn.apply(event)` holds the client rules from [09-streaming-events](../docs/architecture/09-streaming-events.md#rules-for-the-client) |
| [views.py](views.py) | Draws one answer and the activity panel |
| [format.py](format.py) | The panel's wording: durations, labels, search and token lines |
| [config.py](config.py) | Settings from environment variables |

The flow for one question:

1. `app.py` adds a `Turn` to the conversation and calls `client.ask()` with the conversation's thread ID and role.
2. Each event goes into `turn.apply(event)`. Tokens redraw only the answer; other events redraw the panel too.
3. The stream ends with `response_end` (the final, checked answer replaces the streamed preview) or `error`. A stream that just stops is shown as cut short.
4. The page is drawn again from the saved state.

Streamlit runs `app.py` from top to bottom on every click, so all state lives in `st.session_state`. A Stop click ends the run in the middle of the loop; the next run finds the turn still marked as streaming and marks it as stopped, keeping the text that had arrived.

Event types the UI does not know yet are kept and listed, so a newer API never breaks an older UI.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `API_URL` | `http://localhost:8000` | Where the API is. Docker Compose sets `http://api:8000` |
| `UI_CONNECT_TIMEOUT` | `5` | Seconds to wait for the API to accept a question |
| `UI_READ_TIMEOUT` | `60` | Longest silence accepted while an answer streams. The API sends a keep-alive every 15 seconds, so a longer silence means it is gone |

Streamlit's own settings are in [.streamlit/config.toml](../.streamlit/config.toml): cross-site request forgery protection on, usage statistics off, short error details in the browser, and no lookup of the machine's public IP on start.

## Not built yet

- **Sign-in.** The role picker sends the `X-Dev-Role` header, which a production API refuses. `feature/auth-rbac` replaces it with a Keycloak login and shows the user's name and role.
- **Past conversations across sessions.** The list holds the conversations of this browser tab. `feature/memory` stores them in Postgres and adds `GET /threads`, and the sidebar will read from there.
- **Thumbs up/down and the approval step.** They come with `feature/hitl-feedback`, linked to the run ID the panel already shows.
- **Links to the source documents.** Sources show the document ID, title and section. A link needs a document endpoint that checks the reader's role.

## Tests

- `tests/unit/test_ui_sse.py`, `test_ui_conversation.py`, `test_ui_format.py`: the event reader, the client rules and the panel's wording.
- `tests/integration/test_ui_client.py`: the client against replies recorded from the real API (scripted agents), including rewrites, refusals (404, 409, 422, 503), lost connections, timeouts and Stop.
- `tests/integration/test_ui_app.py`: the whole page, run headless with Streamlit's `AppTest`: asking, following up, example questions, errors, switching role and conversation, Stop.
- `tests/security/test_ui_security.py`: HTML in an answer is never run, no role in the request body, Streamlit's protections, and an image without backend code.

The replies the UI is tested with are recorded from the real API ([tests/helpers/ui.py](../tests/helpers/ui.py)), so a change to the stream that the UI does not handle fails these tests.
