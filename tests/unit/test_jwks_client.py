"""Fetching and caching Keycloak's signing keys (security/auth.py: JwksClient)."""

from collections.abc import Callable

import httpx
import pytest

from core.errors import AuthUnavailableError
from security.auth import JwksClient, parse_jwks
from tests.helpers.tokens import TEST_KEY_ID, TOKENS

JWKS_URL = "http://keycloak.test/realms/helix/protocol/openid-connect/certs"


class FakeKeycloak:
    """Serves the test JWKS and counts the requests. Can be switched off."""

    def __init__(self) -> None:
        self.calls = 0
        self.up = True
        self.document = TOKENS.jwks

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if not self.up:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=self.document)


def _client(
    keycloak: Callable[[httpx.Request], httpx.Response],
    *,
    cache_seconds: float = 300,
    min_refresh_seconds: float = 30,
) -> JwksClient:
    return JwksClient(
        JWKS_URL,
        cache_seconds=cache_seconds,
        min_refresh_seconds=min_refresh_seconds,
        timeout_seconds=1,
        transport=httpx.MockTransport(keycloak),
    )


async def test_keys_are_fetched_once_and_then_served_from_memory() -> None:
    keycloak = FakeKeycloak()
    client = _client(keycloak)

    for _ in range(5):
        assert await client.key_for(TEST_KEY_ID) is not None

    assert keycloak.calls == 1
    await client.aclose()


async def test_an_unknown_key_id_refetches_only_after_the_short_wait() -> None:
    """A flood of made-up key IDs must not become a flood of calls to Keycloak."""
    keycloak = FakeKeycloak()
    client = _client(keycloak, min_refresh_seconds=3600)

    await client.key_for(TEST_KEY_ID)
    for n in range(10):
        assert await client.key_for(f"made-up-{n}") is None

    assert keycloak.calls == 1


async def test_a_rotated_key_is_picked_up_without_a_restart() -> None:
    keycloak = FakeKeycloak()
    client = _client(keycloak, min_refresh_seconds=0)
    await client.key_for(TEST_KEY_ID)

    rotated = dict(TOKENS.jwks["keys"][0], kid="rotated-key")
    keycloak.document = {"keys": [rotated]}

    assert await client.key_for("rotated-key") is not None
    assert keycloak.calls == 2


async def test_keys_already_in_memory_keep_working_while_keycloak_is_down() -> None:
    keycloak = FakeKeycloak()
    client = _client(keycloak, cache_seconds=10, min_refresh_seconds=1)
    await client.key_for(TEST_KEY_ID)
    keycloak.up = False
    client._fetched_at = 0  # make the cache stale

    assert await client.key_for(TEST_KEY_ID) is not None


async def test_no_keys_at_all_means_sign_in_is_unavailable() -> None:
    keycloak = FakeKeycloak()
    keycloak.up = False

    with pytest.raises(AuthUnavailableError):
        await _client(keycloak).key_for(TEST_KEY_ID)


def test_only_usable_signing_keys_are_kept() -> None:
    signing = TOKENS.jwks["keys"][0]
    document = {
        "keys": [
            signing,
            # Keycloak also publishes an encryption key in the same list.
            {**signing, "kid": "enc-key", "use": "enc", "alg": "RSA-OAEP"},
            {**signing, "kid": "hmac", "alg": "HS256"},
            {"kid": "broken", "kty": "RSA"},
            {"kty": "RSA"},  # no kid
            "not a key",
        ]
    }
    assert set(parse_jwks(document)) == {TEST_KEY_ID}


@pytest.mark.parametrize("document", [{}, {"keys": "nope"}, {"keys": []}])
def test_an_empty_or_odd_document_gives_no_keys(document: dict[str, object]) -> None:
    assert parse_jwks(document) == {}
