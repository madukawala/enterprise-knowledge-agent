"""One way for every agent to call a language model, with failover.

Agents never talk to Gemini or OpenRouter directly. They ask the gateway for
either plain text or a filled-in Pydantic model, and the gateway:

1. tries each provider that has a key, in order: Gemini, then OpenRouter
   (ADR-0002);
2. limits every call to `LLM_TIMEOUT_SECONDS`;
3. treats an error, a timeout or output that does not fit the schema as a
   failure of that provider, and moves on to the next one;
4. raises `LLMUnavailableError` only when every provider has failed. The
   calling node catches it and degrades (see the node docstrings).

Every call returns an `LLMCall` record (provider, model, tokens, time), so
the answer can say which model wrote it and the activity panel can show it.

The circuit breaker (stop calling a provider that keeps failing for a
while) is added in `feature/resilience`, inside `_call`, without changing
the agents.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import Runnable
from pydantic import BaseModel, SecretStr

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
    """One configured model, e.g. ("gemini", "gemini-3.6-flash", <chat model>)."""

    name: str
    model: str
    chat: BaseChatModel


class _UnusableOutputError(Exception):
    """The model answered, but the answer cannot be used (empty or wrong shape)."""


@dataclass
class LLMGateway:
    providers: Sequence[ChatProvider]
    timeout_seconds: float = 30.0
    metrics: AgentMetrics | None = None
    _structured: dict[tuple[str, type[BaseModel]], Runnable[Any, Any]] = field(
        default_factory=dict, init=False, repr=False
    )

    async def structured(
        self, schema: type[SchemaT], messages: Sequence[BaseMessage], *, purpose: str
    ) -> tuple[SchemaT, LLMCall]:
        """Ask for an instance of `schema`, filled in by the model."""

        async def invoke(provider: ChatProvider) -> tuple[SchemaT, AIMessage]:
            runnable = self._structured_runnable(provider, schema)
            output = await runnable.ainvoke(list(messages))
            parsed = output.get("parsed")
            if not isinstance(parsed, schema):
                raise _UnusableOutputError(f"output did not match {schema.__name__}")
            raw = output.get("raw")
            return parsed, raw if isinstance(raw, AIMessage) else AIMessage("")

        return await self._call(purpose, invoke)

    async def text(self, messages: Sequence[BaseMessage], *, purpose: str) -> tuple[str, LLMCall]:
        """Ask for a plain text reply."""

        async def invoke(provider: ChatProvider) -> tuple[str, AIMessage]:
            reply = await provider.chat.ainvoke(list(messages))
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
            self._structured[key] = provider.chat.with_structured_output(schema, include_raw=True)
        return self._structured[key]

    async def _call(
        self,
        purpose: str,
        invoke: Callable[[ChatProvider], Awaitable[tuple[ResultT, AIMessage]]],
    ) -> tuple[ResultT, LLMCall]:
        for position, provider in enumerate(self.providers):
            started = time.perf_counter()
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    result, raw = await invoke(provider)
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
                continue

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


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class LLMSetupError(Exception):
    """No language model is configured. The message says what to do."""


def build_gateway(settings: Settings, *, metrics: AgentMetrics | None = None) -> LLMGateway:
    """Create the providers that have a key, in failover order.

    The model names come from `.env`; the settings already refuse a key
    without its model (core/config.py).
    """
    providers = [
        ChatProvider(name=name, model=model, chat=_chat_model(name, key, model, settings))
        for name, key, model in settings.llm_providers()
    ]
    if not providers:
        raise LLMSetupError(
            "No language model is configured. Add GEMINI_API_KEY and GEMINI_MODEL to .env, "
            "and ideally OPENROUTER_API_KEY and OPENROUTER_MODEL as the fallback "
            "(see .env.example)."
        )
    if len(providers) == 1:
        log.warning("llm_no_fallback", provider=providers[0].name)
    return LLMGateway(providers, timeout_seconds=settings.llm_timeout_seconds, metrics=metrics)


def _chat_model(name: str, key: SecretStr, model: str, settings: Settings) -> BaseChatModel:
    """The LangChain chat model for one provider."""
    # Imported here so the rest of the package (and the tests) never load
    # the provider SDKs unless a real model is used.
    if name == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=key,
            temperature=settings.llm_temperature,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
    if name == "openrouter":
        # OpenRouter speaks the OpenAI API, so the OpenAI client is used with
        # OpenRouter's address.
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model_name=model,
            openai_api_key=key,
            openai_api_base=OPENROUTER_BASE_URL,
            temperature=settings.llm_temperature,
            request_timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            # Token counts are only sent with streamed replies when asked for.
            stream_usage=True,
        )
    raise LLMSetupError(f"Unknown language model provider: {name}")
