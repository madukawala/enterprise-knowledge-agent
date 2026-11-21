"""Checks Keycloak login tokens and turns them into a `Principal`.

The UI signs the user in with Keycloak and sends the access token to the API
as `Authorization: Bearer <token>`. The API trusts the token only after it
has checked, in this order:

1. The token is not absurdly long and has a readable header.
2. The header names RS256. Anything else is refused before the signature is
   looked at: `none` (no signature at all), HS256 (a classic trick where the
   public key is used as a shared secret) and every other algorithm.
3. The header names a key ID (`kid`) that Keycloak really publishes.
4. The signature is valid for that key.
5. The issuer is our realm, the audience is this API, and the token has not
   expired (a small clock skew is allowed). `exp`, `iat`, `sub`, `iss` and
   `aud` must all be present.
6. It is an access token, not an ID token or a refresh token.
7. It carries one of our realm roles. A valid user without one may sign in
   to Keycloak but may not use the app.

The role comes from the token only (ADR-0007). Nothing in the request body,
headers or the conversation can change it.

Keycloak's public keys are fetched from its JWKS endpoint and kept for a few
minutes (`JwksClient`). If Keycloak is briefly down, keys already in memory
keep working. Tests use `StaticKeys` with a key pair they generate
themselves, so they never need a running Keycloak.

Every refused token is logged on the security channel with a short reason
(`expired`, `wrong_audience` ...). The API logs a request with no token the
same way (`missing`, `no_bearer_token`). The token itself is never logged, and
the user only ever sees "You need to sign in again." so the reason gives an
attacker nothing to work with.
"""

import asyncio
import hashlib
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import jwt
from jwt import PyJWK

from core.config import Settings
from core.errors import AuthError, AuthUnavailableError, PermissionDeniedError
from core.logging import SECURITY_LOGGER, get_logger
from security.roles import Role

security_log = get_logger(SECURITY_LOGGER)

# Keycloak signs access tokens with RS256 by default. Only the algorithms
# listed here are ever accepted.
ALLOWED_ALGORITHMS = ("RS256",)
# Real Keycloak tokens are 1-2 KB. Anything far bigger is refused unread.
MAX_TOKEN_CHARS = 8_192
REQUIRED_CLAIMS = ("exp", "iat", "sub", "iss", "aud")

NO_ROLE_MESSAGE = "Your account has no role in this app. Ask an administrator for access."

# Highest first. A user with several realm roles gets the highest one.
_ROLE_RANK = (Role.ADMIN, Role.ANALYST, Role.VIEWER)


@dataclass(frozen=True)
class Principal:
    """The signed-in user, as stated by a valid token."""

    user_id: str  # Keycloak's `sub`: stable, and never reused for another user
    username: str
    name: str
    role: Role

    @property
    def pseudonym(self) -> str:
        """A stable stand-in for the user ID, used in logs and traces.

        The same user always gets the same value, so their requests can be
        followed in the logs, but the value says nothing about who they are.
        """
        return pseudonymise(self.user_id)


def pseudonymise(user_id: str) -> str:
    return "u_" + hashlib.sha256(user_id.encode()).hexdigest()[:16]


def role_from_claims(claims: Mapping[str, Any]) -> Role | None:
    """The highest of our roles in the token's realm roles, or None."""
    realm_access = claims.get("realm_access")
    roles = realm_access.get("roles") if isinstance(realm_access, Mapping) else None
    if not isinstance(roles, list):
        return None
    names = {name for name in roles if isinstance(name, str)}
    return next((role for role in _ROLE_RANK if role.value in names), None)


# ---------------------------------------------------------------
# Where the signing keys come from
# ---------------------------------------------------------------


class KeySource(Protocol):
    async def key_for(self, kid: str) -> PyJWK | None:
        """The public key with this ID, or None if there is no such key."""
        ...

    async def aclose(self) -> None: ...


def parse_jwks(document: Mapping[str, Any]) -> dict[str, PyJWK]:
    """The signing keys in a JWKS document, by key ID.

    Keycloak also publishes an encryption key in the same document; keys
    that are not for signatures, or that PyJWT cannot use, are skipped.
    """
    keys: dict[str, PyJWK] = {}
    entries = document.get("keys")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, Mapping):
            continue
        kid = entry.get("kid")
        if not isinstance(kid, str) or entry.get("use", "sig") != "sig":
            continue
        if entry.get("alg", "RS256") not in ALLOWED_ALGORITHMS:
            continue
        try:
            keys[kid] = PyJWK(dict(entry))
        except jwt.PyJWTError:  # not a usable public key
            continue
    return keys


class StaticKeys:
    """A fixed set of keys. Used by tests, with a key pair they make themselves."""

    def __init__(self, jwks: Mapping[str, Any]) -> None:
        self._keys = parse_jwks(jwks)

    async def key_for(self, kid: str) -> PyJWK | None:
        return self._keys.get(kid)

    async def aclose(self) -> None:
        return None


