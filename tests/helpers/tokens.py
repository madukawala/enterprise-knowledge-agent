"""Signed test tokens that look like the ones Keycloak issues.

Tests never talk to the real Keycloak. Instead they sign tokens with an RSA
key generated for the test session and give the API the matching public key
(as a JWKS), the same way the real API reads Keycloak's keys.

    app = create_app(settings, signing_keys=TOKENS.signing_keys)
    await http.get("/me", headers=bearer(Role.ANALYST))

A second `TokenFactory()` has a different key, so its tokens are forgeries
as far as the app is concerned.
"""

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from security.auth import StaticKeys
from security.roles import Role

TEST_ISSUER = "http://localhost:8080/realms/helix"
TEST_AUDIENCE = "enterprise-knowledge-api"
TEST_KEY_ID = "test-signing-key"


@dataclass(frozen=True)
class TestUser:
    __test__ = False  # not a test class, despite the name

    username: str
    role: Role
    name: str = ""
    user_id: str = field(default_factory=lambda: str(uuid.uuid4()))


# One user per role, mirroring the test users in the Keycloak realm file.
TEST_USERS: dict[Role, TestUser] = {
    Role.VIEWER: TestUser(username="vera.viewer", role=Role.VIEWER, name="Vera Viewer"),
    Role.ANALYST: TestUser(username="adam.analyst", role=Role.ANALYST, name="Adam Analyst"),
    Role.ADMIN: TestUser(username="ada.admin", role=Role.ADMIN, name="Ada Admin"),
}


class TokenFactory:
    """Creates signed JWTs for test users. Each instance has its own key pair."""

    def __init__(self) -> None:
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    @property
    def jwks(self) -> dict[str, Any]:
        """The public key as a JWKS document, like Keycloak's certs endpoint."""
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self._private_key.public_key()))
        jwk.update({"kid": TEST_KEY_ID, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    def claims(self, user: TestUser, *, expires_in: int = 300) -> dict[str, Any]:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": TEST_ISSUER,
            "aud": TEST_AUDIENCE,
            "sub": user.user_id,
            "preferred_username": user.username,
            # Keycloak adds its own default role next to ours.
            "realm_access": {"roles": ["default-roles-helix", user.role.value]},
            "typ": "Bearer",
            "azp": "enterprise-knowledge-ui",
            "iat": now,
            "exp": now + expires_in,
        }
        if user.name:
            claims["name"] = user.name
        return claims

    @property
    def public_pem(self) -> bytes:
        """The public key in PEM form, for the HS256 key-confusion attack test."""
        return self._private_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    @property
    def signing_keys(self) -> StaticKeys:
        """The public key as the API's key source, in place of Keycloak."""
        return StaticKeys(self.jwks)

    def token(
        self,
        role: Role,
        *,
        expires_in: int = 300,
        extra_claims: dict[str, Any] | None = None,
        drop_claims: tuple[str, ...] = (),
        kid: str = TEST_KEY_ID,
    ) -> str:
        """Return a signed token for the test user with `role`.

        Use a negative `expires_in` for an expired token, `extra_claims` to
        override any claim (for example a wrong issuer or audience), and
        `drop_claims` to leave claims out.
        """
        return self.token_for(
            TEST_USERS[role],
            expires_in=expires_in,
            extra_claims=extra_claims,
            drop_claims=drop_claims,
            kid=kid,
        )

    def token_for(
        self,
        user: TestUser,
        *,
        expires_in: int = 300,
        extra_claims: dict[str, Any] | None = None,
        drop_claims: tuple[str, ...] = (),
        kid: str = TEST_KEY_ID,
    ) -> str:
        """Return a signed token for any test user, for example a second analyst."""
        payload = self.claims(user, expires_in=expires_in)
        payload.update(extra_claims or {})
        for claim in drop_claims:
            payload.pop(claim, None)
        return jwt.encode(payload, self._private_key, algorithm="RS256", headers={"kid": kid})


# One key pair for the whole test run. The app fixtures trust its public
# key, and the helpers below sign with it, so a test only says which role
# is asking.
TOKENS = TokenFactory()


def bearer(role: Role, factory: TokenFactory = TOKENS) -> dict[str, str]:
    """The Authorization header for the test user with `role`."""
    return {"Authorization": f"Bearer {factory.token(role)}"}


def bearer_for(user: TestUser, factory: TokenFactory = TOKENS) -> dict[str, str]:
    return {"Authorization": f"Bearer {factory.token_for(user)}"}
