"""Checks that the shared test helpers behave as other tests expect."""

from collections.abc import Callable, Iterable

import jwt
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from security.roles import Role
from tests.helpers.tokens import TEST_AUDIENCE, TEST_ISSUER, TokenFactory

FakeLLM = Callable[[Iterable[str | AIMessage]], GenericFakeChatModel]


def _decode(factory: TokenFactory, token: str) -> dict[str, object]:
    key = jwt.PyJWKSet.from_dict(factory.jwks).keys[0]
    claims: dict[str, object] = jwt.decode(
        token, key, algorithms=["RS256"], audience=TEST_AUDIENCE, issuer=TEST_ISSUER
    )
    return claims


@pytest.mark.parametrize("role", list(Role))
def test_tokens_carry_the_role_and_verify_with_the_jwks(
    token_factory: TokenFactory, role: Role
) -> None:
    claims = _decode(token_factory, token_factory.token(role))

    assert claims["realm_access"] == {"roles": [role.value]}
    assert claims["sub"]


def test_role_fixtures_give_one_token_per_role(
    token_factory: TokenFactory, viewer_token: str, analyst_token: str, admin_token: str
) -> None:
    roles = [
        _decode(token_factory, token)["realm_access"]
        for token in (viewer_token, analyst_token, admin_token)
    ]

    assert roles == [{"roles": ["viewer"]}, {"roles": ["analyst"]}, {"roles": ["admin"]}]


def test_expired_tokens_are_rejected(token_factory: TokenFactory) -> None:
    token = token_factory.token(Role.VIEWER, expires_in=-60)

    with pytest.raises(jwt.ExpiredSignatureError):
        _decode(token_factory, token)


def test_tokens_from_another_key_are_rejected(token_factory: TokenFactory) -> None:
    token = TokenFactory().token(Role.ADMIN)

    with pytest.raises(jwt.InvalidSignatureError):
        _decode(token_factory, token)


async def test_fake_llm_replies_in_order(fake_llm: FakeLLM) -> None:
    model = fake_llm(["first answer", "second answer"])

    first = await model.ainvoke("question one")
    second = await model.ainvoke("question two")

    assert (first.content, second.content) == ("first answer", "second answer")
