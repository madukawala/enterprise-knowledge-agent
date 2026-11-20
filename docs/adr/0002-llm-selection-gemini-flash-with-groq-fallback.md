# ADR-0002: Gemini Flash as the main LLM, Groq as the fallback

- **Status:** Accepted
- **Date:** 2026-11-20

## Context

The assignment asks for a modern LLM and a documented reason for the choice. The model must:

- call tools reliably (supervisor routing, MCP tools, search plans);
- handle long context (retrieved chunks, research batches);
- stream tokens fast enough for the chat window;
- cost nothing for development and the demo.

The job description stresses failover and treating models as external services, so one provider is not enough. A single provider outage or a free-tier rate-limit lockout must not stop the assistant.

## Decision

- **Primary:** Google **Gemini Flash** (current Flash model through the Google AI Studio free tier). It handles supervisor routing, tool calling, research, aggregation and answers.
- **Fallback:** **Groq** free tier, `llama-3.3-70b-versatile`. It is a different provider on different infrastructure, so failover survives a full Gemini outage.
- Both sit behind one **provider-neutral LLM gateway** (LangChain chat models): per-call timeout, retries with exponential back-off and jitter, and a **circuit breaker per provider** (closed → open after repeated failures → half-open probe). Model names, order and thresholds are set in config.
- Each switch is recorded as a LangSmith tag (`llm_provider`), a Prometheus counter and an activity-panel event. The demo forces the Gemini breaker open and the trace shows Groq taking over.
- If Groq sign-up is not available, **Cerebras** or **OpenRouter** (free models) replace it with a config change; the gateway does not change.

## Consequences

- **Positive:** real failover across providers with no local model; very fast responses from Groq.
- **Positive:** prompts are written to work on both models (plain instructions, JSON-schema tool definitions), which keeps the system model-agnostic.
- **Negative:** two models answer slightly differently. The validator node checks every answer the same way whichever model wrote it, and evaluations are run on both.
- **Negative:** free tiers have per-minute and per-day limits. Mitigations: back-off, the breaker, a small research depth and fan-out, summaries for long threads, and a per-user daily token budget.
- **Negative:** free-tier data may be used by the provider to improve its services. Only the fictional Helix Bank corpus is used, and this is listed in [ASSUMPTIONS.md](../ASSUMPTIONS.md). A real bank would use a paid, no-training tier or a private deployment.

## Alternatives considered

| Option | Why not |
|---|---|
| Ollama with a local model as the fallback | Uses laptop RAM needed by Docker; slow and weaker tool calling on an M1 Pro. Dropped. |
| Gemini only, with retries | No protection from a Gemini outage or quota lockout |
| Groq as the primary | Smaller context and tighter daily token limits than Gemini's free tier; better as the fast fallback |
| Paid commercial LLM APIs | No suitable free tier |

## Related

[07-failure-handling](../architecture/07-failure-handling.drawio.svg) · resilience tests in [TESTING.md](../TESTING.md)
