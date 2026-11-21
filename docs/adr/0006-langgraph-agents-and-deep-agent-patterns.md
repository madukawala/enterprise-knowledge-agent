# ADR-0006: Plain LangGraph `StateGraph`, research sub-graph with deep-agent patterns

- **Status:** Accepted
- **Date:** 2026-11-20

## Context

The assignment asks for LangGraph orchestration with specialised agents (supervisor, retrieval, research, response). Reviewers check how state, routing and failures are handled ("butterfly effect"). Options were plain LangGraph, the prebuilt supervisor helper, or the `deepagents` package.

## Decision

- Build the graph directly with **`StateGraph`**: input guard → supervisor → retrieval / research / tool agents → response → validator → memory writer, as in [03-agent-graph](../architecture/03-agent-graph.drawio.svg).
- **Supervisor:** classifies intent, writes a short plan (task decomposition) and routes with conditional edges. Agents return to the supervisor, which decides whether the answer is complete.
- **Research agent:** a **sub-graph** that uses deep-agent patterns built by hand: a planning list, sub-agents per batch (LangGraph `Send` for parallel fan-out), and a scratchpad in state. See [ADR-0011](0011-rlm-python-search-plans-in-sandbox.md).
- **Human approval:** a node that calls `interrupt()` before risky or costly steps, and resumes on approve or reject.
- **Shared typed state:** a `TypedDict` with reducers, and Pydantic models for plans, chunks, tool results and citations.
- **Failure containment:** each node catches its own errors and writes them to `errors[agent]` instead of raising. The supervisor retries, skips or answers with what it has, and the answer states what was missing.
- **Checkpointing:** the Postgres checkpointer stores every step, which gives multi-turn memory, interrupt and resume ([ADR-0008](0008-memory-design.md)).

## Changes during implementation (`feature/rlm-research`)

- The research agent is one graph node, and its batch sub-agents run in parallel inside it with `asyncio.gather`, instead of a separate sub-graph with `Send`. The recursion depth differs per run, and keeping it inside one node keeps the graph, its diagram and the streamed events simple. The sub-agents' searches and model calls still show in the LangSmith trace, and the `research` event shows every batch in the activity panel.

## Human approval (`feature/hitl-feedback`)

- An **approval node** (agents/nodes/approval.py) stands in front of the research agent. It calls LangGraph's `interrupt()`: the run stops, the checkpointer saves it, and the stream ends with an `approval_required` event. `POST /approvals/{thread_id}` resumes it with `Command(resume=...)`. Approved: research runs. Rejected: one normal search instead, and the answer says so.
- The person who asked is the one who approves: the cost being approved is their own quota and time. Approving needs the same permission as the research itself, checked again in the API.
- The node wrapper lets LangGraph's control-flow exceptions (`GraphBubbleUp`) through, so an interrupt is never mistaken for a failure.

## Consequences

- **Positive:** we control where role checks run and which events stream to the activity panel; every node can be explained on its own.
- **Positive:** the graph is drawn from code (`make diagrams`), so the diagram always matches the implementation.
- **Negative:** more code than a prebuilt agent. It is kept small: one module per node, with a docstring saying what it does and why.

## Update during implementation

Built first in `feature/langgraph-agents` ([agents/](../../agents/README.md)): the input guard, supervisor, retrieval agent, response agent and validator. The research agent (`feature/rlm-research`), the memory writer (`feature/memory`), the tools agent (`feature/mcp-server`) and the approval step (`feature/hitl-feedback`) joined the same graph later, each routed by the supervisor in code.

- **The supervisor uses the model once per question.** One structured call returns the intent and a short plan. Code maps the intent to agents (`AGENTS_FOR_INTENT`), so the model can never route to an agent that does not exist. After each agent returns, plain code decides what runs next: retry a failed search once, search again without filters if nothing matched, or go on to the answer. This keeps a question at three model calls and makes routing easy to test. A model-driven loop was the alternative, but it costs a call per decision on a free quota and is harder to bound.
- **Before the research and tool agents existed,** research questions used a wider search (20 chunks) and enterprise-data questions searched the documents. That wider search is still the fallback when the research agent fails completely.
- **Containment is in one place.** Every node runs through the same wrapper ([agents/nodes/base.py](../../agents/nodes/base.py)): an unexpected exception is logged with its traceback, counted, and replaced by that node's safe fallback (`on_error`). Expected failures (the model or search being down) are handled inside the node with a clear message. The input guard fails closed. A recursion limit of 30 steps stops a routing loop.
- **Fixed answers where a model adds nothing.** Refusals, out of scope, nothing found and search unavailable are written in code, not generated.
- **The validator is code, not a second model.** It checks that every citation number points to a source retrieved in this run, rewrites once, then falls back to listing the sources.
- **Reducers.** Lists collected during a question (errors, steps, limits, model calls) append rather than overwrite, so parallel research sub-agents can use them later. Sending `None` clears them at the start of a question.
- **Checkpointing** uses LangGraph's in-memory saver for now, with an explicit list of types it may rebuild. `feature/memory` swaps in Postgres.
- **The diagram is generated from code** into [03-agent-graph.generated.md](../architecture/03-agent-graph.generated.md), and a test fails if it goes stale.

## Alternatives considered

| Option | Why not |
|---|---|
| `deepagents` package for research | Hides the loop and its tool calls; harder to enforce role checks and stream fine-grained events |
| Prebuilt supervisor helper | Less control over routing, state shape and failure handling |
| A single ReAct agent with all tools | No specialisation; one failure affects the whole run; weak on the LangGraph criterion |

## Related

[03-agent-graph](../architecture/03-agent-graph.drawio.svg) · [05-rlm-research](../architecture/05-rlm-research.md) · graph tests in [TESTING.md](../TESTING.md)
