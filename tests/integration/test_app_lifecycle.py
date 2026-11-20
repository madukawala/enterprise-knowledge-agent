from fastapi import FastAPI
from starlette.types import Message, Receive, Scope, Send

from api.main import create_app
from api.middleware import RequestContextMiddleware
from core.config import Environment, Settings


async def test_startup_and_shutdown_run_cleanly(app: FastAPI) -> None:
    async with app.router.lifespan_context(app):
        pass


def test_api_docs_are_hidden_in_production() -> None:
    app = create_app(Settings(_env_file=None, app_env=Environment.PRODUCTION))

    assert app.docs_url is None
    assert app.openapi_url is None


async def test_non_http_traffic_passes_through_unchanged() -> None:
    seen: list[Scope] = []

    async def inner_app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        return None

    scope: Scope = {"type": "lifespan"}
    await RequestContextMiddleware(inner_app)(scope, receive, send)

    assert seen == [{"type": "lifespan"}]
