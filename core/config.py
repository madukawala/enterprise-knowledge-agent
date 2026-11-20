"""Application settings.

Settings come from environment variables, with the `.env` file as a fallback.
Every variable is listed with an example value in `.env.example`.

Use `get_settings()` instead of creating `Settings()` directly, so the
environment is read once per process.
"""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
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

    @property
    def is_production(self) -> bool:
        return self.app_env is Environment.PRODUCTION


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read once and then cached."""
    return Settings()
