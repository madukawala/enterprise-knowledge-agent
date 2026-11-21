"""Application settings.

Settings come from environment variables, with the `.env` file as a fallback.
Every variable is listed with an example value in `.env.example`.

Use `get_settings()` instead of creating `Settings()` directly, so the
environment is read once per process.
"""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    PRODUCTION = "production"


class LogFormat(StrEnum):
    JSON = "json"
    """One JSON object per line. Used in Docker and CI."""

    CONSOLE = "console"
    """Coloured, human-friendly lines for local development."""


# The `.env` names of each provider's key and model, for error messages.
_LLM_VARIABLES = {
    "anthropic": ("ANTHROPIC_API_KEY", "CLAUDE_MODEL"),
    "gemini": ("GEMINI_API_KEY", "GEMINI_MODEL"),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # A settings error must never print the values it was given: they
        # include API keys and passwords (the message would end up in the
        # terminal or the container logs).
        hide_input_in_errors=True,
    )

    app_name: str = "enterprise-knowledge-agent"
    app_env: Environment = Environment.LOCAL

    log_level: LogLevel = "INFO"
    log_format: LogFormat = LogFormat.JSON

    # Docker sets API_HOST=0.0.0.0 inside the container. Outside Docker the
    # API only listens on the local machine.
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)

    # Connection strings for the backing services. Docker Compose sets both.
    # When one is empty, `/readyz` skips that check, so the API can still be
    # run on its own with `make run-api`. Stored as SecretStr because the
    # URLs contain passwords; they never appear in logs or error messages.
    postgres_url: SecretStr | None = None
    redis_url: SecretStr | None = None

    # How long each readiness check may take before it counts as failed.
    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    # Request metrics for Prometheus on /metrics.
    metrics_enabled: bool = True

    # --- Pinecone and the document index (ADR-0003, ADR-0004, ADR-0005) ---
    # Needed by `make ingest` and by document search. Without a key,
    # `make ingest-check` still reads and chunks the documents.
    pinecone_api_key: SecretStr | None = None
    pinecone_index_name: str = Field(
        default="helix-knowledge", min_length=1, max_length=45, pattern=r"^[a-z0-9-]+$"
    )
    # The free Starter plan only offers serverless indexes on aws/us-east-1.
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"
    # Pinecone-hosted embedding model. The dimension must match the model;
    # change both together, then delete the index and run `make ingest` again.
    embedding_model: str = "multilingual-e5-large"
    embedding_dimension: int = Field(default=1024, gt=0)

    # Where ingestion reads the documents from, and where it saves the fitted
    # BM25 parameters and the document catalog (git-ignored, rebuilt by ingest).
    sample_docs_dir: Path = Path("data/sample_docs")
    index_artifacts_dir: Path = Path("data/index")

    # --- Hybrid search (ADR-0003, ADR-0010) ---
    # score = alpha x dense score + (1 - alpha) x BM25 score.
    # 1.0 is meaning only. It must stay above 0: at 0 the dense query vector
    # would be all zeros, which Pinecone does not accept.
    hybrid_alpha: float = Field(default=0.5, gt=0, le=1)
    # How many chunks a search returns, after merging and re-ranking.
    retrieval_top_k: int = Field(default=8, ge=1, le=20)
    # How many matches to ask each collection for before merging.
    retrieval_candidates_per_collection: int = Field(default=20, ge=1, le=100)
    # Stops one long document from filling every result slot.
    retrieval_max_chunks_per_document: int = Field(default=3, ge=1, le=20)
    # Time limit for each call to Pinecone during a search (embed, query, re-rank).
    retrieval_timeout_seconds: float = Field(default=8.0, gt=0, le=60)
    # Pinecone-hosted re-ranker. If it fails or the free quota runs out, the
    # search keeps the hybrid order instead of failing.
    rerank_enabled: bool = True
    rerank_model: str = "bge-reranker-v2-m3"
    # How many merged candidates are sent to the re-ranker.
    rerank_candidates: int = Field(default=30, ge=1, le=100)

    # --- Language models (ADR-0017) ---
    # Claude answers first. If it fails, times out or returns something
    # unusable, Gemini takes over. One key is enough to run the agents;
    # with both, an outage at one company does not stop the assistant.
    #
    # Model names have no default on purpose. Models are renamed and retired
    # often, and a name kept in code goes stale without anyone noticing. So
    # each provider with a key must also have its model set in `.env`
    # (`.env.example` lists the recommended ones); see
    # `_every_provider_has_a_model` below.
    anthropic_api_key: SecretStr | None = None
    claude_model: str | None = None
    # Claude is paid per token, so it is set up to spend as little as
    # possible (agents/llm.py): no thinking, low effort, and a cap on the
    # length of every reply. The longest reply the agents need is an
    # answer of a few paragraphs, well under 2,000 tokens.
    anthropic_max_tokens: int = Field(default=2000, ge=256, le=16000)
    # How much work Claude puts into each reply. "low" is the cheapest and
    # is enough for routing, search plans and answers from given sources.
    anthropic_effort: Literal["low", "medium", "high"] = "low"
    gemini_api_key: SecretStr | None = None
    gemini_model: str | None = None
    # Gemini only. Low, so routing and answers stay close to the sources.
    # Claude is left at its own default, because the current Claude models
    # refuse any other temperature.
    llm_temperature: float = Field(default=0.2, ge=0, le=1)
    # Time limit for one model call, and retries inside one provider before
    # moving on to the next one.
    llm_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    llm_max_retries: int = Field(default=1, ge=0, le=5)
    # Circuit breaker per provider (agents/breaker.py): this many failures
    # within the window, or one rate-limit reply, and the provider is skipped
    # for `llm_breaker_open_seconds`, then probed with one call.
    llm_breaker_failures: int = Field(default=5, ge=1, le=100)
    llm_breaker_window_seconds: float = Field(default=60.0, gt=0, le=3600)
    llm_breaker_open_seconds: float = Field(default=30.0, gt=0, le=3600)

    # --- Agents (ADR-0006) ---
    # How many earlier messages of the conversation the agents see. Enough
    # for follow-up questions; small enough to keep prompts and quota low.
    agent_history_messages: int = Field(default=6, ge=0, le=20)

    # --- Research agent (ADR-0011, docs/architecture/05-rlm-research.md) ---
    # A research question is split into batches, each investigated by a
    # sub-agent; a batch with too much material splits again. These bound
    # how far it goes, so one question cannot use up the free model quota.
    research_max_depth: int = Field(default=3, ge=1, le=5)
    research_max_fanout: int = Field(default=5, ge=1, le=10)
    # Model tokens one research run may use before it stops splitting and
    # extracting, and answers with what it has.
    research_token_budget: int = Field(default=40_000, ge=1_000, le=500_000)
    # Most sources handed to the answer, across every batch.
    research_max_sources: int = Field(default=20, ge=1, le=40)
    # Free model tiers allow only a few requests a minute. Sub-agents make
    # at most this many model calls at the same time, and only this many
    # batches get findings written by the model; the others still give
    # their sources to the answer.
    research_parallel_calls: int = Field(default=2, ge=1, le=10)
    research_max_extractions: int = Field(default=4, ge=0, le=20)

    # --- Memory (ADR-0008) ---
    # Long-term memory per user: their profile and one-line summaries of
    # past questions, recalled into later answers. Off: nothing is saved.
    memory_enabled: bool = True
    # Threads and long-term memory older than this are deleted by the admin
    # clean-up (POST /admin/memory/cleanup).
    memory_retention_days: int = Field(default=30, ge=1, le=3650)
    # When a thread has more messages than this, the older ones are replaced
    # in prompts by a running summary (one model call, every few turns).
    memory_summary_after_messages: int = Field(default=12, ge=4, le=100)

    # --- Human approval ---
    # Deep research can cost several model calls, so the user is asked to
    # approve it first (POST /approvals/{thread_id}). Off: it runs at once.
    approval_for_research: bool = True

    # --- Chat stream (POST /chat) ---
    # Time limit for one whole answer. A normal question takes 10 to 40
    # seconds on the free tiers; a run still going after this is stopped
    # and the client gets an `error` event.
    chat_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    # A comment line is sent when nothing else was sent for this long, so
    # proxies keep the connection open and a closed browser tab is noticed.
    chat_keepalive_seconds: float = Field(default=15.0, gt=0, le=60)

    # --- Login (Keycloak, ADR-0007) ---
    # Every request except the health checks and /metrics needs a Keycloak
    # access token. The API checks the token itself with Keycloak's public
    # signing keys; it never sends the token anywhere.
    #
    # Where the API fetches the signing keys from. Inside Docker this is
    # http://keycloak:8080; outside Docker, http://localhost:8080.
    keycloak_url: str = Field(default="http://localhost:8080", pattern=r"^https?://")
    keycloak_realm: str = Field(default="helix", min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    # The issuer written in every token. Keycloak always names its public
    # address here (http://localhost:8080 locally), even when the API talks
    # to it over the Docker network, so this is set on its own.
    auth_issuer: str = Field(default="http://localhost:8080/realms/helix", pattern=r"^https?://")
    # A token is only accepted if it was issued for this API.
    auth_audience: str = Field(default="enterprise-knowledge-api", min_length=1)
    # How long the signing keys are kept before they are fetched again. A
    # token signed with a key the API has not seen yet makes it fetch the
    # keys at once (at most every `auth_jwks_min_refresh_seconds`), so a key
    # rotation in Keycloak does not need a restart.
    auth_jwks_cache_seconds: int = Field(default=300, ge=10, le=86_400)
    auth_jwks_min_refresh_seconds: int = Field(default=30, ge=1, le=3_600)
    # Clocks on two machines are never exactly the same. A token is still
    # accepted this many seconds after it expires.
    auth_clock_skew_seconds: int = Field(default=30, ge=0, le=120)
    auth_timeout_seconds: float = Field(default=3.0, gt=0, le=30)

    # --- Rate limiting (security/rate_limit.py) ---
    # Two limits per question, both kept in Redis so every API worker sees
    # the same counts:
    #
    # 1. Questions per minute, as token buckets: one per user (sized by the
    #    user's role) and one shared by all users of a role. A user may send
    #    `rate_limit_burst` questions back to back, then one more each time
    #    the bucket refills.
    # 2. A daily budget of language model tokens per user, by role. Each
    #    question reserves `rate_limit_tokens_per_question` before it runs,
    #    and the reservation is corrected to the real count when the answer
    #    ends. A question the user abandons keeps its reservation, so leaving
    #    early does not dodge the budget.
    #
    # Admins can change the per-role numbers while the API runs
    # (PUT /admin/rate-limits/{role}); these are the starting values.
    rate_limit_enabled: bool = True
    rate_limit_burst: int = Field(default=5, ge=1, le=100)
    rate_limit_viewer_per_minute: int = Field(default=10, ge=1, le=600)
    rate_limit_analyst_per_minute: int = Field(default=20, ge=1, le=600)
    rate_limit_admin_per_minute: int = Field(default=40, ge=1, le=600)
    # All users of one role together. Protects the shared free model quota
    # from many users at once.
    rate_limit_role_per_minute: int = Field(default=120, ge=1, le=10_000)
    rate_limit_viewer_daily_tokens: int = Field(default=100_000, ge=1_000)
    rate_limit_analyst_daily_tokens: int = Field(default=250_000, ge=1_000)
    rate_limit_admin_daily_tokens: int = Field(default=500_000, ge=1_000)
    # A normal question uses 3 model calls and 3,000 to 6,000 tokens.
    rate_limit_tokens_per_question: int = Field(default=4_000, ge=1, le=100_000)
    # If Redis stops answering, each API worker keeps its own counts in
    # memory, with every limit multiplied by this, because the workers can
    # no longer see each other's counts.
    rate_limit_fallback_factor: float = Field(default=0.5, gt=0, le=1)
    # A rate-limit check must never slow a question down for long.
    rate_limit_redis_timeout_seconds: float = Field(default=0.5, gt=0, le=5)

    # --- Python sandbox (sandbox/README.md) ---
    # Where the API sends Python to run. Docker Compose sets
    # http://sandbox:8100. Empty: the Python tool answers 503 and research
    # plans fall back to a fixed plan.
    sandbox_url: str | None = Field(default=None, pattern=r"^https?://")
    # The sandbox stops a run after 5 seconds; this covers the round trip.
    sandbox_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    # --- Enterprise data tools (the MCP server, mcp_server/README.md) ---
    # Where the API's tools agent reaches the MCP server. Docker Compose sets
    # http://mcp:8200/mcp. Empty: enterprise-data questions are answered
    # from the documents, and the answer says the tools were unavailable.
    mcp_url: str | None = Field(default=None, pattern=r"^https?://")
    mcp_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    # --- LangSmith tracing ---
    # Off unless switched on and a key is set. The app works the same
    # without it; it just sends no traces.
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "helix-knowledge-agent"
    # Change for the EU region: https://eu.api.smith.langchain.com
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    @field_validator(
        "postgres_url",
        "redis_url",
        "pinecone_api_key",
        "anthropic_api_key",
        "gemini_api_key",
        "langsmith_api_key",
        "claude_model",
        "gemini_model",
        "sandbox_url",
        "mcp_url",
        mode="before",
    )
    @classmethod
    def _empty_means_not_set(cls, value: object) -> object:
        # `.env.example` lists every key with an empty value, e.g.
        # `GEMINI_API_KEY=`. Treat that as "not set", not as an empty key.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _every_provider_has_a_model(self) -> Self:
        """A provider with a key must have its model set too.

        Checked when the settings are read, so a missing model stops the app
        at startup with a clear message, not on a user's first question. A
        provider without a key is off, and its model is not needed.
        """
        missing = []
        for name, key, model in self._llm_settings():
            if key is not None and model is None:
                key_var, model_var = _LLM_VARIABLES[name]
                missing.append(f"{key_var} is set, so {model_var} must be set too")
        if missing:
            raise ValueError("; ".join(missing) + ". See .env.example for the recommended models.")
        return self

    def _llm_settings(self) -> list[tuple[str, SecretStr | None, str | None]]:
        # The order here is the failover order.
        return [
            ("anthropic", self.anthropic_api_key, self.claude_model),
            ("gemini", self.gemini_api_key, self.gemini_model),
        ]

    def llm_providers(self) -> list[tuple[str, SecretStr, str]]:
        """The language model providers that have a key, in failover order:
        (name, key, model)."""
        return [
            (name, key, model)
            for name, key, model in self._llm_settings()
            if key is not None and model is not None
        ]

    @property
    def jwks_url(self) -> str:
        """Keycloak's public signing keys for the realm (a JWKS document)."""
        base = self.keycloak_url.rstrip("/")
        return f"{base}/realms/{self.keycloak_realm}/protocol/openid-connect/certs"

    @property
    def is_production(self) -> bool:
        return self.app_env is Environment.PRODUCTION


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read once and then cached."""
    return Settings()
