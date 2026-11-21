# ui

The Streamlit chat app. On the left is the conversation; on the right is the **agent activity panel**, which shows live what the agents are doing to answer: the supervisor's plan, each LangGraph node as it starts and finishes, what the search looked for and found, the citation checks, any fallback to the backup model, and finally which model wrote the answer and how many tokens it used.

The UI is a thin client. It never imports backend code (`api`, `agents`, `rag`, `core`, ...); it only calls the API over HTTP and reads the server-sent events of `POST /chat`. CI checks this with import-linter, and the UI's Docker image does not even contain the backend code or its libraries. Why it is built this way is in [ADR-0014](../docs/adr/0014-streamlit-chat-ui.md).

## Run it

```bash
docker compose up -d keycloak   # sign-in (or `make up` for the whole stack)
make run-api                    # terminal 1: the API on http://localhost:8000
make run-ui                     # terminal 2: the UI on http://localhost:8501
```

or `make up` to run everything in Docker; the UI is then on <http://localhost:8501> as well. The API needs a model key (and Pinecone for document answers); see the main [README](../README.md#getting-started).

Sign in with one of the demo users from the realm file:

| Username | Password | Role |
|---|---|---|
| `vera.viewer` | `viewer-demo-pass` | viewer |
| `adam.analyst` | `analyst-demo-pass` | analyst |
| `ada.admin` | `admin-demo-pass` | admin |

## What is on the screen

| Area | What it shows |
|---|---|
| Sign-in page | Username and password. Shown until you sign in, and again when your session ends |
| Sidebar | Your name, username and role; "What you can use" (only the tools your role may use, and the document levels you can read); today's token usage against your daily budget, updated after each answer; "What I remember" (the questions the assistant remembers, with a button to forget them); Sign out; New conversation; the conversations of this tab, then your earlier conversations saved on the server; whether the API is reachable; the current thread ID |
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
| Tool calls | "Enterprise data tools": each MCP call with its arguments, how many records came back and the time; "Research": the Python plan and every batch | `tool_call`, `research` |
| Memory updates | A "Memory writer" step saying what was remembered or summarised | `memory` |

## How it works

