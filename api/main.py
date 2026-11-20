"""Builds the FastAPI application.

Run locally with `make run-api`, which starts:

    uvicorn api.main:app --reload

`create_app()` takes optional settings so tests can build an app with their
own configuration instead of reading the environment.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.errors import register_exception_handlers
from api.middleware import RequestContextMiddleware
from api.routes import health
from core.config import Settings, get_settings
from core.logging import configure_logging, get_logger

logger = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info("api_starting", app_env=settings.app_env)
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

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    return app


app = create_app()
