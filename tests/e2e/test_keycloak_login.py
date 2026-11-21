"""Login against the real Keycloak from Docker Compose.

The other tests sign their own tokens. These ones prove the same code works
with tokens from the real realm file: the issuer, the audience mapper, the
realm roles and the key rotation endpoint all have to line up.

Needs Keycloak running (`make up`, or `docker compose up -d keycloak`).
Run with `make test-keycloak`. Skipped when Keycloak cannot be reached.
"""

import httpx
import pytest
from fastapi import FastAPI

from api.main import create_app
from core.config import Environment, Settings
from security.auth import TokenVerifier
from security.roles import Role
from ui.auth import KeycloakLogin, LoginError
from ui.config import UiSettings

KEYCLOAK_URL = "http://localhost:8080"
DEMO_USERS = {Role.VIEWER: "vera.viewer", Role.ANALYST: "adam.analyst", Role.ADMIN: "ada.admin"}


def _keycloak_is_up() -> bool:
    try:
        return httpx.get(f"{KEYCLOAK_URL}/realms/helix", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not _keycloak_is_up(), reason="Keycloak is not running")


@pytest.fixture
def login() -> KeycloakLogin:
    return KeycloakLogin(UiSettings(keycloak_url=KEYCLOAK_URL))


@pytest.fixture
def real_settings() -> Settings:
    return Settings(_env_file=None, app_env=Environment.TEST, keycloak_url=KEYCLOAK_URL)


def _token(login: KeycloakLogin, role: Role) -> str:
    return login.sign_in(DEMO_USERS[role], f"{role.value}-demo-pass").access_token


@pytest.mark.parametrize("role", list(Role))
async def test_each_demo_user_gets_a_token_the_api_accepts(
    login: KeycloakLogin, real_settings: Settings, role: Role
) -> None:
    verifier = TokenVerifier.from_settings(real_settings)
    try:
        principal = await verifier.verify(_token(login, role))
    finally:
        await verifier.keys.aclose()

    assert principal.role is role
    assert principal.username == DEMO_USERS[role]


def test_a_wrong_password_is_refused(login: KeycloakLogin) -> None:
    with pytest.raises(LoginError):
        login.sign_in("vera.viewer", "not-the-password")


async def test_the_role_matrix_holds_with_real_tokens(
    login: KeycloakLogin, real_settings: Settings
) -> None:
    app: FastAPI = create_app(real_settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        statuses = {}
        for role in Role:
            headers = {"Authorization": f"Bearer {_token(login, role)}"}
            statuses[role] = (await http.get("/admin/access-matrix", headers=headers)).status_code

    assert statuses == {Role.VIEWER: 403, Role.ANALYST: 403, Role.ADMIN: 200}


def test_a_signed_out_session_cannot_be_refreshed(login: KeycloakLogin) -> None:
    session = login.sign_in("vera.viewer", "viewer-demo-pass")
    login.sign_out(session)

    with pytest.raises(LoginError):
        login.fresh(type(session)(session.access_token, session.refresh_token, 0, None))
