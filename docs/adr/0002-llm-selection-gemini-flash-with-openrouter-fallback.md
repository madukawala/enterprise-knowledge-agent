# ADR-0002: Gemini Flash as the main LLM, OpenRouter as the fallback

- **Status:** Accepted
- **Date:** 2026-11-20 (fallback provider and model names updated 2026-11-21)

## Context

The assignment asks for a modern LLM and a documented reason for the choice. The model must:

- call tools reliably (supervisor routing, MCP tools, search plans);
- handle long context (retrieved chunks, research batches);
- stream tokens fast enough for the chat window;
- cost nothing for development and the demo.

The job description stresses failover and treating models as external services, so one provider is not enough. A single provider outage or a free-tier rate-limit lockout must not stop the assistant.

## Decision

- **Primary:** Google **Gemini Flash** through the Google AI Studio free tier (recommended model `gemini-3.6-flash`). It handles supervisor routing, tool calling, research, aggregation and answers.
- **Fallback:** **OpenRouter** with a free, non-Google model (recommended model `nvidia/nemotron-3.5-super-vl:free`). It runs on different companies' infrastructure, so failover survives a full Google outage. Google models are left out on purpose, so one outage cannot take out both providers.
- Both sit behind one **provider-neutral LLM gateway** (LangChain chat models): a time limit on every call, a few retries inside each provider, and a **circuit breaker per provider** (closed → open after repeated failures → half-open probe). The breaker is added with `feature/resilience`. Provider order and thresholds are set in code and config.
- **Model names are chosen in `.env` only** (`GEMINI_MODEL`, `OPENROUTER_MODEL`), with no default in code. A provider with a key but no model stops the app at startup with a message naming the missing setting.
- Each switch is recorded on the answer (`llm_calls`, "written by"), as a log line, a Prometheus counter and a LangSmith trace step. The demo forces Gemini to fail, and the trace shows OpenRouter taking over.

## Consequences

- **Positive:** real failover across providers, with no local model.
- **Positive:** one OpenRouter key reaches many hosted models through the standard OpenAI API. If the free model is retired, the replacement is a change in `.env`.
- **Positive:** prompts are written to work on both models (plain instructions, JSON-schema output), which keeps the system model-agnostic.
- **Negative:** two models answer slightly differently. The validator node checks every answer the same way whichever model wrote it, and evaluations are run on both.
- **Negative:** free tiers have per-minute and per-day limits. Free OpenRouter models allow about 50 requests a day (about 16 questions), which suits a fallback and the failover demo, not a main model. Mitigations: back-off, the breaker, a small research depth and fan-out, summaries for long threads, and a per-user daily token budget.
- **Negative:** free-tier data may be used by the provider to improve its services, and OpenRouter's free models are only served by providers that may log prompts. Only the fictional Helix Bank corpus is used, and this is listed in [ASSUMPTIONS.md](../ASSUMPTIONS.md). A real bank would use a paid, no-training tier or a private deployment.

## Update during implementation

- **Model names.** Google has deprecated Gemini 2.5 Flash for new projects. The newer stable `gemini-3.8-flash` was tried first, but on the free tier it returned "503 Service Unavailable" on every call, while `gemini-3.6-flash` answered. A name kept in code would go stale without anyone noticing, so model names have no default: the next retirement is a change in `.env`, made by someone who knows which model they are choosing. `.env.example` lists the recommended models. Embedding and re-ranking models keep their defaults, because they are tied to the index and are not retired the way free chat models are. `make test-live` calls each configured model on its own, so a retired name shows up there first.
- **Choosing the OpenRouter model.** Every free, non-Google model that supports structured output was tried with the real supervisor and search-plan prompts. `nvidia/nemotron-3.5-super-vl:free` filled both correctly in 2 to 4 seconds; others were rate-limited upstream or much slower. OpenRouter is reached with LangChain's OpenAI client and OpenRouter's address.
- **The gateway is a short loop of our own, not LangChain's `with_fallbacks()`.** The loop ([agents/llm.py](../../agents/llm.py)) knows which provider answered, which the answer, the metrics and the activity panel all need. It treats output that does not fit the schema as a failure, and it gives the circuit breaker (`feature/resilience`) one place to go.
- Each call has its own time limit (`LLM_TIMEOUT_SECONDS`, default 30) and at most `LLM_MAX_RETRIES` retries inside one provider before the next provider is tried.
- One key is enough to run the agents. With only one, a warning is logged that there is no fallback.
- **Two models really do answer differently.** Given sources that described a problem but not its fix, Gemini answered in part and the OpenRouter model replied NOT_FOUND. The answer prompt now says when to answer in part, and the validator checks both the same way.

## Alternatives considered

| Option | Why not |
|---|---|
| Ollama with a local model as the fallback | Uses laptop RAM needed by Docker; slow and weaker tool calling on an M1 Pro |
| Gemini only, with retries | No protection from a Gemini outage or quota lockout |
| A Google model (Gemma) on OpenRouter as the fallback | A Google outage could take out both providers |
| Cerebras | Its free tier changed in 2026 and now appears to need a card; check before relying on it |
| Paid commercial LLM APIs | No suitable free tier |

## Related

[07-failure-handling](../architecture/07-failure-handling.drawio.svg) · [agents/README.md](../../agents/README.md) · resilience tests in [TESTING.md](../TESTING.md)
