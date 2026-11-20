"""Streamlit drawing code: one answer in the chat, and the agent activity panel.

Each function draws into whatever Streamlit container is active, so the
same code draws a finished answer from history and redraws a live one
while it streams.
"""

from typing import Any

import streamlit as st

from ui import format as fmt
from ui.conversation import Step, Turn, TurnStatus

_STEP_ICONS = {
    "running": ":material/progress_activity:",
    "ok": ":material/check_circle:",
    "error": ":material/error:",
    "stopped": ":material/stop_circle:",
}

_STATUS_BADGES: dict[TurnStatus, tuple[str, Any]] = {
    TurnStatus.STREAMING: ("Working", "blue"),
    TurnStatus.DONE: ("Done", "green"),
    TurnStatus.FAILED: ("Failed", "red"),
    TurnStatus.STOPPED: ("Stopped", "gray"),
}

# Shown at the end of the text while the answer is still being written.
_CURSOR = " ▌"


# ---------------------------------------------------------------
# The answer in the chat window
# ---------------------------------------------------------------


def answer(turn: Turn) -> None:
    if turn.status is TurnStatus.STREAMING:
        if turn.preview:
            st.markdown(turn.preview + _CURSOR)
        else:
            st.caption(_waiting_text(turn))
        return

    if turn.final is not None:
        _final_answer(turn.final)
    elif turn.preview:
        # Stopped or failed half-way: keep what was written, marked as unfinished.
        st.markdown(turn.preview)
    if turn.status is TurnStatus.STOPPED:
        st.caption("Stopped before the answer was finished.")
    if turn.error:
        reference = f" Reference: `{turn.request_id}`" if turn.request_id else ""
        st.error(turn.error + reference, icon=":material/error:")


def _waiting_text(turn: Turn) -> str:
    node = turn.active_node
    if node is None:
        return "Sending your question…"
    return f"{fmt.label(node, fmt.NODE_LABELS)} is working…"


def _final_answer(final: dict[str, Any]) -> None:
    kind = str(final.get("kind", "answered"))
    if kind not in {"answered", "conversation"}:
        st.badge(fmt.label(kind, fmt.ANSWER_KIND_LABELS), color="orange")
    st.markdown(str(final.get("text", "")))

    citations = final.get("citations") or []
    if citations:
        st.markdown("**Sources**")
        st.markdown("\n".join(f"- {fmt.source_line(c)}" for c in citations))
    limits = final.get("limits") or []
    if limits:
        st.caption("Limits: " + " ".join(limits))
    steps = final.get("steps") or []
    if steps:
        with st.expander("How I got this answer"):
            st.markdown("\n".join(f"- {step}" for step in steps))


# ---------------------------------------------------------------
# The agent activity panel
# ---------------------------------------------------------------


def activity(turn: Turn) -> None:
    text, color = _STATUS_BADGES[turn.status]
    st.badge(text, color=color)
    if turn.run_id:
        traced = "traced in LangSmith" if turn.traced else "not traced"
        st.caption(f"Run `{turn.run_id}` ({traced})")

    if turn.plan is not None:
        st.markdown("**Agent state**")
        st.markdown(fmt.plan_line(turn.plan))
        plan_steps = turn.plan.get("plan") or []
        if plan_steps:
            st.markdown("\n".join(f"{n}. {s}" for n, s in enumerate(plan_steps, start=1)))

    if turn.steps:
        st.markdown("**Steps**")
        for step in turn.steps:
            _step(step)

    for search in turn.searches:
        _search(search)

    if turn.checks:
        st.markdown("**Validation**")
        for check in turn.checks:
            _check(check)

    for warning in turn.warnings:
        st.warning(warning, icon=":material/warning:")
    for reason in turn.restarts:
        st.info(fmt.label(reason, fmt.RESTART_LABELS), icon=":material/replay:")

    if turn.other:
        st.markdown("**Other events**")
        for event in turn.other:
            st.caption(f"`{event.get('type')}`")

    if turn.final is not None:
        _response(turn)

    if turn.events:
        with st.expander(f"Raw events ({len(turn.events)}, plus {turn.tokens} tokens)"):
            st.json(turn.events, expanded=False)


def _step(step: Step) -> None:
    icon = _STEP_ICONS.get(step.status, ":material/radio_button_unchecked:")
    took = fmt.duration(step.duration_ms)
    line = f"{icon} {fmt.label(step.node, fmt.NODE_LABELS)}"
    if took:
        line += f" · {took}"
    if step.status == "error":
        line += " · failed, the run carried on without it"
    st.markdown(line)
    if step.summary:
        st.caption(" ".join(step.summary))


def _search(search: dict[str, Any]) -> None:
    st.markdown("**Retrieval**")
    st.markdown(f"Query: _{search.get('query', '')}_")
    st.caption(fmt.search_line(search))
    st.caption("Filters: " + fmt.filters_line(search.get("filters") or {}))
    failed = search.get("failed_collections") or []
    if failed:
        st.warning(f"No answer from: {', '.join(failed)}", icon=":material/warning:")
    if search.get("relaxed"):
        st.caption("Second try, with the optional filters dropped.")


def _check(check: dict[str, Any]) -> None:
    icon = ":material/check_circle:" if check.get("passed") else ":material/cancel:"
    st.markdown(f"{icon} {fmt.check_line(check)}")
    issues = check.get("issues") or []
    if issues:
        st.caption(" ".join(issues))


def _response(turn: Turn) -> None:
    final = turn.final or {}
    st.markdown("**Response**")
    written_by = final.get("written_by")
    parts = [fmt.label(final.get("kind"), fmt.ANSWER_KIND_LABELS)]
    if written_by:
        parts.append(f"written by {written_by}")
    parts.append(f"in {fmt.duration(final.get('duration_ms'))}")
    st.markdown(", ".join(parts))
    calls = final.get("llm_calls") or []
    st.caption(fmt.tokens_line(calls))
    if calls:
        st.dataframe(fmt.call_rows(calls), hide_index=True)
