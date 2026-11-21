"""Calls the real language models. Run with `make test-live`.

Uses ANTHROPIC_API_KEY and GEMINI_API_KEY from .env. Claude is paid per
token, but the whole run costs well under one US cent with
claude-haiku-5-5. Each provider is checked on its own, so a wrong or retired model
name in the settings shows up here, not in front of a user. The full
question test also needs `make ingest`.
"""

from datetime import date

import pytest
from langchain_core.messages import HumanMessage

from agents.context import prompt
from agents.llm import ChatProvider, LLMGateway, build_gateway
from agents.nodes.supervisor import SupervisorDecision
from agents.service import open_agent_service
from agents.state import AnswerKind, Intent
from core.config import Settings
from rag.bm25 import PARAMS_FILE
from security.roles import Role

pytestmark = pytest.mark.live

# The real supervisor prompt, so the model sees the same intent definitions
# as in the app.
QUESTION = prompt(
    "supervisor",
    "Summarize payment-failure outages over the last year",
    [],
    today=date.today(),
    history=6,
)


def _providers() -> list[ChatProvider]:
    settings = Settings()
    if not settings.active_llm_providers():
        return []
    return list(build_gateway(settings).providers)


@pytest.mark.parametrize("provider", _providers(), ids=lambda p: p.name)
async def test_each_configured_model_answers_with_structured_output(
    provider: ChatProvider,
) -> None:
    gateway = LLMGateway([provider], timeout_seconds=60)
    decision, call = await gateway.structured(SupervisorDecision, QUESTION, purpose="live")
    assert decision.intent in {Intent.RESEARCH, Intent.DOCUMENT_QUESTION}
    assert call.provider == provider.name
    assert call.output_tokens > 0


@pytest.mark.parametrize("provider", _providers(), ids=lambda p: p.name)
async def test_each_configured_model_writes_plain_text(provider: ChatProvider) -> None:
    gateway = LLMGateway([provider], timeout_seconds=60)
    text, _ = await gateway.text(
        [HumanMessage("Reply with the single word: ready")], purpose="live"
    )
    assert "ready" in text.lower()


async def test_a_real_question_is_answered_with_citations() -> None:
    settings = Settings()
    if not settings.active_llm_providers():
        pytest.skip("no model key is set")
    if (
        settings.pinecone_api_key is None
        or not (settings.index_artifacts_dir / PARAMS_FILE).is_file()
    ):
        pytest.skip("needs PINECONE_API_KEY and `make ingest`")

    async with open_agent_service(settings) as agents:
        run = await agents.ask(
            "What should I do when the PayHub database connection pool runs out?",
            role=Role.VIEWER,
        )

    assert run.answer.kind is AnswerKind.ANSWERED, run.answer.to_markdown()
    assert "RB-001" in {item.citation.doc_id for item in run.answer.citations}
