"""Turns LangGraph's raw stream into the events in agents/events.py.

The graph is run with three stream modes at once:

- `tasks`: a message when each node starts and when it finishes, with the
  state update it returned. Node timings, `node_start` / `node_end` and
  the events about each node's work (search, plan, checks) come from here.
- `messages`: the language model's tokens as they arrive, labelled with the
  node that asked for them. Only the response agent's tokens are passed on;
  the supervisor's and the search planner's output is structured data,
  not text for the user.
- `values`: the whole state after each step. The last one holds the answer.

`RunRecorder` keeps what it needs between messages (start times, the
current answer attempt) and remembers the final state, so the service can
build the `response_end` event and the `AgentRun` from it.
"""

import time
from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import AIMessageChunk
from langgraph.types import StreamMode

from agents.events import (
    AgentEvent,
    AgentStateEvent,
    DegradedEvent,
    NodeEndEvent,
    NodeStartEvent,
    ResponseEndEvent,
    ResponseStartEvent,
    RetrievalEvent,
    SourceRef,
    TokenEvent,
    ValidationEvent,
)
from agents.nodes.response import NOT_FOUND_REPLY, fallback_answer
from agents.state import AgentError, AgentName, Answer, LLMCall, Plan, SearchSummary, Step

STREAM_MODES: list[StreamMode] = ["tasks", "messages", "values"]


