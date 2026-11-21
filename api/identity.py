"""Who is asking: the signed-in user, read from their Keycloak access token.

Every route that needs a user depends on `current_caller`. It reads the
`Authorization: Bearer <token>` header, has security/auth.py check the
token, and returns a `Caller`. There is no other way in:

- No token, or a token that fails any check: 401 "You need to sign in again."
  Both are logged as `token_rejected` on the security channel, with a
  reason such as `missing`, `no_bearer_token` or `expired`.
- A valid token for a user with none of our roles: 403.
- The role is never read from the request body, another header or the
  conversation (ADR-0007). `POST /chat` refuses a body with a `role` field.

Routes that need a particular permission add `requires(Action.X)` as a
dependency, which checks the role against security/policies.py.

Once the caller is known, their pseudonymised user ID and role are added to
every log line for the rest of the request.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, cast

import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from security.auth import Principal, TokenVerifier, reject
from security.policies import Action, require
from security.roles import Role

# auto_error=False: a missing header is answered by our own AuthError, with
# the same JSON shape as every other error, instead of FastAPI's default.
_bearer = HTTPBearer(
    auto_error=False,
    description="A Keycloak access token for the helix realm.",
)


@dataclass(frozen=True)
class Caller:
    user_id: str
    username: str
    name: str
    role: Role
    # Logged instead of the user ID (see Principal.pseudonym).
    pseudonym: str

    @property
    def owner(self) -> str:
        """The key the caller's conversations belong to: their user ID.

        Two users with the same role do not share conversations.
        """
        return self.user_id

    @classmethod
    def from_principal(cls, principal: Principal) -> "Caller":
        return cls(
            user_id=principal.user_id,
            username=principal.username,
            name=principal.name,
            role=principal.role,
            pseudonym=principal.pseudonym,
        )


async def current_caller(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Caller:
    if credentials is None:
        # No usable header at all, or another scheme such as Basic. Logged
        # like a bad token, so probing without one shows up too.
        has_header = bool(request.headers.get("authorization", "").strip())
        raise reject("no_bearer_token" if has_header else "missing")
    verifier = cast(TokenVerifier, request.app.state.token_verifier)
    caller = Caller.from_principal(await verifier.verify(credentials.credentials))
    structlog.contextvars.bind_contextvars(user=caller.pseudonym, role=caller.role.value)
    return caller


CurrentCaller = Annotated[Caller, Depends(current_caller)]


def requires(action: Action) -> Callable[[Caller], Awaitable[Caller]]:
    """A route dependency: the caller must be signed in and allowed to do `action`.

    @router.get("/admin/x")
    async def x(caller: Annotated[Caller, Depends(requires(Action.VIEW_AUDIT_LOG))]): ...
    """

    async def check(caller: CurrentCaller) -> Caller:
        require(caller.role, action, user=caller.pseudonym)
        return caller

    return check
