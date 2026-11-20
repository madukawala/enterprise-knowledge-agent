"""Prometheus metrics for the chat stream.

Per-request numbers (count, errors, latency) already come from the
instrumentator in api/main.py. A stream is one long request, so these add
what that cannot see: how streams end, how many are open, and how long
the user waits for the first word of the answer.
"""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class ChatMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        registry = registry or CollectorRegistry()
        self.streams = Counter(
            "chat_streams_total",
            "Chat streams by how they ended: completed, cancelled (client left), timeout or error.",
            ["outcome"],
            registry=registry,
        )
        self.active = Gauge(
            "chat_streams_active",
            "Chat streams open right now.",
            registry=registry,
        )
        self.events = Counter(
            "chat_stream_events_total",
            "Events sent to clients, by type.",
            ["type"],
            registry=registry,
        )
        self.first_token = Histogram(
            "chat_first_token_seconds",
            "Time from the question to the first token of the answer.",
            buckets=(0.5, 1, 2, 4, 8, 15, 30, 60),
            registry=registry,
        )
