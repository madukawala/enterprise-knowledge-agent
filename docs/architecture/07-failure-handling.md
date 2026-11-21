# 7. Failure handling, as built

The picture is [07-failure-handling.drawio.svg](07-failure-handling.drawio.svg). This page lists every failure the system plans for, what the user sees, and the test that proves it. The rule throughout: a failing dependency makes the answer smaller, never makes the request crash, and the answer or the activity panel says what was missing.

| Failure | What happens | What the user sees | Proven by |
|---|---|---|---|
| Claude fails or times out | The gateway moves to Gemini for that call; the provider's SDK has already retried once with back-off | The answer, with "the main model did not answer, so gemini / ... was used" in the activity panel | `tests/unit/test_llm_gateway.py` |
| Claude keeps failing, or answers "rate limited" | Its circuit breaker opens: Claude is skipped for 30 seconds, then one probe call decides whether to use it again. A 429 opens the breaker at once | Answers keep coming from Gemini without waiting for Claude to time out; `llm_breaker_state{provider="anthropic"}` shows 2 | `tests/resilience/test_llm_failover.py` |
| Both providers down | Every node degrades: the supervisor uses its default plan, the answer lists the most relevant sources | "I couldn't write a full answer just now. These documents look most relevant ..."; with every breaker open this comes at once | `tests/graph/test_failure_containment.py`, `tests/resilience/test_llm_failover.py` |
| A model answers in the wrong shape | That provider's answer is dropped and the next one is tried; this does not count against the breaker | Nothing, unless every provider fails | `tests/resilience/test_llm_failover.py`, `tests/unit/test_llm_gateway.py` |
| Pinecone down or slow | The search is retried once; then the answer says document search is unavailable | "Document search is unavailable right now" | `tests/graph/test_failure_containment.py` |
| One collection fails | The others' results are used | A limit naming the missing collections | `tests/unit/test_retrieval.py` |
| Re-ranker fails or out of quota | The hybrid order is kept | A limit: results are in search-score order | `tests/unit/test_retrieval.py` |
| Python sandbox down | The Python tool answers 503; research uses its fixed plan (the last twelve months by quarter); `/readyz` reports `degraded` | The tool's message; research still answers | `tests/integration/test_python_tool_api.py`, `tests/graph/test_research.py`, `tests/unit/test_readiness.py` |
| Code runs too long or uses too much memory | The sandbox kills it | `timeout` or `crashed`, with the reason | `tests/security/test_sandbox_escapes.py`, `tests/e2e/test_sandbox_container.py` |
| The MCP server is down, or a tool call fails | The documents answer the question | A limit: some enterprise data could not be fetched | `tests/graph/test_tools_agent.py` |
| A research batch fails | The other batches still count | A limit naming the missing batch | `tests/graph/test_research.py` |
| Every research batch fails | The supervisor runs one wide search instead | A normal answer, with the step in "How I got this answer" | `tests/graph/test_research.py` |
| Redis down | Each API worker keeps its own rate-limit counts, with limits halved; admin changes to limits get 503 | Nothing, except stricter limits | `tests/integration/test_rate_limit_redis.py` |
| Keycloak down | Signing keys already fetched keep working; with none, requests get 503 `auth_unavailable` (not 401, so the UI does not sign users out) | "Sign-in is unavailable right now" only if the API has never fetched the keys | `tests/unit/test_jwks_client.py` |
| Long-term memory store fails | The question is answered without memory | Nothing | `tests/graph/test_memory.py` |
| LangSmith down | Tracing errors are logged; the app carries on | Nothing | `tests/integration/test_agents.py` |
| The client disconnects mid-answer | The run is cancelled, including the model call in progress; no backup model is called | Nothing (they left) | `tests/resilience/test_chat_stream.py` |
| A run takes too long | Stopped after `CHAT_TIMEOUT_SECONDS` | An `error` event: the answer took too long | `tests/resilience/test_chat_stream.py` |
| Rate limit or token budget reached | 429 with `Retry-After` | "Please wait 10 seconds", or when the budget resets | `tests/security/test_rate_limit_api.py` |
| Invalid request | 422 with the field and the reason | A clear message | `tests/integration/test_chat_api.py` |
| An unexpected bug in one agent | The node's wrapper logs it, counts it, and uses that node's safe fallback | A limit saying what was missing | `tests/graph/test_failure_containment.py` |

## Showing it live

Each of these can be shown by stopping a container while the stack runs:

```bash
docker compose stop sandbox     # research falls back to the fixed plan; /readyz says degraded
docker compose stop redis       # rate limits move into each API worker, halved
docker compose stop keycloak    # signed-in users carry on with the cached keys
docker compose start sandbox redis keycloak
```

To see the model failover, set `ANTHROPIC_API_KEY` to a wrong value in `.env` and restart the API: every answer comes from Gemini, the activity panel says so, and after five failures in a minute `llm_breaker_state{provider="anthropic"}` on `/metrics` shows the breaker open.
