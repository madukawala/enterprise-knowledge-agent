"""Signed test tokens that look like the ones Keycloak issues.

Tests never talk to the real Keycloak. Instead they sign tokens with an RSA
key generated for the test session and give the API the matching public key
(as a JWKS), the same way the real API reads Keycloak's keys.
"""

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from security.roles import Role

TEST_ISSUER = "http://localhost:8080/realms/helix"
TEST_AUDIENCE = "enterprise-knowledge-api"
TEST_KEY_ID = "test-signing-key"


@dataclass(frozen=True)
class TestUser:
    __test__ = False  # not a test class, despite the name

    username: str
    role: Role
    user_id: str = field(default_factory=lambda: str(uuid.uuid4()))


# One user per role, mirroring the test users in the Keycloak realm file.
TEST_USERS: dict[Role, TestUser] = {
    Role.VIEWER: TestUser(username="vera.viewer", role=Role.VIEWER),
    Role.ANALYST: TestUser(username="adam.analyst", role=Role.ANALYST),
    Role.ADMIN: TestUser(username="ada.admin", role=Role.ADMIN),
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
        return {
            "iss": TEST_ISSUER,
            "aud": TEST_AUDIENCE,
            "sub": user.user_id,
            "preferred_username": user.username,
            "realm_access": {"roles": [user.role.value]},
            "iat": now,
            "exp": now + expires_in,
        }

    def token(
        self,
        role: Role,
        *,
        expires_in: int = 300,
        extra_claims: dict[str, Any] | None = None,
    ) -> str:
        """Return a signed token for the test user with `role`.

        Use a negative `expires_in` for an expired token, and `extra_claims`
        to override any claim (for example a wrong issuer or audience).
        """
        payload = self.claims(TEST_USERS[role], expires_in=expires_in)
        payload.update(extra_claims or {})
        return jwt.encode(
            payload, self._private_key, algorithm="RS256", headers={"kid": TEST_KEY_ID}
        )
