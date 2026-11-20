"""Builds the FastAPI application.

Run locally with `make run-api`, which starts:

    uvicorn api.main:app --reload

`create_app()` takes optional settings so tests can build an app with their
own configuration instead of reading the environment.

On startup the agents are set up once (models, search index, graph) and
shared by every request. If no language model is configured, the API
still starts, so health checks and metrics work, and `POST /chat` answers
503 until a key is added. The reason is in the startup log.
"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from prometheus_client import CollectorRegistry
from prometheus_fastapi_instrumentator import Instrumentator

from agents.llm import LLMSetupError
from agents.service import AgentService, open_agent_service
from agents.telemetry import AgentMetrics
from api.errors import register_exception_handlers
from api.middleware import RequestContextMiddleware
from api.readiness import build_checks
from api.routes import chat, health
from api.telemetry import ChatMetrics
from api.threads import ThreadRegistry
from core.config import Settings, get_settings
from core.logging import configure_logging, get_logger
from rag.telemetry import RetrievalMetrics

logger = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    # One metrics registry per app: the request metrics, the agents, search
    # and the chat stream all appear together on /metrics.
    registry = CollectorRegistry()
    agent_metrics = AgentMetrics(registry)
    retrieval_metrics = RetrievalMetrics(registry)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("api_starting", app_env=settings.app_env)
        async with AsyncExitStack() as stack:
            app.state.agents = await _open_agents(stack, settings, agent_metrics, retrieval_metrics)
            yield
        logger.info("api_stopped")

    app = FastAPI(
        title="Enterprise Knowledge Agent API",
        version="0.1.0",
        lifespan=lifespan,
        # The interactive API docs are useful locally but not in production.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.state.settings = settings
    app.state.readiness_checks = build_checks(settings)
    app.state.agents = None  # set on startup
    app.state.chat_metrics = ChatMetrics(registry)
    app.state.threads = ThreadRegistry(
        busy_for_at_most=settings.chat_timeout_seconds + settings.chat_keepalive_seconds
    )

    if settings.metrics_enabled:
        _add_metrics(app, registry)
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(chat.router)
    return app


async def _open_agents(
    stack: AsyncExitStack,
    settings: Settings,
    metrics: AgentMetrics,
    retrieval_metrics: RetrievalMetrics,
) -> AgentService | None:
    """Set up the agents for the app's lifetime, or None if no model is configured."""
    try:
        agents = await stack.enter_async_context(
            open_agent_service(settings, metrics=metrics, retrieval_metrics=retrieval_metrics)
        )
    except LLMSetupError as error:
        logger.warning("agents_not_configured", reason=str(error))
        return None
    logger.info("agents_ready", traced=agents.traced)
    return agents


def _add_metrics(app: FastAPI, registry: CollectorRegistry) -> None:
    """Expose request count, errors and latency per endpoint on /metrics.

    Each app gets its own metrics registry, so tests can build many apps in
    one process without "duplicated metric" errors. Health probes and the
    metrics endpoint itself are left out, as Docker and Prometheus call them
    every few seconds and they would hide the real traffic.
    """
    Instrumentator(
        registry=registry,
        excluded_handlers=["/healthz", "/readyz", "/metrics"],
    ).instrument(app).expose(app, include_in_schema=False)


app = create_app()
