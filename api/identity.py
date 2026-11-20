"""Who is asking: the caller's role, and the key their conversations belong to.

There is no login yet; it arrives with `feature/auth-rbac`. Until then the
role is read from the `X-Dev-Role` header, for local use and tests only:

- It is refused when `APP_ENV=production`, so a deployed API can never be
  used without login.
- A missing header means `viewer`, the lowest role.
- The role is never read from the request body or the conversation
  (ADR-0007). `POST /chat` refuses a body with a `role` field.

`feature/auth-rbac` replaces `current_caller` with one that reads the
validated Keycloak token. The routes use the `Caller` it returns and do
not change.
"""

from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Header, Request

from core.config import Settings
from core.errors import AuthError, InvalidRequestError
from security.roles import Role

DEV_ROLE_HEADER = "X-Dev-Role"


@dataclass(frozen=True)
class Caller:
    role: Role
    # Who owns the conversations this caller starts. For now the role (all
    # local users of one role share it); the user ID from the token later.
    owner: str


async def current_caller(
    request: Request,
    dev_role: Annotated[
        str | None,
        Header(
            alias=DEV_ROLE_HEADER,
            description="Local development only, until login is added: viewer, analyst or admin.",
        ),
    ] = None,
) -> Caller:
    settings = cast(Settings, request.app.state.settings)
    if settings.is_production:
        raise AuthError("Sign-in is required.")
    if dev_role is None:
        role = Role.VIEWER
    else:
        try:
            role = Role(dev_role.strip().lower())
        except ValueError:
            raise InvalidRequestError(
                f"{DEV_ROLE_HEADER} must be one of: {', '.join(Role)}."
            ) from None
    return Caller(role=role, owner=f"role:{role.value}")
