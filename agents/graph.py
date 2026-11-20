"""The agent graph: which node runs after which.

    START -> input_guard -> supervisor <-> retrieval
                 |              |
                 |  (blocked)   v
                 +--------> response -> validator -> END
                                ^           |
                                +-----------+  (rewrite once)

- The input guard sends a blocked question straight to the response agent,
  which answers with the refusal. No model is called.
- The supervisor sends the work to the agents in its plan; each one returns
  to the supervisor, which decides what runs next (`next_agent`).
- The validator sends a rejected draft back to the response agent once.

The research agent, the tool agent, human approval and the memory writer
join this graph in their own branches (see the design diagram,
docs/architecture/03-agent-graph.drawio.svg). `make diagrams` draws the
graph from this code into docs/architecture/03-agent-graph.generated.md.
"""

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents.nodes import (
    InputGuard,
    NodeContext,
    ResponseAgent,
    RetrievalAgent,
    Supervisor,
    Validator,
)
from agents.state import AgentName, AgentState

GRAPH_NAME = "helix_agent"

# Routes the supervisor may choose. Anything else goes to the answer.
_SUPERVISOR_ROUTES = (AgentName.RETRIEVAL.value, AgentName.RESPONSE.value)


def after_input_guard(state: AgentState) -> str:
    if state.get("blocked_reason"):
        return AgentName.RESPONSE.value
    return AgentName.SUPERVISOR.value


def after_supervisor(state: AgentState) -> str:
    target = state.get("next_agent")
    return (
        target.value if target and target.value in _SUPERVISOR_ROUTES else AgentName.RESPONSE.value
    )


def after_validator(state: AgentState) -> str:
    if state.get("answer") is None and state.get("next_agent") is AgentName.RESPONSE:
        return AgentName.RESPONSE.value
    return END


def build_graph(
    context: NodeContext, *, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
    graph = StateGraph(AgentState)
    for node in (InputGuard, Supervisor, RetrievalAgent, ResponseAgent, Validator):
        graph.add_node(node.name.value, node(context))

    graph.add_edge(START, AgentName.INPUT_GUARD.value)
    graph.add_conditional_edges(
        AgentName.INPUT_GUARD.value,
        after_input_guard,
        [AgentName.SUPERVISOR.value, AgentName.RESPONSE.value],
    )
    graph.add_conditional_edges(
        AgentName.SUPERVISOR.value, after_supervisor, list(_SUPERVISOR_ROUTES)
    )
    graph.add_edge(AgentName.RETRIEVAL.value, AgentName.SUPERVISOR.value)
    graph.add_edge(AgentName.RESPONSE.value, AgentName.VALIDATOR.value)
    graph.add_conditional_edges(
        AgentName.VALIDATOR.value, after_validator, [AgentName.RESPONSE.value, END]
    )
    return graph.compile(checkpointer=checkpointer, name=GRAPH_NAME)
