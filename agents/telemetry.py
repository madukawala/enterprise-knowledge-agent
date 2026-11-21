"""Prometheus metrics for the agents and the language models.

Like the search metrics, they are created on a registry the caller passes
in, so tests can build many agent services in one process. Labels only
take a few fixed values (node names, providers, answer kinds), never user
input, so the number of series stays small.
"""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from agents.state import AgentName, AnswerKind, ErrorKind


class AgentMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        registry = registry or CollectorRegistry()
        self.runs = Counter(
            "agent_runs_total",
            "Questions answered by the agent graph, by the kind of answer given.",
            ["answer_kind"],
            registry=registry,
        )
        self.run_duration = Histogram(
            "agent_run_duration_seconds",
            "Time from question to validated answer.",
            buckets=(0.5, 1, 2, 4, 8, 15, 30, 60),
            registry=registry,
        )
        self.node_duration = Histogram(
            "agent_node_duration_seconds",
            "Time spent in each node of the agent graph.",
            ["node"],
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30),
            registry=registry,
        )
        self.node_errors = Counter(
            "agent_node_errors_total",
            "Node failures that were contained (written to the state, not raised).",
            ["node", "kind"],
            registry=registry,
        )
        self.validations = Counter(
            "agent_answer_validations_total",
            "Validator results: passed, regenerate (first failure) or fallback.",
            ["result"],
            registry=registry,
        )
        self.llm_calls = Counter(
            "llm_calls_total",
            "Language model calls by provider and outcome.",
            ["provider", "purpose", "outcome"],
            registry=registry,
        )
        self.llm_tokens = Counter(
            "llm_tokens_total",
            "Tokens sent to and received from each provider.",
            ["provider", "direction"],
            registry=registry,
        )
        self.llm_breaker_state = Gauge(
            "llm_breaker_state",
            "Circuit breaker per provider: 0 closed (in use), 1 half-open (probing), "
            "2 open (skipped).",
            ["provider"],
            registry=registry,
        )
        self.llm_fallbacks = Counter(
            "llm_fallbacks_total",
            "Calls answered by a later provider because an earlier one failed.",
            ["provider"],
            registry=registry,
        )
        # Guardrails (security/guardrails): what was blocked, dropped or fixed.
        self.guardrail_blocks = Counter(
            "guardrail_blocks_total",
            "Questions refused, retrieved chunks dropped and answers rejected by a guardrail, "
            "by where (input, retrieved, output) and why.",
            ["guard", "category"],
            registry=registry,
        )
        self.tool_runs = Counter(
            "tool_runs_total",
            "Tool runs by tool and outcome (ok, rejected, error, timeout, crashed, unavailable).",
            ["tool", "status"],
            registry=registry,
        )
        self.feedback = Counter(
            "answer_feedback_total",
            "Thumbs up and down on answers.",
            ["score"],
            registry=registry,
        )
        self.pii_masked = Counter(
            "pii_masked_total",
            "Personal or secret values masked in answers, by kind.",
            ["kind"],
            registry=registry,
        )

    def node_error(self, node: AgentName, kind: ErrorKind) -> None:
        self.node_errors.labels(node=node.value, kind=kind.value).inc()

    def run_finished(self, kind: AnswerKind, seconds: float) -> None:
        self.runs.labels(answer_kind=kind.value).inc()
        self.run_duration.observe(seconds)
