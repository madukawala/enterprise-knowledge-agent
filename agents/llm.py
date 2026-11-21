"""One way for every agent to call a language model, with failover.

Agents never talk to Claude or Gemini directly. They ask the gateway for
either plain text or a filled-in Pydantic model, and the gateway:

1. tries each provider that has a key, in order: Claude, then Gemini
   (ADR-0017);
2. limits every call to `LLM_TIMEOUT_SECONDS`;
3. treats an error, a timeout or output that does not fit the schema as a
   failure of that provider, and moves on to the next one;
4. raises `LLMUnavailableError` only when every provider has failed. The
   calling node catches it and degrades (see the node docstrings).

Every call returns an `LLMCall` record (provider, model, tokens, time), so
the answer can say which model wrote it and the activity panel can show it.

Each provider has a circuit breaker (agents/breaker.py): a provider that
keeps failing, or that answers "rate limited", is skipped for a while and
then probed with one call, so questions do not wait for a provider that is
down. Retries with back-off inside one provider are done by the provider's
own SDK (`LLM_MAX_RETRIES`).

Claude is paid per token, so `_chat_model` sets it up to spend as little as
possible:

- no thinking and low effort: the agents' tasks are routing, short plans
  and answers from sources they are given, which do not need long
  reasoning, and thinking tokens are billed as output;
- a cap on the length of every reply (`ANTHROPIC_MAX_TOKENS`);
- filled-in models use Claude's own JSON output instead of a tool call,
  which sends fewer tokens and always returns valid JSON;
- the rules at the start of every prompt (the system message) are marked
  for Claude's prompt cache. The same rules are sent with every question,
  and a cached read costs a tenth of the normal price.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.runnables import Runnable
from pydantic import BaseModel, SecretStr

from agents.breaker import BreakerState, CircuitBreaker, is_rate_limit
from agents.state import LLMCall
from agents.telemetry import AgentMetrics
from core.config import Settings
from core.errors import LLMUnavailableError
from core.logging import get_logger

log = get_logger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)
ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class ChatProvider:
    """One configured model, e.g. ("anthropic", "claude-haiku-5-5", <chat model>)."""

    name: str
    model: str
    chat: BaseChatModel
    # How `with_structured_output` asks for a filled-in model. None keeps
    # LangChain's default for the provider.
    structured_method: str | None = None
    # Mark the system message for the provider's prompt cache (Claude only).
    cache_system_prompt: bool = False

    def prepare(self, messages: Sequence[BaseMessage]) -> list[BaseMessage]:
        """The messages as this provider should receive them."""
        if not self.cache_system_prompt:
            return list(messages)
        return [_cached(message) for message in messages]


def _cached(message: BaseMessage) -> BaseMessage:
    """Ask Claude to cache everything up to the end of the system message.

    The system message holds the shared rules and the task of one agent. It
    is the same for every question on a day (it includes today's date), so
    after the first call it is read from the cache at a tenth of the price.
    The question and sources come after it and are never cached. A prompt
    shorter than the model's minimum (512 tokens on current models) is
    simply not cached; nothing fails.
    """
    if not isinstance(message, SystemMessage) or not isinstance(message.content, str):
        return message
    return SystemMessage(
        [{"type": "text", "text": message.content, "cache_control": {"type": "ephemeral"}}]
    )


class _UnusableOutputError(Exception):
    """The model answered, but the answer cannot be used (empty or wrong shape)."""


@dataclass
class LLMGateway:
    providers: Sequence[ChatProvider]
    timeout_seconds: float = 30.0
    metrics: AgentMetrics | None = None
    # One per provider name. A provider without one is always tried.
    breakers: dict[str, CircuitBreaker] = field(default_factory=dict)
    _structured: dict[tuple[str, type[BaseModel]], Runnable[Any, Any]] = field(
        default_factory=dict, init=False, repr=False
    )

    async def structured(
        self, schema: type[SchemaT], messages: Sequence[BaseMessage], *, purpose: str
    ) -> tuple[SchemaT, LLMCall]:
        """Ask for an instance of `schema`, filled in by the model."""

        async def invoke(provider: ChatProvider) -> tuple[SchemaT, AIMessage]:
            runnable = self._structured_runnable(provider, schema)
            output = await runnable.ainvoke(provider.prepare(messages))
            parsed = output.get("parsed")
            if not isinstance(parsed, schema):
                raise _UnusableOutputError(f"output did not match {schema.__name__}")
            raw = output.get("raw")
            return parsed, raw if isinstance(raw, AIMessage) else AIMessage("")

        return await self._call(purpose, invoke)

    async def text(self, messages: Sequence[BaseMessage], *, purpose: str) -> tuple[str, LLMCall]:
        """Ask for a plain text reply."""

        async def invoke(provider: ChatProvider) -> tuple[str, AIMessage]:
            reply = await provider.chat.ainvoke(provider.prepare(messages))
            if not isinstance(reply, AIMessage) or not reply.text.strip():
                raise _UnusableOutputError("empty reply")
            return reply.text.strip(), reply

        return await self._call(purpose, invoke)

    def _structured_runnable(
        self, provider: ChatProvider, schema: type[BaseModel]
    ) -> Runnable[Any, Any]:
        # Built once per provider and schema, then reused.
        key = (provider.name, schema)
        if key not in self._structured:
            options = {"method": provider.structured_method} if provider.structured_method else {}
            self._structured[key] = provider.chat.with_structured_output(
                schema, include_raw=True, **options
            )
        return self._structured[key]

    async def _call(
        self,
        purpose: str,
        invoke: Callable[[ChatProvider], Awaitable[tuple[ResultT, AIMessage]]],
    ) -> tuple[ResultT, LLMCall]:
        for position, provider in enumerate(self.providers):
            breaker = self.breakers.get(provider.name)
            if breaker is not None and not breaker.allow():
                log.info("llm_provider_skipped", provider=provider.name, purpose=purpose)
                self._count(provider.name, purpose, "skipped")
                continue
            started = time.perf_counter()
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    result, raw = await invoke(provider)
            except _UnusableOutputError as error:
                # The provider is up but gave a bad answer: try the next one,
                # without counting it against the provider's breaker.
                log.warning(
                    "llm_call_failed",
                    provider=provider.name,
                    model=provider.model,
                    purpose=purpose,
                    outcome="bad_output",
                    error_type=type(error).__name__,
                )
                self._count(provider.name, purpose, "bad_output")
                if breaker is not None:
                    breaker.success()
                continue
            except Exception as error:  # any provider error means "try the next one"
                # Provider SDKs raise many different error types. Only the
                # type is logged: messages can echo parts of the prompt.
                outcome = "timeout" if isinstance(error, TimeoutError) else "error"
                log.warning(
                    "llm_call_failed",
                    provider=provider.name,
                    model=provider.model,
                    purpose=purpose,
                    outcome=outcome,
                    error_type=type(error).__name__,
                )
                self._count(provider.name, purpose, outcome)
                if breaker is not None:
                    breaker.failure(rate_limited=is_rate_limit(error))
                continue

            if breaker is not None:
                breaker.success()
            record = _record(purpose, provider, raw, started, fallback=position > 0)
            self._count(provider.name, purpose, "ok")
            if self.metrics:
                self.metrics.llm_tokens.labels(provider=provider.name, direction="input").inc(
                    record.input_tokens
                )
                self.metrics.llm_tokens.labels(provider=provider.name, direction="output").inc(
                    record.output_tokens
                )
                if record.fallback:
                    self.metrics.llm_fallbacks.labels(provider=provider.name).inc()
            if record.fallback:
                log.warning("llm_fallback_used", provider=provider.name, purpose=purpose)
            return result, record

        log.error("llm_unavailable", purpose=purpose, providers=len(self.providers))
        raise LLMUnavailableError()

    def _count(self, provider: str, purpose: str, outcome: str) -> None:
        if self.metrics:
            self.metrics.llm_calls.labels(provider=provider, purpose=purpose, outcome=outcome).inc()


def _record(
    purpose: str, provider: ChatProvider, raw: AIMessage, started: float, *, fallback: bool
) -> LLMCall:
    usage: Mapping[str, Any] = raw.usage_metadata or {}
    return LLMCall(
        purpose=purpose,
        provider=provider.name,
        model=provider.model,
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        duration_ms=round((time.perf_counter() - started) * 1000),
        fallback=fallback,
    )


class LLMSetupError(Exception):
    """No language model is configured. The message says what to do."""


def build_gateway(settings: Settings, *, metrics: AgentMetrics | None = None) -> LLMGateway:
    """Create the providers that have a key, in failover order.

    The model names come from `.env`; the settings already refuse a key
    without its model (core/config.py).
    """
    providers = [
        _provider(name, key, model, settings) for name, key, model in settings.llm_providers()
    ]
    if not providers:
        raise LLMSetupError(
            "No language model is configured. Add ANTHROPIC_API_KEY and CLAUDE_MODEL to .env, "
            "and ideally GEMINI_API_KEY and GEMINI_MODEL as the backup (see .env.example)."
        )
    if len(providers) == 1:
        log.warning("llm_no_fallback", provider=providers[0].name)

    def on_change(provider: str, state: BreakerState) -> None:
        if metrics is not None:
            metrics.llm_breaker_state.labels(provider=provider).set(_STATE_VALUE[state])

    breakers = {
        provider.name: CircuitBreaker(
            provider.name,
            failures=settings.llm_breaker_failures,
            window_seconds=settings.llm_breaker_window_seconds,
            open_seconds=settings.llm_breaker_open_seconds,
            on_change=on_change,
        )
        for provider in providers
    }
    return LLMGateway(
        providers,
        timeout_seconds=settings.llm_timeout_seconds,
        metrics=metrics,
        breakers=breakers,
    )


# For the `llm_breaker_state` gauge: 0 closed, 1 half-open, 2 open.
_STATE_VALUE = {BreakerState.CLOSED: 0, BreakerState.HALF_OPEN: 1, BreakerState.OPEN: 2}


def _provider(name: str, key: SecretStr, model: str, settings: Settings) -> ChatProvider:
    """One provider with its chat model, set up for the agents."""
    # Imported here so the rest of the package (and the tests) never load
    # the provider SDKs unless a real model is used.
    if name == "anthropic":
        from langchain_anthropic import ChatAnthropic

        claude = ChatAnthropic(
            model=model,
            anthropic_api_key=key,
            max_tokens=settings.anthropic_max_tokens,
            # No thinking, and low effort: see the module docstring. The
            # temperature is left at Claude's default, because the current
            # models refuse any other value.
            thinking={"type": "disabled"},
            output_config={"effort": settings.anthropic_effort},
            default_request_timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        return ChatProvider(
            name,
            model,
            claude,
            structured_method="json_schema",
            cache_system_prompt=True,
        )
    if name == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        gemini = ChatGoogleGenerativeAI(
            model=model,
            google_api_key=key,
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        return ChatProvider(name, model, gemini)
    raise LLMSetupError(f"Unknown language model provider: {name}")
