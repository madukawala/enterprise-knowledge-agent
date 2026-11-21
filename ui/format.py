"""Short, readable text for the activity panel, built from event fields.

Kept apart from the Streamlit code so the wording can be tested directly.
"""

from collections.abc import Iterable, Mapping
from typing import Any

# Names a person would use, for the graph's node names.
NODE_LABELS = {
    "input_guard": "Input guard",
    "supervisor": "Supervisor",
    "retrieval": "Retrieval agent",
    "research": "Research agent",
    "response": "Response agent",
    "validator": "Validator",
    "memory": "Memory writer",
    "tools": "Tools agent",
}

INTENT_LABELS = {
    "document_question": "document question",
    "research": "research across documents",
    "enterprise_data": "enterprise data lookup",
    "conversation": "conversation",
    "out_of_scope": "out of scope",
}

RERANK_LABELS = {
    "applied": "re-ranked",
    "failed": "re-ranking failed, search order kept",
    "disabled": "re-ranking off",
    "not_needed": "no re-ranking needed",
}

ANSWER_KIND_LABELS = {
    "answered": "Answered from the documents",
    "conversation": "Conversation",
    "not_found": "Not found in the documents you can read",
    "unavailable": "Document search unavailable",
    "refused": "Question blocked",
    "out_of_scope": "Out of scope",
    "fallback": "Sources only, no written answer",
}

RESTART_LABELS = {
    "rewrite": "The validator rejected the draft, so the answer was written again.",
    "model_switch": "A model failed while writing, so the next model started again.",
}


def label(value: Any, labels: Mapping[str, str]) -> str:
    text = str(value)
    return labels.get(text, text.replace("_", " "))


def duration(ms: int | float | None) -> str:
    if ms is None:
        return ""
    if ms < 1000:
        return f"{int(ms)} ms"
    return f"{ms / 1000:.1f} s"


def plan_line(plan: Mapping[str, Any]) -> str:
    """'Intent: document question. Agents: retrieval.'"""
    parts = [f"Intent: {label(plan.get('intent'), INTENT_LABELS)}."]
    agents = [label(agent, NODE_LABELS) for agent in plan.get("agents", ())]
    if agents:
        parts.append(f"Agents: {', '.join(agents)}.")
    return " ".join(parts)


def search_line(search: Mapping[str, Any]) -> str:
    """'runbooks, architecture: 40 matches, 8 kept (re-ranked), 1.2 s'"""
    collections = ", ".join(search.get("collections", ())) or "no collections"
    parts = [
        f"{collections}: {search.get('candidates', 0)} matches",
        f"{search.get('hits', 0)} kept ({label(search.get('rerank'), RERANK_LABELS)})",
    ]
    took = duration(search.get("took_ms"))
    if took:
        parts.append(took)
    return ", ".join(parts)


def filters_line(filters: Mapping[str, Any]) -> str:
    """'department = payments, access level in public, internal'"""
    parts = []
    for name, value in filters.items():
        name_text = name.replace("_", " ")
        if isinstance(value, list | tuple):
            parts.append(f"{name_text} in {', '.join(map(str, value))}")
        else:
            parts.append(f"{name_text} = {value}")
    return "; ".join(parts) or "none"


def check_line(check: Mapping[str, Any]) -> str:
    stage = check.get("stage")
    name = "Input check" if stage == "input" else "Citation check"
    attempt = int(check.get("attempt", 1))
    if stage == "output" and attempt > 1:
        name += f" (attempt {attempt})"
    return f"{name}: {'passed' if check.get('passed') else 'failed'}"


def tokens_line(calls: Iterable[Mapping[str, Any]]) -> str:
    """'3 model calls, 2,140 tokens in, 310 out'"""
    calls = list(calls)
    if not calls:
        return "No model calls"
    tokens_in = sum(int(call.get("input_tokens", 0)) for call in calls)
    tokens_out = sum(int(call.get("output_tokens", 0)) for call in calls)
    noun = "call" if len(calls) == 1 else "calls"
    return f"{len(calls)} model {noun}, {tokens_in:,} tokens in, {tokens_out:,} out"


def call_rows(calls: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One table row per model call."""
    return [
        {
            "purpose": call.get("purpose", ""),
            "model": f"{call.get('provider', '')} / {call.get('model', '')}",
            "tokens in": call.get("input_tokens", 0),
            "tokens out": call.get("output_tokens", 0),
            "time": duration(call.get("duration_ms")),
            "backup model": "yes" if call.get("fallback") else "",
        }
        for call in calls
    ]


def source_line(citation: Mapping[str, Any]) -> str:
    """'[1] RB-001 PayHub connection pool exhausted, section: Steps to fix'"""
    return (
        f"[{citation.get('number')}] **{citation.get('doc_id')}** {citation.get('title')}"
        f", section: {citation.get('section')}"
    )


def describe_wait(seconds: int) -> str:
    """'45 seconds', '3 minutes', '5 hours 12 minutes': for the user, not exact."""
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = -(-seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, minutes = divmod(minutes, 60)
    text = f"{hours} hour{'s' if hours != 1 else ''}"
    return text + (f" {minutes} minute{'s' if minutes != 1 else ''}" if minutes else "")


def research_line(research: Mapping[str, Any]) -> str:
    """'Plan written by the model and run in the sandbox. 6 batches, depth 2, 14 sources.'"""
    source = (
        "Plan written by the model and run in the sandbox"
        if research.get("plan_source") == "sandbox"
        else "Fixed plan: the last twelve months by quarter"
    )
    batches = research.get("batches") or []
    text = (
        f"{source}. {len(batches)} batches, depth {research.get('max_depth', 1)}, "
        f"{research.get('sources', 0)} sources, {research.get('tokens_used', 0):,} tokens."
    )
    if research.get("stopped_by_budget"):
        text += " Stopped at the token budget."
    return text


def batch_line(batch: Mapping[str, Any]) -> str:
    """'  2026 Q1: 8 sources, 3 findings (split)'"""
    indent = "\u00a0\u00a0" * (int(batch.get("depth", 1)) - 1)
    if batch.get("error"):
        return f"{indent}{batch.get('label')}: failed ({batch.get('error')})"
    text = f"{indent}{batch.get('label')}: {batch.get('hits', 0)} sources"
    if batch.get("split"):
        return text + ", split into smaller batches"
    return text + f", {batch.get('findings', 0)} findings"


def tool_line(call: Mapping[str, Any]) -> str:
    """'service_owner(service=PayHub): 1 found, 120 ms'"""
    args = ", ".join(f"{k}={v}" for k, v in (call.get("arguments") or {}).items())
    head = f"{call.get('tool')}({args})"
    if not call.get("ok"):
        return f"{head}: failed ({call.get('error') or 'no reason given'})"
    return f"{head}: {call.get('items', 0)} found, {duration(call.get('duration_ms'))}"
