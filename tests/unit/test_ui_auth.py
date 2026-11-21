"""Signing in to Keycloak from the UI (ui/auth.py), with a fake Keycloak."""

import time
from urllib.parse import parse_qs

import httpx
import pytest

from ui.auth import (
    BAD_CREDENTIALS,
    SESSION_ENDED,
    KeycloakLogin,
    LoginError,
    Session,
)
from ui.config import UiSettings

TOKEN_URL = "http://localhost:8080/realms/helix/protocol/openid-connect/token"
LOGOUT_URL = "http://localhost:8080/realms/helix/protocol/openid-connect/logout"


def _tokens(access: str = "access-1", refresh: str | None = "refresh-1") -> dict[str, object]:
    body: dict[str, object] = {"access_token": access, "expires_in": 300}
    if refresh:
        body.update(refresh_token=refresh, refresh_expires_in=1800)
    return body


class FakeKeycloak:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.requests: list[httpx.Request] = []
        self.responses = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def form(self, n: int = 0) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(self.requests[n].content.decode()).items()}


def _login(keycloak: FakeKeycloak) -> KeycloakLogin:
    return KeycloakLogin(UiSettings(), transport=httpx.MockTransport(keycloak))


def test_signing_in_uses_the_password_grant_for_the_ui_client() -> None:
    keycloak = FakeKeycloak(httpx.Response(200, json=_tokens()))

    session = _login(keycloak).sign_in("  adam.analyst ", "analyst-demo-pass")

    assert str(keycloak.requests[0].url) == TOKEN_URL
    assert keycloak.form() == {
        "client_id": "enterprise-knowledge-ui",
        "scope": "openid",
        "grant_type": "password",
        "username": "adam.analyst",
        "password": "analyst-demo-pass",
    }
    assert session.access_token == "access-1"
    assert session.refresh_token == "refresh-1"
    assert 290 < session.expires_at - time.time() <= 300


@pytest.mark.parametrize("status", [400, 401])
def test_a_refused_sign_in_gives_one_message_whatever_the_reason(status: int) -> None:
    keycloak = FakeKeycloak(httpx.Response(status, json={"error": "invalid_grant"}))
    with pytest.raises(LoginError) as caught:
        _login(keycloak).sign_in("nobody", "wrong")
    assert caught.value.message == BAD_CREDENTIALS


def test_empty_fields_are_not_sent_to_keycloak() -> None:
    keycloak = FakeKeycloak()
    with pytest.raises(LoginError):
        _login(keycloak).sign_in(" ", "")
    assert keycloak.requests == []


def test_keycloak_down_gives_a_clear_message() -> None:
    keycloak = FakeKeycloak(httpx.ConnectError("refused"))
    with pytest.raises(LoginError, match="Cannot reach the sign-in service"):
        _login(keycloak).sign_in("vera.viewer", "pass")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="oops"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"expires_in": 300}),
        httpx.Response(200, json={"access_token": "", "expires_in": 300}),
    ],
)
def test_odd_replies_from_keycloak_are_errors_not_crashes(response: httpx.Response) -> None:
    with pytest.raises(LoginError):
        _login(FakeKeycloak(response)).sign_in("vera.viewer", "pass")


def _session(expires_in: float, refresh_in: float | None = 1800) -> Session:
    now = time.time()
    return Session(
        access_token="old",
        refresh_token="refresh-1" if refresh_in is not None else None,
        expires_at=now + expires_in,
        refresh_expires_at=now + refresh_in if refresh_in is not None else None,
    )


def test_a_fresh_session_is_kept_without_calling_keycloak() -> None:
    keycloak = FakeKeycloak()
    session = _session(expires_in=200)
    assert _login(keycloak).fresh(session) is session
    assert keycloak.requests == []


def test_a_session_about_to_expire_is_refreshed() -> None:
    keycloak = FakeKeycloak(httpx.Response(200, json=_tokens("access-2", "refresh-2")))

    session = _login(keycloak).fresh(_session(expires_in=10))

    assert keycloak.form()["grant_type"] == "refresh_token"
    assert keycloak.form()["refresh_token"] == "refresh-1"
    assert session.access_token == "access-2"


@pytest.mark.parametrize(
    "session",
    [_session(expires_in=-5, refresh_in=-1), _session(expires_in=-5, refresh_in=None)],
    ids=["refresh token expired", "no refresh token"],
)
def test_a_session_that_cannot_be_refreshed_has_ended(session: Session) -> None:
    keycloak = FakeKeycloak()
    with pytest.raises(LoginError) as caught:
        _login(keycloak).fresh(session)
    assert caught.value.message == SESSION_ENDED
    assert keycloak.requests == []


def test_a_refresh_keycloak_refuses_means_the_session_has_ended() -> None:
    keycloak = FakeKeycloak(httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(LoginError) as caught:
        _login(keycloak).fresh(_session(expires_in=0))
    assert caught.value.message == SESSION_ENDED


def test_signing_out_ends_the_session_in_keycloak() -> None:
    keycloak = FakeKeycloak(httpx.Response(204))

    _login(keycloak).sign_out(_session(expires_in=200))

    assert str(keycloak.requests[0].url) == LOGOUT_URL
    assert keycloak.form() == {"client_id": "enterprise-knowledge-ui", "refresh_token": "refresh-1"}


def test_signing_out_while_keycloak_is_down_does_not_fail() -> None:
    _login(FakeKeycloak(httpx.ConnectError("refused"))).sign_out(_session(expires_in=200))


def test_signing_out_without_a_refresh_token_does_nothing() -> None:
    keycloak = FakeKeycloak()
    _login(keycloak).sign_out(_session(expires_in=200, refresh_in=None))
    assert keycloak.requests == []


def test_a_session_never_prints_its_tokens() -> None:
    session = _session(expires_in=200)
    assert "old" not in repr(session)
    assert "refresh-1" not in repr(session)
    assert "old" not in str(session)
