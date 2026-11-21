# 10. Request lifecycle

One chat request from login to the streamed answer, showing where each security layer and each piece of observability sits.

```mermaid
sequenceDiagram
    actor U as User
    participant UI as Streamlit UI
    participant KC as Keycloak
    participant MW as API middleware
    participant RL as Redis (token bucket)
    participant G as LangGraph run
    participant PG as Postgres
    participant LS as LangSmith

    U->>UI: open the app
    UI->>KC: OIDC login (password grant from the UI server; PKCE redirect in production)
    KC-->>UI: access token (JWT with realm roles)
    U->>UI: ask a question
    UI->>MW: POST /chat (Bearer JWT, thread_id, message)
    MW->>MW: add request_id
    MW->>MW: verify JWT (cached JWKS): signature, issuer, audience, expiry
    alt invalid or expired token
        MW-->>UI: 401
    end
    MW->>RL: take a token (user and role bucket)
    alt bucket empty
        MW-->>UI: 429 + Retry-After
    end
    MW->>MW: validate the body (Pydantic schema, size limits)
    MW->>G: start the run (user, role, allowed tools, thread_id)
    G->>PG: load the thread checkpoint
    G->>LS: trace the run (tags: role, thread, request_id, pseudonymised user ID)
    G-->>UI: SSE events (see 09-streaming-events)
    G->>PG: save checkpoint, memory, audit row
    G-->>UI: response_end (citations, provider, LangSmith run ID)
```

## What each step adds

| Step | Security | Observability |
|---|---|---|
| Login | Keycloak OIDC, short-lived tokens | login events in Keycloak |
| Middleware | JWT check, rate limit, input schema, CORS and security headers | `request_id` on every log line and response header |
| Graph run | injection scan, role-filtered tools, role check before each tool, permission filters on retrieval, output checks | LangSmith trace per run, SSE events to the activity panel, Prometheus metrics per node |
| Save | PII masked before memory is saved; append-only audit row | audit log in Postgres, `security` log channel for blocks |
