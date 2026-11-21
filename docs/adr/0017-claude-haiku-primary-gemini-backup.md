# ADR-0017: Claude Haiku as the main LLM, Gemini as the backup

- **Status:** Accepted
- **Date:** 2026-11-21
- **Replaces:** [ADR-0002](0002-llm-selection-gemini-flash-with-openrouter-fallback.md)

## Context

ADR-0002 chose Gemini Flash on its free tier as the main model and a free OpenRouter model as the fallback, so the project would cost nothing. Running the full system and the answer evaluation on 21 November 2026 showed the limits of that choice:

- one day of normal testing used up both free tiers. Every call to Gemini and to OpenRouter was then refused with "rate limited" (429), and every answer fell back to the reduced mode (a list of the most relevant sources);
- the free OpenRouter model allows about 50 requests a day, roughly 16 questions, which is not enough for a demo, an evaluation run and a reviewer trying the app on the same day;
- free-tier providers may keep prompts to improve their services, which is the wrong habit to show for a bank, even with fictional data.

The assistant still needs to call tools and return structured output reliably, stream answers, and keep working when one provider is down. Cost still matters: it should be a few US dollars for the whole project, not a running bill.

## Decision

- **Main model: Anthropic Claude, `claude-haiku-5-5`.** It is the cheapest current Claude model (US$0.10 per million input tokens, US$0.50 per million output tokens), fast enough for streaming, and reliable with structured output.
- **Backup: Google Gemini, `gemini-3.6-flash`, on its free tier.** A different company, so an Anthropic outage or rate limit does not stop the assistant. The circuit breaker, time limits and failover from ADR-0002 are unchanged.
- **OpenRouter is removed.** Two providers from two companies already give real failover, and a third free provider with a 50-requests-a-day limit added code and settings but little safety. No local model is used (unchanged from ADR-0001).
- **Model names stay in `.env` only** (`CLAUDE_MODEL`, `GEMINI_MODEL`), with no default in code.
- **Claude is set up to spend as little as possible** ([agents/llm.py](../../agents/llm.py)):

| Setting | What it does | Why |
|---|---|---|
| Thinking off | Claude answers straight away, without a hidden reasoning step | The agents' tasks are routing, short plans and answers from given sources. Thinking tokens are billed as output, the most expensive kind |
| Effort `low` (`ANTHROPIC_EFFORT`) | Shorter, more direct replies | Enough for these tasks; can be raised to `medium` or `high` in `.env` |
| Reply cap (`ANTHROPIC_MAX_TOKENS`, 2,000) | No reply can be longer than this | Answers need well under 2,000 tokens; the cap stops a runaway reply |
| Claude's own JSON output for filled-in models | Claude returns JSON that always matches the schema, instead of calling a pretend tool | Measured on the supervisor prompt: 12% fewer input tokens and 40% fewer output tokens than a tool call |
| Prompt cache on the system message | The rules and task at the start of each prompt are cached for 5 minutes | Every question sends the same rules. A cached read costs a tenth of the normal price; in testing, 1,149 of 1,177 input tokens of a supervisor call came from the cache |
| No temperature | Claude runs at its default | The current Claude models refuse any other temperature. Gemini keeps `LLM_TEMPERATURE=0.2` |

The existing limits keep the total small as well: three model calls for a normal question, routing in code, research limits (depth, fan-out, a 40,000-token budget), and the per-user daily token budget from ADR-0015.

## Consequences

- **Positive:** answers no longer depend on free daily limits. A demo, an evaluation run and a reviewer can all use the assistant on the same day.
- **Positive:** low and predictable cost. Measured with `claude-haiku-5-5`: a document question used 3 calls and about 6,400 tokens (about US$0.001); a deep research question used 6 calls and about 31,000 tokens (about US$0.004).
- **Positive:** Anthropic does not use API data to train its models by default, which is closer to what a bank would accept.
- **Positive:** fewer settings and one fewer SDK (the OpenAI client is no longer needed).
- **Negative:** Claude is paid. Running the app needs an Anthropic key with credit. Without one, the app runs on Gemini alone (one key is still enough), with a warning that there is no backup.
- **Negative:** the two models still answer slightly differently. The validator checks every answer the same way, whichever model wrote it.
- **Negative:** strict JSON output cannot carry a free-form dictionary, because the model may only use keys the schema names. The tools agent's arguments are therefore listed field by field (`ToolArguments` in [agents/nodes/tools.py](../../agents/nodes/tools.py)), and `check_arguments` still refuses any argument that does not belong to the chosen tool.

## Notes from the implementation

- The Anthropic client needs `brotlicffi` 1.2 or newer. With 1.1, every non-streamed call failed with a misleading "connection error", although the API had answered. The version is pinned in `pyproject.toml`.
- The new settings are called `ANTHROPIC_MAX_TOKENS` and `ANTHROPIC_EFFORT`, not `CLAUDE_...`, because some developer tools already set environment variables starting with `CLAUDE_`, which would silently override `.env`.
- With thinking left on, Claude often chose not to think on the short supervisor call anyway. Turning it off makes sure no thinking tokens are spent on the longer calls (answers and research) either.

## Alternatives considered

| Option | Why not |
|---|---|
| Keep Gemini free tier as the main model | Daily limits were used up by one day of testing; the evaluation then measured only the reduced mode |
| Keep OpenRouter as a third provider | About 50 requests a day; more code and settings for little extra safety |
| Claude Sonnet or Opus as the main model | Several times the price; the agents' tasks do not need the extra reasoning |
| A local model with Ollama | Uses laptop memory needed by Docker; slow and weaker tool calling on an M1 Pro (ADR-0001) |
| Leave thinking on at the default effort | More output tokens on longer calls for tasks that do not need it |

## Related

[07-failure-handling](../architecture/07-failure-handling.md) · [agents/README.md](../../agents/README.md) · [ASSUMPTIONS.md](../ASSUMPTIONS.md) · [ADR-0015 rate limiting](0015-rate-limiting.md)
