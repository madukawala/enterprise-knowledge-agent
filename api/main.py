"""Builds the FastAPI application.

Run locally with `make run-api`, which starts:

    uvicorn api.main:app --reload

`create_app()` takes optional settings so tests can build an app with their
own configuration instead of reading the environment. Tests also pass
their own signing keys, so login tokens can be checked without Keycloak.

Every route except the health checks and /metrics needs a Keycloak login
token (api/identity.py). Questions are rate limited per user and per role,
with the counts in Redis (security/rate_limit.py). Keycloak's public keys are fetched on the first
request that needs them, not at startup, so the API can start before
Keycloak is ready.

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
from agents.tools.sandbox import CodeRunner, SandboxClient
from api.audit import AuditLog
from api.errors import register_exception_handlers
from api.middleware import RequestContextMiddleware
from api.readiness import build_checks
from api.routes import admin, chat, feedback, health, me, threads, tools
from api.telemetry import ChatMetrics
from api.threads import ThreadRegistry
from core.config import Settings, get_settings
from core.logging import configure_logging, get_logger
from rag.telemetry import RetrievalMetrics
from security.auth import KeySource, TokenVerifier
from security.rate_limit import RateLimiter, RateLimitMetrics, RedisStore, Store

logger = get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    signing_keys: KeySource | None = None,
    rate_limit_store: Store | None = None,
    sandbox: CodeRunner | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    verifier = TokenVerifier.from_settings(settings, signing_keys)
    configure_logging(settings.log_level, settings.log_format)

    # One metrics registry per app: the request metrics, the agents, search
    # and the chat stream all appear together on /metrics.
    registry = CollectorRegistry()
    agent_metrics = AgentMetrics(registry)
    retrieval_metrics = RetrievalMetrics(registry)
    rate_limiter = RateLimiter(
        settings,
        rate_limit_store or _rate_limit_store(settings),
        metrics=RateLimitMetrics(registry),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("api_starting", app_env=settings.app_env)
        async with AsyncExitStack() as stack:
            stack.push_async_callback(verifier.keys.aclose)
            stack.push_async_callback(rate_limiter.aclose)
            if app.state.sandbox is not None:
                stack.push_async_callback(app.state.sandbox.aclose)
            app.state.agents = await _open_agents(
                stack, settings, agent_metrics, retrieval_metrics, app.state.sandbox
            )
            if app.state.agents is not None and app.state.agents.store is not None:
                # Thread owners and the audit log are saved next to the
                # conversations (api/threads.py, api/audit.py).
                app.state.threads.index = app.state.agents.store
                app.state.audit = AuditLog(app.state.agents.store)
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
    app.state.token_verifier = verifier
    app.state.rate_limiter = rate_limiter
    app.state.agent_metrics = agent_metrics
    app.state.audit = None  # set on startup, once the store is open
    app.state.sandbox = sandbox or _sandbox(settings)
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
    app.include_router(me.router)
    app.include_router(chat.router)
    app.include_router(threads.router)
    app.include_router(feedback.router)
    app.include_router(tools.router)
    app.include_router(admin.router)
    return app


def _sandbox(settings: Settings) -> CodeRunner | None:
    """The Python sandbox client, or None when SANDBOX_URL is not set."""
    if not settings.sandbox_url:
        return None
    return SandboxClient(settings.sandbox_url, timeout_seconds=settings.sandbox_timeout_seconds)


def _rate_limit_store(settings: Settings) -> Store | None:
    """Redis when it is configured. Without it (`make run-api` on its own),
    the limiter keeps its counts in memory."""
    if settings.redis_url is None or not settings.redis_url.get_secret_value():
        return None
    return RedisStore.from_url(
        settings.redis_url.get_secret_value(),
        timeout_seconds=settings.rate_limit_redis_timeout_seconds,
    )


async def _open_agents(
    stack: AsyncExitStack,
    settings: Settings,
    metrics: AgentMetrics,
    retrieval_metrics: RetrievalMetrics,
    sandbox: CodeRunner | None,
) -> AgentService | None:
    """Set up the agents for the app's lifetime, or None if no model is configured."""
    try:
        agents = await stack.enter_async_context(
            open_agent_service(
                settings,
                metrics=metrics,
                retrieval_metrics=retrieval_metrics,
                sandbox=sandbox,
            )
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
