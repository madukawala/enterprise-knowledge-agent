"""Login tokens: every way a bad token can arrive, and that each one is refused.

Each attack is tried twice: against the token checker on its own, and
through the API, where it must end in a 401 with the standard message and
never reach the agents.
"""

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from core.errors import AuthError, PermissionDeniedError
from security.auth import MAX_TOKEN_CHARS, TokenVerifier, pseudonymise, role_from_claims
from security.roles import Role
from tests.helpers.tokens import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    TEST_KEY_ID,
    TEST_USERS,
    TOKENS,
    TestUser,
    TokenFactory,
)


def _verifier() -> TokenVerifier:
    return TokenVerifier(TOKENS.signing_keys, issuer=TEST_ISSUER, audience=TEST_AUDIENCE)


def _b64(data: dict[str, Any] | bytes) -> str:
    raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unsigned_token(**header: Any) -> str:
    """A token with `alg: none` and an empty signature."""
    claims = TOKENS.claims(TEST_USERS[Role.ADMIN])
    return f"{_b64({'alg': 'none', 'kid': TEST_KEY_ID, **header})}.{_b64(claims)}."


def _hs256_with_public_key() -> str:
    """The classic key-confusion attack: HMAC-sign with the server's public key."""
    header = {"alg": "HS256", "kid": TEST_KEY_ID, "typ": "JWT"}
    signing_input = f"{_b64(header)}.{_b64(TOKENS.claims(TEST_USERS[Role.ADMIN]))}"
    signature = hmac.new(TOKENS.public_pem, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


def _tampered_role() -> str:
    """A real viewer token with the payload edited to say admin."""
    header, _, signature = TOKENS.token(Role.VIEWER).split(".")
    claims = TOKENS.claims(TEST_USERS[Role.VIEWER])
    claims["realm_access"] = {"roles": ["admin"]}
    return f"{header}.{_b64(claims)}.{signature}"


# name -> a token that must be refused with 401
BAD_TOKENS = {
    "expired": lambda: TOKENS.token(Role.ADMIN, expires_in=-120),
    "wrong audience": lambda: TOKENS.token(Role.ADMIN, extra_claims={"aud": "another-app"}),
    "wrong issuer": lambda: TOKENS.token(
        Role.ADMIN, extra_claims={"iss": "http://evil.example/realms/helix"}
    ),
    "signed by another key with our key id": lambda: TokenFactory().token(Role.ADMIN),
    "unknown key id": lambda: TOKENS.token(Role.ADMIN, kid="not-a-real-key"),
    "alg none": _unsigned_token,
    "HS256 signed with the public key": _hs256_with_public_key,
    "payload edited after signing": _tampered_role,
    "no expiry": lambda: TOKENS.token(Role.ADMIN, drop_claims=("exp",)),
    "no subject": lambda: TOKENS.token(Role.ADMIN, drop_claims=("sub",)),
    "no audience": lambda: TOKENS.token(Role.ADMIN, drop_claims=("aud",)),
    "an ID token": lambda: TOKENS.token(Role.ADMIN, extra_claims={"typ": "ID"}),
    "a refresh token": lambda: TOKENS.token(Role.ADMIN, extra_claims={"typ": "Refresh"}),
    "not a token": lambda: "hello",
    "three dots of nothing": lambda: "..",
    "far too long": lambda: "a" * (MAX_TOKEN_CHARS + 1),
}


# ---------------------------------------------------------------
# The token checker on its own
# ---------------------------------------------------------------


@pytest.mark.parametrize("role", list(Role))
async def test_a_valid_token_gives_the_user_and_role(role: Role) -> None:
    user = TEST_USERS[role]

    principal = await _verifier().verify(TOKENS.token(role))

    assert principal.role is role
    assert principal.user_id == user.user_id
    assert principal.username == user.username
    assert principal.name == user.name


@pytest.mark.parametrize("name", list(BAD_TOKENS))
async def test_bad_tokens_are_refused(name: str) -> None:
    with pytest.raises(AuthError):
        await _verifier().verify(BAD_TOKENS[name]())


async def test_a_token_a_little_past_expiry_is_still_accepted_for_clock_skew() -> None:
    token = TOKENS.token(Role.VIEWER, expires_in=-10)
    assert (await _verifier().verify(token)).role is Role.VIEWER


async def test_a_valid_user_without_one_of_our_roles_is_forbidden() -> None:
    token = TOKENS.token(
        Role.VIEWER, extra_claims={"realm_access": {"roles": ["default-roles-helix"]}}
    )
    with pytest.raises(PermissionDeniedError):
        await _verifier().verify(token)


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({"realm_access": {"roles": ["viewer", "admin"]}}, Role.ADMIN),
        ({"realm_access": {"roles": ["analyst", "viewer"]}}, Role.ANALYST),
        ({"realm_access": {"roles": ["superuser"]}}, None),
        ({"realm_access": {"roles": "admin"}}, None),
        ({"realm_access": "admin"}, None),
        # A role at the top level or in another client is not a realm role.
        ({"roles": ["admin"]}, None),
        ({"resource_access": {"other-app": {"roles": ["admin"]}}}, None),
        ({}, None),
    ],
)
def test_the_role_comes_only_from_realm_roles_and_the_highest_wins(
    claims: dict[str, Any], expected: Role | None
) -> None:
    assert role_from_claims(claims) is expected


