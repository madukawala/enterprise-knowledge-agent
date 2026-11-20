"""Full graph runs with a fake model: the right agents run for each kind of question."""

from agents.state import AnswerKind
from core.errors import LLMUnavailableError
from rag.schema import Collection
from security.roles import Role
from tests.helpers.agents import (
    FakeSearcher,
    agent_service,
    decision,
    hit,
    result_with,
    search_plan,
)
from tests.helpers.llm import ScriptedChatModel

ANSWER_PATH = ("input_guard", "supervisor", "retrieval", "supervisor", "response", "validator")


async def test_a_document_question_is_searched_then_answered_with_citations() -> None:
    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("document_question")],
            "SearchPlan": [search_plan("PayHub connection pool exhausted", ["runbooks"])],
        },
        texts=["Increase the pool size and restart the workers [1]. Check the alert first [2]."],
    )
    searcher = FakeSearcher([result_with(hit("RB-001"), hit("RB-002"), hit("RB-003"))])

    run = await agent_service(model, searcher).ask("pool exhausted?", role=Role.VIEWER)

    assert run.nodes == ANSWER_PATH
    assert run.answer.kind is AnswerKind.ANSWERED
    assert [c.citation.doc_id for c in run.answer.citations] == ["RB-001", "RB-002"]
    assert run.answer.written_by == "gemini / gemini-test"
    assert [call.purpose for call in run.llm_calls] == ["supervisor", "search_plan", "answer"]
    request, role = searcher.requests[0]
    assert request.query == "PayHub connection pool exhausted"
    assert request.collections == (Collection.RUNBOOKS,)
    assert role is Role.VIEWER
    assert run.search is not None
    assert run.search.hits == 3


async def test_the_steps_explain_how_the_answer_was_made() -> None:
    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("document_question", ["Search runbooks", "Answer"])],
            "SearchPlan": [search_plan("pool exhausted")],
        },
        texts=["Restart it [1]."],
    )
    run = await agent_service(model, FakeSearcher([result_with(hit("RB-001"))])).ask(
        "pool?", role=Role.VIEWER
    )
    assert run.answer.steps == (
        "Understood the request as document question. Plan: Search runbooks; Answer.",
        'Searched runbooks for "pool exhausted". Found 4 matches and kept 1 after re-ranking.',
        "Wrote the answer from 1 sources with gemini / gemini-test.",
        "Checked that the cited source was retrieved in this run: passed.",
    )


async def test_small_talk_is_answered_without_searching() -> None:
    model = ScriptedChatModel(
        structured={"SupervisorDecision": [decision("conversation", ["Reply"])]},
        texts=["Hello! I can help with the bank's internal documents."],
    )
    searcher = FakeSearcher()

    run = await agent_service(model, searcher).ask("hi there", role=Role.VIEWER)

    assert run.nodes == ("input_guard", "supervisor", "response", "validator")
    assert run.answer.kind is AnswerKind.CONVERSATION
    assert searcher.requests == []
    # The small-talk prompt is used, and it gets no sources.
    assert "small talk" in model.prompts_for("text")[0]
    assert "<source id=" not in model.prompts_for("text")[0]


async def test_out_of_scope_requests_get_the_standard_refusal_without_a_model_answer() -> None:
    model = ScriptedChatModel(
        structured={"SupervisorDecision": [decision("out_of_scope", ["Decline politely"])]}
    )
    run = await agent_service(model, FakeSearcher()).ask(
        "Which shares should I buy?", role=Role.ANALYST
    )
    assert run.answer.kind is AnswerKind.OUT_OF_SCOPE
    assert "can't help" in run.answer.text
    # Only the supervisor used the model; no answer was generated.
    assert [call.purpose for call in run.llm_calls] == ["supervisor"]


async def test_research_questions_use_a_wider_search_and_say_so() -> None:
    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("research", ["Find outages", "Group root causes"])],
            "SearchPlan": [
                search_plan(
                    "payment failure outages",
                    ["incidents"],
                    departments=["payments"],
                    date_from="2025-11-21",
                    date_to="2026-11-21",
                )
            ],
        },
        texts=["Five outages share two root causes [1][2]."],
    )
    searcher = FakeSearcher([result_with(hit("INC-1"), hit("INC-2"))])

    run = await agent_service(model, searcher).ask(
        "Summarize payment-failure outages over the last year", role=Role.ANALYST
    )

    request = searcher.requests[0][0]
    assert request.top_k == 20
    assert request.filters.date_from is not None
    assert any("Deep research" in limit for limit in run.answer.limits)


