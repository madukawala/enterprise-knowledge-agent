"""Fixtures shared by every test layer.

Markers are added automatically from the folder a test lives in, so a test
in `tests/security/` is always run by `make test-security`.
"""

from collections.abc import AsyncIterator, Callable, Iterable
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from api.main import create_app
from core.config import Environment, LogFormat, Settings
from security.roles import Role
from tests.helpers.llm import fake_chat_model
from tests.helpers.tokens import TOKENS, TokenFactory

_TESTS_DIR = Path(__file__).parent
_FOLDER_MARKERS = {"unit", "integration", "graph", "security", "resilience", "e2e"}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        folder = item.path.relative_to(_TESTS_DIR).parts[0]
        if folder in _FOLDER_MARKERS:
            item.add_marker(folder)


# ---------------------------------------------------------------
# Settings and the API
# ---------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    # _env_file=None: tests must not depend on a developer's local .env file.
    return Settings(
        _env_file=None,
        app_env=Environment.TEST,
        log_level="DEBUG",
        log_format=LogFormat.JSON,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    # Trusts the test signing key instead of fetching Keycloak's keys.
    return create_app(settings, signing_keys=TOKENS.signing_keys)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    # raise_app_exceptions=False: return the 500 response the user would see
    # instead of re-raising the error inside the test.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


# ---------------------------------------------------------------
# Login tokens, one per role
# ---------------------------------------------------------------


@pytest.fixture(scope="session")
def token_factory() -> TokenFactory:
    # The same key pair the app fixtures trust.
    return TOKENS


@pytest.fixture
def viewer_token(token_factory: TokenFactory) -> str:
    return token_factory.token(Role.VIEWER)


@pytest.fixture
def analyst_token(token_factory: TokenFactory) -> str:
    return token_factory.token(Role.ANALYST)


@pytest.fixture
def admin_token(token_factory: TokenFactory) -> str:
    return token_factory.token(Role.ADMIN)


# ---------------------------------------------------------------
# Language models
# ---------------------------------------------------------------


@pytest.fixture
def fake_llm() -> Callable[[Iterable[str | AIMessage]], GenericFakeChatModel]:
    """Build a fake chat model: `fake_llm(["first reply", "second reply"])`."""
    return fake_chat_model