def test_the_pseudonym_is_stable_and_does_not_contain_the_user_id() -> None:
    user_id = TEST_USERS[Role.ADMIN].user_id
    assert pseudonymise(user_id) == pseudonymise(user_id)
    assert user_id not in pseudonymise(user_id)
    assert pseudonymise(user_id) != pseudonymise(TEST_USERS[Role.VIEWER].user_id)


# ---------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------


async def _get_me(app: FastAPI, headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return await http.get("/me", headers=headers)


@pytest.mark.parametrize("name", list(BAD_TOKENS))
async def test_the_api_answers_bad_tokens_with_the_same_401(
    app: FastAPI, name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    token = BAD_TOKENS[name]()

    response = await _get_me(app, {"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    error = response.json()["error"]
    # Same message whatever was wrong, so it tells an attacker nothing.
    assert error == {
        "code": "unauthorized",
        "message": "You need to sign in again.",
        "request_id": error["request_id"],
    }
    # The reason is in the security log, the token itself is not.
    output = capsys.readouterr().out
    assert '"token_rejected"' in output
    assert '"logger": "security"' in output
    if len(token) > 20:
        assert token not in output


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ({}, "missing"),
        ({"Authorization": ""}, "missing"),
        ({"Authorization": "Bearer"}, "no_bearer_token"),
        ({"Authorization": "Basic dmVyYTpwYXNz"}, "no_bearer_token"),
        ({"Authorization": f"Token {TOKENS.token(Role.ADMIN)}"}, "no_bearer_token"),
        # Old local-development header: gives nothing on its own.
        ({"X-Dev-Role": "admin"}, "missing"),
    ],
    ids=["none", "empty", "no token", "basic", "wrong scheme", "dev header"],
)
async def test_requests_without_a_bearer_token_get_401_and_are_logged(
    app: FastAPI, headers: dict[str, str], reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    response = await _get_me(app, headers)

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "You need to sign in again."
    rejected = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if '"token_rejected"' in line
    ]
    assert [(line["logger"], line["reason"]) for line in rejected] == [("security", reason)]
    # A credential sent with the wrong scheme is still never written down.
    for value in headers.values():
        if len(value) > 20:
            assert value.split(" ", 1)[1] not in json.dumps(rejected)


async def test_a_user_without_a_role_gets_403_not_401(app: FastAPI) -> None:
    token = TOKENS.token(Role.VIEWER, extra_claims={"realm_access": {"roles": []}})
    response = await _get_me(app, {"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert "no role in this app" in response.json()["error"]["message"]


async def test_logs_name_the_user_by_pseudonym_only(
    app: FastAPI, capsys: pytest.CaptureFixture[str]
) -> None:
    user = TestUser(username="private.person", role=Role.ANALYST, name="Private Person")
    response = await _get_me(app, {"Authorization": f"Bearer {TOKENS.token_for(user)}"})

    assert response.status_code == 200
    finished = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if '"request_finished"' in line
    ]
    assert finished[-1]["user"] == pseudonymise(user.user_id)
    assert finished[-1]["role"] == "analyst"
    output = json.dumps(finished)
    for private in (user.user_id, user.username, user.name):
        assert private not in output


async def test_the_issued_at_time_must_be_present(app: FastAPI) -> None:
    token = TOKENS.token(Role.VIEWER, drop_claims=("iat",))
    response = await _get_me(app, {"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_test_tokens_expire_in_minutes_like_keycloaks() -> None:
    claims = TOKENS.claims(TEST_USERS[Role.VIEWER])
    assert claims["exp"] - int(time.time()) <= 300
