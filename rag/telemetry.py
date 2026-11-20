"""Prometheus metrics for document search.

The metrics are created on a registry passed in by the caller. The API will
pass the registry it serves on /metrics when it starts using retrieval
(agents branch). Tests and the command-line tools use their own registry,
so nothing is shared between them.

Labels only ever hold values from small fixed sets (a collection name, an
outcome), never a question or a document ID, so the number of time series
stays small.
"""

from prometheus_client import CollectorRegistry, Counter, Histogram


class RetrievalMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.searches = Counter(
            "retrieval_searches_total",
            "Searches, by outcome: ok, partial (some collections failed) or failed.",
            ["outcome"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "retrieval_search_duration_seconds",
            "Time for one whole search: embed, query every collection, merge, re-rank.",
            buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 16),
            registry=self.registry,
        )
        self.collection_errors = Counter(
            "retrieval_collection_errors_total",
            "Collections that could not be searched, by reason (timeout or error).",
            ["collection", "reason"],
            registry=self.registry,
        )
        self.rerank = Counter(
            "retrieval_rerank_total",
            "Re-ranker use: applied, failed (hybrid order kept), disabled or not_needed.",
            ["status"],
            registry=self.registry,
        )
        self.dropped_records = Counter(
            "retrieval_dropped_records_total",
            "Matches thrown away: invalid_metadata, or access_level_mismatch, which "
            "should never happen and points to a filter bug.",
            ["reason"],
            registry=self.registry,
        )
