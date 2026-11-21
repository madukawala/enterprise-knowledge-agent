"""The wording of the activity panel, and the UI's settings."""

import pytest

from ui import format as fmt
from ui.config import UiSettings


def test_durations_read_naturally() -> None:
    assert [fmt.duration(ms) for ms in (None, 0, 999, 1000, 6740)] == [
        "",
        "0 ms",
        "999 ms",
        "1.0 s",
        "6.7 s",
    ]


def test_unknown_values_fall_back_to_plain_words() -> None:
    assert fmt.label("retrieval", fmt.NODE_LABELS) == "Retrieval agent"
    assert fmt.label("research_agent", fmt.NODE_LABELS) == "research agent"


def test_the_plan_line_names_the_intent_and_agents() -> None:
    plan = {"intent": "document_question", "agents": ["retrieval"]}
    assert fmt.plan_line(plan) == "Intent: document question. Agents: Retrieval agent."
    assert fmt.plan_line({"intent": "conversation"}) == "Intent: conversation."


def test_the_search_line_has_collections_counts_rerank_and_time() -> None:
    search = {
        "collections": ["runbooks", "architecture"],
        "candidates": 40,
        "hits": 8,
        "rerank": "applied",
        "took_ms": 1200,
    }
    assert fmt.search_line(search) == (
        "runbooks, architecture: 40 matches, 8 kept (re-ranked), 1.2 s"
    )


def test_filters_are_listed_one_by_one() -> None:
    filters = {"department": "payments", "document_types": ["runbook", "incident"]}
    assert fmt.filters_line(filters) == "department = payments; document types in runbook, incident"
    assert fmt.filters_line({}) == "none"


def test_check_lines_name_the_stage_and_the_attempt() -> None:
    assert fmt.check_line({"stage": "input", "passed": True}) == "Input check: passed"
    assert fmt.check_line({"stage": "output", "passed": False, "attempt": 2}) == (
        "Citation check (attempt 2): failed"
    )


def test_model_calls_are_summed_and_listed() -> None:
    calls = [
        {"purpose": "supervisor", "provider": "anthropic", "model": "haiku", "input_tokens": 1000},
        {
            "purpose": "answer",
            "provider": "gemini",
            "model": "flash",
            "input_tokens": 1140,
            "output_tokens": 310,
            "duration_ms": 2500,
            "fallback": True,
        },
    ]
    assert fmt.tokens_line(calls) == "2 model calls, 2,140 tokens in, 310 out"
    assert fmt.tokens_line(calls[:1]) == "1 model call, 1,000 tokens in, 0 out"
    assert fmt.tokens_line([]) == "No model calls"
    assert fmt.call_rows(calls)[1] == {
        "purpose": "answer",
        "model": "gemini / flash",
        "tokens in": 1140,
        "tokens out": 310,
        "time": "2.5 s",
        "backup model": "yes",
    }


def test_a_source_line_has_the_number_document_and_section() -> None:
    citation = {"number": 1, "doc_id": "RB-001", "title": "Pool exhaustion", "section": "Fix"}
    assert fmt.source_line(citation) == "[1] **RB-001** Pool exhaustion, section: Fix"


# ---------------------------------------------------------------
# Settings
# ---------------------------------------------------------------


def test_settings_have_safe_defaults() -> None:
    assert UiSettings.from_env({}) == UiSettings()


def test_settings_are_read_from_the_environment() -> None:
    settings = UiSettings.from_env(
        {"API_URL": "http://api:8000/", "UI_CONNECT_TIMEOUT": "2", "UI_READ_TIMEOUT": "90"}
    )
    assert settings == UiSettings(api_url="http://api:8000", connect_timeout=2, read_timeout=90)


def test_keycloak_settings_are_read_from_the_environment() -> None:
    settings = UiSettings.from_env(
        {
            "KEYCLOAK_URL": "http://keycloak:8080/",
            "KEYCLOAK_REALM": "other",
            "KEYCLOAK_CLIENT_ID": "other-ui",
        }
    )
    assert settings.keycloak_url == "http://keycloak:8080"
    assert settings.keycloak_realm == "other"
    assert settings.keycloak_client_id == "other-ui"


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"API_URL": "api:8000"}, "API_URL must start with http"),
        ({"KEYCLOAK_URL": "keycloak:8080"}, "KEYCLOAK_URL must start with http"),
        ({"UI_READ_TIMEOUT": "soon"}, "UI_READ_TIMEOUT must be a number"),
        ({"UI_CONNECT_TIMEOUT": "0"}, "UI_CONNECT_TIMEOUT must be more than 0"),
    ],
)
def test_bad_settings_are_refused_with_a_clear_message(env: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        UiSettings.from_env(env)


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (1, "1 second"),
        (59, "59 seconds"),
        (90, "2 minutes"),
        (3600, "1 hour"),
        (3720, "1 hour 2 minutes"),
    ],
)
def test_waits_are_described_in_plain_words(seconds: int, text: str) -> None:
    assert fmt.describe_wait(seconds) == text


def test_tool_calls_are_described_for_the_panel() -> None:
    ok = {
        "tool": "service_owner",
        "arguments": {"service": "PayHub"},
        "ok": True,
        "items": 1,
        "duration_ms": 120,
    }
    failed = {"tool": "list_incidents", "arguments": {}, "ok": False, "error": "timeout"}
    assert fmt.tool_line(ok) == "service_owner(service=PayHub): 1 found, 120 ms"
    assert fmt.tool_line(failed) == "list_incidents(): failed (timeout)"