class RunRecorder:
    def __init__(self, *, run_id: str, thread_id: str, traced: bool = False) -> None:
        self.run_id = run_id
        self.thread_id = thread_id
        self.traced = traced
        # The nodes in the order they finished, e.g. input_guard, supervisor, ...
        self.nodes: list[str] = []
        # The last full state seen (`values` mode).
        self.final: dict[str, Any] = {}
        self._started: dict[str, float] = {}
        self._attempt = 0
        self._model_call: str | None = None
        self._held = ""
        self._holding = True

    def accepted(self) -> AgentStateEvent:
        """The first event of every run."""
        return AgentStateEvent(
            status="accepted", run_id=self.run_id, thread_id=self.thread_id, traced=self.traced
        )

    def translate(self, mode: str, chunk: Any) -> list[AgentEvent]:
        """The events for one item of the raw stream (often none)."""
        if mode == "values":
            self.final = dict(chunk)
            return []
        if mode == "tasks":
            return self._task(chunk)
        if mode == "messages":
            message, metadata = chunk
            return self._token(message, metadata)
        return []

    def response_end(self, duration_ms: int) -> ResponseEndEvent:
        answer = self.answer
        return ResponseEndEvent(
            kind=answer.kind,
            text=answer.text,
            markdown=answer.to_markdown(),
            citations=tuple(SourceRef.of(c.number, c.citation) for c in answer.citations),
            steps=answer.steps,
            limits=answer.limits,
            written_by=answer.written_by,
            llm_calls=tuple(self.final.get("llm_calls", [])),
            duration_ms=duration_ms,
        )

    @property
    def answer(self) -> Answer:
        # The validator always sets an answer. The fallback only matters if
        # the run stopped early (the recursion limit, see AgentService).
        answer = self.final.get("answer")
        return answer if isinstance(answer, Answer) else fallback_answer(self.final.get("hits", []))

    # ---------------------------------------------------------------
    # Nodes starting and finishing
    # ---------------------------------------------------------------

    def _task(self, task: Mapping[str, Any]) -> list[AgentEvent]:
        try:
            node = AgentName(task["name"])
        except ValueError:
            return []  # not one of our nodes
        if "result" not in task:
            return self._node_started(task["id"], node)
        return self._node_finished(task, node)

    def _node_started(self, task_id: str, node: AgentName) -> list[AgentEvent]:
        self._started[task_id] = time.perf_counter()
        events: list[AgentEvent] = [NodeStartEvent(node=node)]
        if node is AgentName.RESPONSE:
            self._attempt += 1
            self._new_draft()
            first = self._attempt == 1
            events.append(
                ResponseStartEvent(attempt=self._attempt, reason="first" if first else "rewrite")
            )
        return events

    def _node_finished(self, task: Mapping[str, Any], node: AgentName) -> list[AgentEvent]:
        started = self._started.pop(task["id"], time.perf_counter())
        update: Mapping[str, Any] = task.get("result") or {}
        errors: Mapping[str, AgentError | None] = update.get("errors") or {}
        failed = task.get("error") is not None or errors.get(node.value) is not None
        self.nodes.append(node.value)

        events = self._work_done(node, update, errors)
        steps: Sequence[Step] = update.get("steps") or []
        events.append(
            NodeEndEvent(
                node=node,
                duration_ms=round((time.perf_counter() - started) * 1000),
                status="error" if failed else "ok",
                summary=tuple(step.summary for step in steps),
            )
        )
        return events

    def _work_done(
        self, node: AgentName, update: Mapping[str, Any], errors: Mapping[str, AgentError | None]
    ) -> list[AgentEvent]:
        """What the node did, read from the state update it returned."""
        events: list[AgentEvent] = []
        if node is AgentName.INPUT_GUARD:
            reason = update.get("blocked_reason")
            events.append(
                ValidationEvent(
                    stage="input", passed=not reason, issues=(reason,) if reason else ()
                )
            )
        plan = update.get("plan")
        if node is AgentName.SUPERVISOR and isinstance(plan, Plan):
            events.append(
                AgentStateEvent(
                    status="planned",
                    run_id=self.run_id,
                    thread_id=self.thread_id,
                    traced=self.traced,
                    intent=plan.intent,
                    plan=plan.steps,
                    agents=plan.agents,
                )
            )
        search = update.get("search")
        if isinstance(search, SearchSummary):
            events.append(_retrieval_event(search))
        if (error := errors.get(AgentName.RETRIEVAL.value)) is not None:
            events.append(DegradedEvent(dependency="documents", message=error.message))
        if (error := errors.get(AgentName.RESPONSE.value)) is not None:
            events.append(DegradedEvent(dependency="language_model", message=error.message))
        calls: Sequence[LLMCall] = update.get("llm_calls") or []
        events.extend(
            DegradedEvent(
                dependency="language_model",
                message=f"The main model did not answer, so {call.provider} / {call.model} "
                f"was used for the {call.purpose.replace('_', ' ')}.",
            )
            for call in calls
            if call.fallback
        )
        validation = update.get("validation")
        if node is AgentName.VALIDATOR and validation is not None:
            events.append(
                ValidationEvent(
                    stage="output",
                    passed=validation.passed,
                    issues=validation.issues,
                    attempt=validation.attempt,
                )
            )
        return events

    # ---------------------------------------------------------------
    # Tokens of the answer
    # ---------------------------------------------------------------

    def _new_draft(self) -> None:
        self._model_call = None
        self._held = ""
        self._holding = True

    def _token(self, message: Any, metadata: Mapping[str, Any]) -> list[AgentEvent]:
        if not isinstance(message, AIMessageChunk):
            return []  # whole messages written to the state, not tokens
        if metadata.get("langgraph_node") != AgentName.RESPONSE.value:
            return []
        events: list[AgentEvent] = []
        # Every model call has its own ID. A new ID inside the same attempt
        # means the first model failed half-way and the next one started.
        if self._model_call is not None and message.id != self._model_call:
            self._new_draft()
            events.append(ResponseStartEvent(attempt=self._attempt, reason="model_switch"))
        self._model_call = message.id
        if text := self._release(message.text):
            events.append(TokenEvent(text=text))
        return events

    def _release(self, text: str) -> str:
        """Hold the first tokens back while the reply could still be NOT_FOUND.

        The model answers NOT_FOUND when the sources do not cover the
        question, and the response agent swaps it for a friendly message.
        Without this, the user would see "NOT_FOUND" flash up first.
        """
        if not self._holding:
            return text
        self._held += text
        start = self._held.strip().rstrip(".").upper()
        if NOT_FOUND_REPLY.startswith(start):
            return ""
        self._holding = False
        released, self._held = self._held, ""
        return released


def _retrieval_event(search: SearchSummary) -> RetrievalEvent:
    return RetrievalEvent(
        query=search.query,
        collections=search.collections,
        filters=search.filters.model_dump(mode="json", exclude_defaults=True),
        failed_collections=search.failed_collections,
        candidates=search.candidates,
        hits=search.hits,
        rerank=search.rerank,
        alpha=search.alpha,
        took_ms=search.took_ms,
        relaxed=search.relaxed,
    )