async def test_enterprise_data_questions_search_documents_and_say_tools_are_not_connected() -> None:
    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("enterprise_data")],
            "SearchPlan": [search_plan("owner of PayHub")],
        },
        texts=["The Payments Platform team owns PayHub [1]."],
    )
    run = await agent_service(model, FakeSearcher([result_with(hit("ARC-001"))])).ask(
        "Who owns PayHub?", role=Role.ANALYST
    )
    assert run.answer.kind is AnswerKind.ANSWERED
    assert any("not connected yet" in limit for limit in run.answer.limits)


async def test_the_model_saying_not_found_gives_the_standard_message() -> None:
    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("document_question")],
            "SearchPlan": [search_plan("canteen menu")],
        },
        texts=["NOT_FOUND"],
    )
    run = await agent_service(model, FakeSearcher([result_with(hit("RB-001"))])).ask(
        "What is on the canteen menu?", role=Role.VIEWER
    )
    assert run.answer.kind is AnswerKind.NOT_FOUND
    assert run.answer.citations == ()


async def test_no_matching_documents_skips_the_answer_model() -> None:
    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("document_question")],
            "SearchPlan": [search_plan("canteen menu")],
        },
    )
    run = await agent_service(model, FakeSearcher([result_with()])).ask(
        "canteen menu?", role=Role.VIEWER
    )
    assert run.answer.kind is AnswerKind.NOT_FOUND
    assert "documents you can access" in run.answer.text
    assert [call.purpose for call in run.llm_calls] == ["supervisor", "search_plan"]


async def test_an_empty_question_is_refused_without_calling_anything() -> None:
    model = ScriptedChatModel()
    searcher = FakeSearcher()

    run = await agent_service(model, searcher).ask("   ​ ", role=Role.VIEWER)

    assert run.nodes == ("input_guard", "response", "validator")
    assert run.answer.kind is AnswerKind.REFUSED
    assert model.calls == []
    assert searcher.requests == []


async def test_an_overlong_question_is_refused() -> None:
    run = await agent_service(ScriptedChatModel(), FakeSearcher()).ask(
        "why " * 600, role=Role.VIEWER
    )
    assert run.answer.kind is AnswerKind.REFUSED
    assert "too long" in run.answer.text


async def test_a_rejected_draft_is_written_once_more_with_the_problems_as_notes() -> None:
    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("document_question")],
            "SearchPlan": [search_plan("pool")],
        },
        texts=["The limit is 200 [9].", "The limit is 200 [1]."],
    )
    run = await agent_service(model, FakeSearcher([result_with(hit("RB-001"))])).ask(
        "pool limit?", role=Role.VIEWER
    )
    assert run.nodes[-4:] == ("response", "validator", "response", "validator")
    assert run.answer.kind is AnswerKind.ANSWERED
    assert run.answer.text == "The limit is 200 [1]."
    assert "It cites [9]" in model.prompts_for("text")[1]


async def test_a_draft_rejected_twice_is_replaced_by_the_source_list() -> None:
    model = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("document_question")],
            "SearchPlan": [search_plan("pool")],
        },
        texts=["No citations here.", "Still none."],
    )
    run = await agent_service(model, FakeSearcher([result_with(hit("RB-001"))])).ask(
        "pool limit?", role=Role.VIEWER
    )
    assert run.answer.kind is AnswerKind.FALLBACK
    assert [c.citation.doc_id for c in run.answer.citations] == ["RB-001"]
    assert "No citations here." not in run.answer.text


async def test_the_fallback_provider_writes_the_answer_when_the_first_fails() -> None:
    gemini = ScriptedChatModel(
        structured={
            "SupervisorDecision": [decision("document_question")],
            "SearchPlan": [search_plan("pool")],
        },
        texts=[LLMUnavailableError()],
    )
    openrouter = ScriptedChatModel(texts=["Restart it [1]."])
    from tests.helpers.agents import gateway

    run = await agent_service(
        gateway(gemini, openrouter), FakeSearcher([result_with(hit("RB-1"))])
    ).ask("pool?", role=Role.VIEWER)
    assert run.answer.kind is AnswerKind.ANSWERED
    assert run.answer.written_by == "openrouter / openrouter-test"
    assert run.llm_calls[-1].fallback is True