| Module | What it does |
|---|---|
| [app.py](app.py) | The page: sign-in, sidebar, chat, panel, and the loop that reads the stream and redraws |
| [auth.py](auth.py) | `KeycloakLogin`: signs in, refreshes the access token before it runs out, signs out |
| [client.py](client.py) | `ChatClient.ask()`: sends the question with the access token, yields the events, turns every failure into an `ApiError` with a message that is safe to show. `ChatClient.me()` reads the user's profile |
| [sse.py](sse.py) | Reads the server-sent event format line by line |
| [conversation.py](conversation.py) | `Turn` (one question and what happened) and `Conversation`. `turn.apply(event)` holds the client rules from [09-streaming-events](../docs/architecture/09-streaming-events.md#rules-for-the-client) |
| [views.py](views.py) | Draws one answer and the activity panel |
| [format.py](format.py) | The panel's wording: durations, labels, search and token lines |
| [config.py](config.py) | Settings from environment variables |

The flow for one question:

1. `app.py` makes sure the access token has at least 30 seconds left (refreshing it if not), adds a `Turn` to the conversation and calls `client.ask()` with the token and the conversation's thread ID.
2. Each event goes into `turn.apply(event)`. Tokens redraw only the answer; other events redraw the panel too.
3. The stream ends with `response_end` (the final, checked answer replaces the streamed preview) or `error`. A stream that just stops is shown as cut short.
4. The page is drawn again from the saved state.

Streamlit runs `app.py` from top to bottom on every click, so all state lives in `st.session_state`. A Stop click ends the run in the middle of the loop; the next run finds the turn still marked as streaming and marks it as stopped, keeping the text that had arrived.

Event types the UI does not know yet are kept and listed, so a newer API never breaks an older UI.

## Sign-in

1. The sign-in form sends the username and password to Keycloak's token endpoint, from the UI server, as the public client `enterprise-knowledge-ui` (the OAuth password grant).
2. Keycloak returns an access token (5 minutes) and a refresh token (30 minutes of inactivity). Both stay in the Streamlit session on the UI server; they never reach the browser, a cookie or the URL.
3. The UI calls `GET /me` with the access token and shows the user's name and role. The list of tools comes from the same reply, so the UI never decides permissions itself. Tools the role cannot use are not shown; the API would refuse them anyway.
4. Before each question the token is refreshed if it is about to expire. If the refresh fails, or the API answers 401, the user is signed out with "Your session has ended. Please sign in again."
5. Sign out ends the session in Keycloak (so the refresh token stops working) and clears the tokens, the profile and the conversations of the tab.

A refused sign-in always says "Wrong username or password.", whatever Keycloak's reason, so the form does not reveal which usernames exist. Keycloak locks an account for a while after 5 failed attempts.

The password grant keeps this to one page with no redirect. It is fine on localhost; a deployment should use the browser redirect with PKCE, which is already switched on for this client in the realm file (see [ASSUMPTIONS](../docs/ASSUMPTIONS.md), S7).

## Settings

| Variable | Default | What it does |
|---|---|---|
| `API_URL` | `http://localhost:8000` | Where the API is. Docker Compose sets `http://api:8000` |
| `UI_CONNECT_TIMEOUT` | `5` | Seconds to wait for the API to accept a question |
| `UI_READ_TIMEOUT` | `60` | Longest silence accepted while an answer streams. The API sends a keep-alive every 15 seconds, so a longer silence means it is gone |
| `KEYCLOAK_URL` | `http://localhost:8080` | Where users sign in. Docker Compose sets `http://keycloak:8080` |
| `KEYCLOAK_REALM` | `helix` | The realm from [infra/keycloak/realm-helix.json](../infra/keycloak/realm-helix.json) |
| `KEYCLOAK_CLIENT_ID` | `enterprise-knowledge-ui` | The UI's public client in that realm |

Streamlit's own settings are in [.streamlit/config.toml](../.streamlit/config.toml): cross-site request forgery protection on, usage statistics off, short error details in the browser, and no lookup of the machine's public IP on start.

## Approval and feedback

- When a research question needs approval, the answer shows what will run and two buttons, **Approve** and **Reject**. Approving runs the research (a spinner shows while it works); rejecting answers with one normal search instead.
- Every finished answer has **thumbs up** and **thumbs down** buttons. The vote goes to `POST /feedback`, which saves it and attaches it to the answer's LangSmith trace.

## Rate limits

A question over a limit is answered with 429. The UI shows the API's message, which says how long to wait ("Please wait 10 seconds and try again", or when the daily budget resets at 00:00 UTC). The sidebar shows today's token usage against the daily budget, read again from `GET /me` after each answer.

## Left out on purpose

- **Links to the source documents.** Sources show the document ID, title and section. A link needs a document endpoint that checks the reader's role.

## Tests

- `tests/unit/test_ui_sse.py`, `test_ui_conversation.py`, `test_ui_format.py`: the event reader, the client rules and the panel's wording.
- `tests/unit/test_ui_auth.py`: signing in, refreshing, signing out and Keycloak errors, against a fake Keycloak.
- `tests/integration/test_ui_client.py`: the client against replies recorded from the real API (scripted agents), including rewrites, refusals (401, 404, 409, 422, 503), lost connections, timeouts, Stop, and `GET /me` for each role.
- `tests/integration/test_ui_app.py`: the whole page, run headless with Streamlit's `AppTest`: signing in and out, a wrong password, the tools each role is shown, an expired session, asking, following up, example questions, errors, switching conversation, Stop.
- `tests/security/test_ui_security.py`: HTML in an answer is never run, only the token is sent (no role, no dev header), Streamlit's protections, and an image without backend code.
- `tests/e2e/test_keycloak_login.py` (`make test-keycloak`): sign-in against the real Keycloak.

The replies the UI is tested with are recorded from the real API ([tests/helpers/ui.py](../tests/helpers/ui.py)), so a change to the stream that the UI does not handle fails these tests.
