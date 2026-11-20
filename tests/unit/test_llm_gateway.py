"""The LLM gateway: provider order, failover, time limits and call records."""

import asyncio

import pytest
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, SecretStr

from agents.llm import LLMSetupError, build_gateway
from agents.telemetry import AgentMetrics
from core.config import LogFormat, Settings
from core.errors import LLMUnavailableError
from core.logging import configure_logging
from tests.helpers.agents import gateway
from tests.helpers.llm import ScriptedChatModel

MESSAGES = [HumanMessage("hello")]


class Answer(BaseModel):
    value: int


def _value(metrics: AgentMetrics, name: str, **labels: str) -> float:
    return getattr(metrics, name).labels(**labels)._value.get()  # type: ignore[no-any-return]


async def test_the_first_provider_answers_when_it_works() -> None:
    gemini = ScriptedChatModel(texts=["from gemini"])
    backup = ScriptedChatModel(texts=["from openrouter"])

    text, call = await gateway(gemini, backup).text(MESSAGES, purpose="answer")

    assert text == "from gemini"
    assert (call.provider, call.model, call.fallback) == ("gemini", "gemini-test", False)
    assert (call.input_tokens, call.output_tokens) == (100, 20)
    assert backup.calls == []


async def test_the_next_provider_takes_over_when_the_first_fails() -> None:
    metrics = AgentMetrics()
    gemini = ScriptedChatModel(texts=[RuntimeError("quota exceeded")])
    backup = ScriptedChatModel(texts=["from openrouter"])

    text, call = await gateway(gemini, backup, metrics=metrics).text(MESSAGES, purpose="answer")

    assert text == "from openrouter"
    assert (call.provider, call.fallback) == ("openrouter", True)
    assert _value(metrics, "llm_fallbacks", provider="openrouter") == 1
    assert _value(metrics, "llm_calls", provider="gemini", purpose="answer", outcome="error") == 1


async def test_a_slow_provider_is_cut_off_and_the_next_one_used() -> None:
    metrics = AgentMetrics()
    slow = ScriptedChatModel(texts=["too late"], delay=1.0)
    fast = ScriptedChatModel(texts=["in time"])

    text, _ = await gateway(slow, fast, timeout=0.05, metrics=metrics).text(MESSAGES, purpose="x")

    assert text == "in time"
    assert _value(metrics, "llm_calls", provider="gemini", purpose="x", outcome="timeout") == 1


async def test_an_empty_reply_counts_as_a_failure() -> None:
    text, call = await gateway(
        ScriptedChatModel(texts=["   "]), ScriptedChatModel(texts=["real answer"])
    ).text(MESSAGES, purpose="answer")
    assert (text, call.provider) == ("real answer", "openrouter")


async def test_structured_output_is_parsed_into_the_schema() -> None:
    model = ScriptedChatModel(structured={"Answer": [{"value": 42}]})
    parsed, call = await gateway(model).structured(Answer, MESSAGES, purpose="plan")
    assert parsed == Answer(value=42)
    assert call.purpose == "plan"


async def test_output_that_does_not_fit_the_schema_moves_to_the_next_provider() -> None:
    broken = ScriptedChatModel(structured={"Answer": [{"value": "not a number"}]})
    good = ScriptedChatModel(structured={"Answer": [{"value": 7}]})
    parsed, call = await gateway(broken, good).structured(Answer, MESSAGES, purpose="plan")
    assert (parsed.value, call.provider) == (7, "openrouter")


async def test_every_provider_failing_raises_llm_unavailable() -> None:
    models = [ScriptedChatModel(texts=[RuntimeError("down")]) for _ in range(2)]
    with pytest.raises(LLMUnavailableError):
        await gateway(*models).text(MESSAGES, purpose="answer")


async def test_no_providers_means_unavailable_not_a_crash() -> None:
    with pytest.raises(LLMUnavailableError):
        await gateway().text(MESSAGES, purpose="answer")


async def test_cancellation_is_not_treated_as_a_provider_failure() -> None:
    """If the user leaves, the call stops; it must not fall over to the next provider."""
    slow = ScriptedChatModel(texts=["late"], delay=5)
    backup = ScriptedChatModel(texts=["backup"])
    task = asyncio.create_task(gateway(slow, backup, timeout=10).text(MESSAGES, purpose="x"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backup.calls == []


async def test_provider_errors_are_logged_by_type_only(capsys: pytest.CaptureFixture[str]) -> None:
    """Provider error messages can echo the prompt, so only the type is logged."""
    configure_logging("INFO", LogFormat.JSON)
    failing = ScriptedChatModel(texts=[RuntimeError("prompt text: my account 12345678")])
    await gateway(failing, ScriptedChatModel(texts=["ok"])).text(MESSAGES, purpose="answer")
    output = capsys.readouterr().out
    assert "llm_call_failed" in output
    assert "RuntimeError" in output
    assert "12345678" not in output


# ---------------------------------------------------------------
# build_gateway: providers from the settings
# ---------------------------------------------------------------


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_no_model_key_is_a_clear_setup_error() -> None:
    with pytest.raises(LLMSetupError, match="GEMINI_API_KEY"):
        build_gateway(_settings())


def test_providers_are_ordered_gemini_then_openrouter() -> None:
    built = build_gateway(
        _settings(
            openrouter_api_key=SecretStr("o-key"),
            openrouter_model="or-model",
            gemini_api_key=SecretStr("g-key"),
            gemini_model="gemini-model",
        )
    )
    assert [(p.name, p.model) for p in built.providers] == [
        ("gemini", "gemini-model"),
        ("openrouter", "or-model"),
    ]


def test_openrouter_is_reached_through_its_openai_compatible_address() -> None:
    from langchain_openai import ChatOpenAI

    (provider,) = build_gateway(
        _settings(openrouter_api_key=SecretStr("o-key"), openrouter_model="or-model")
    ).providers
    assert isinstance(provider.chat, ChatOpenAI)
    assert provider.chat.openai_api_base == "https://openrouter.ai/api/v1"
    assert provider.chat.model_name == "or-model"


def test_one_key_is_enough() -> None:
    built = build_gateway(_settings(gemini_api_key=SecretStr("g-key"), gemini_model="other-model"))
    assert [(p.name, p.model) for p in built.providers] == [("gemini", "other-model")]