class JwksClient:
    """Fetches Keycloak's public keys and keeps them for `cache_seconds`.

    A key ID that is not in memory makes the client fetch the keys again,
    because Keycloak may have rotated them. To stop a flood of tokens with
    made-up key IDs from turning into a flood of requests to Keycloak, that
    early fetch happens at most once every `min_refresh_seconds`.
    """

    def __init__(
        self,
        url: str,
        *,
        cache_seconds: float,
        min_refresh_seconds: float,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self.cache_seconds = cache_seconds
        self.min_refresh_seconds = min_refresh_seconds
        self._http = httpx.AsyncClient(timeout=timeout_seconds, transport=transport)
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()

    async def key_for(self, kid: str) -> PyJWK | None:
        if self._is_fresh() and kid in self._keys:
            return self._keys[kid]
        async with self._lock:
            # Another request may have fetched the keys while we waited.
            if self._is_fresh() and kid in self._keys:
                return self._keys[kid]
            if self._may_fetch():
                await self._fetch()
            return self._keys.get(kid)

    def _age(self) -> float:
        return float("inf") if self._fetched_at is None else time.monotonic() - self._fetched_at

    def _is_fresh(self) -> bool:
        return self._age() < self.cache_seconds

    def _may_fetch(self) -> bool:
        return not self._is_fresh() or self._age() >= self.min_refresh_seconds

    async def _fetch(self) -> None:
        try:
            response = await self._http.get(self.url)
            response.raise_for_status()
            keys = parse_jwks(response.json())
        except (httpx.HTTPError, ValueError) as error:
            security_log.error("jwks_fetch_failed", url=self.url, error=type(error).__name__)
            if not self._keys:
                raise AuthUnavailableError() from error
            # Keep using the keys we have; try again after the short wait.
            self._fetched_at = time.monotonic() - self.cache_seconds + self.min_refresh_seconds
            return
        self._keys = keys
        self._fetched_at = time.monotonic()
        security_log.info("jwks_fetched", keys=len(keys))

    async def aclose(self) -> None:
        await self._http.aclose()


# ---------------------------------------------------------------
# Checking a token
# ---------------------------------------------------------------


class TokenVerifier:
    def __init__(
        self, keys: KeySource, *, issuer: str, audience: str, leeway_seconds: int = 30
    ) -> None:
        self.keys = keys
        self.issuer = issuer
        self.audience = audience
        self.leeway_seconds = leeway_seconds

    @classmethod
    def from_settings(cls, settings: Settings, keys: KeySource | None = None) -> "TokenVerifier":
        keys = keys or JwksClient(
            settings.jwks_url,
            cache_seconds=settings.auth_jwks_cache_seconds,
            min_refresh_seconds=settings.auth_jwks_min_refresh_seconds,
            timeout_seconds=settings.auth_timeout_seconds,
        )
        return cls(
            keys,
            issuer=settings.auth_issuer,
            audience=settings.auth_audience,
            leeway_seconds=settings.auth_clock_skew_seconds,
        )

    async def verify(self, token: str) -> Principal:
        """The user the token belongs to. Raises AuthError if it is not valid."""
        claims = await self._checked_claims(token)
        role = role_from_claims(claims)
        user_id = claims["sub"]
        if role is None:
            security_log.warning("token_without_role", user=pseudonymise(str(user_id)))
            raise PermissionDeniedError(NO_ROLE_MESSAGE)
        username = claims.get("preferred_username")
        username = username if isinstance(username, str) and username else str(user_id)
        name = claims.get("name")
        return Principal(
            user_id=str(user_id),
            username=username,
            name=name if isinstance(name, str) and name else username,
            role=role,
        )

    async def _checked_claims(self, token: str) -> dict[str, Any]:
        if not token or len(token) > MAX_TOKEN_CHARS:
            raise reject("malformed")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError:
            raise reject("malformed") from None
        if header.get("alg") not in ALLOWED_ALGORITHMS:
            raise reject("algorithm_not_allowed", alg=str(header.get("alg"))[:20])
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise reject("no_key_id")
        key = await self.keys.key_for(kid)
        if key is None:
            raise reject("unknown_key")

        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=list(ALLOWED_ALGORITHMS),
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway_seconds,
                options={"require": list(REQUIRED_CLAIMS)},
            )
        except jwt.ExpiredSignatureError:
            raise reject("expired") from None
        except jwt.InvalidAudienceError:
            raise reject("wrong_audience") from None
        except jwt.InvalidIssuerError:
            raise reject("wrong_issuer") from None
        except jwt.InvalidSignatureError:
            raise reject("bad_signature") from None
        except jwt.MissingRequiredClaimError:
            raise reject("missing_claim") from None
        except jwt.InvalidTokenError:
            raise reject("invalid") from None

        # Keycloak marks access tokens with typ "Bearer". ID and refresh
        # tokens are signed by the same key but must not open the API.
        if claims.get("typ", "Bearer") != "Bearer":
            raise reject("not_an_access_token")
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise reject("missing_claim")
        return claims


def reject(reason: str, **details: str) -> AuthError:
    """Log why a request's login was refused, and return the 401 to raise.

    Used here for bad tokens, and by the API for requests with no token at
    all, so every refused login is one `token_rejected` line in the
    security log, with a short `reason`.
    """
    security_log.warning("token_rejected", reason=reason, **details)
    return AuthError()
