"""Signs the user in with Keycloak and keeps their tokens fresh.

    login = KeycloakLogin(settings)
    session = login.sign_in("adam.analyst", "analyst-demo-pass")
    ...
    session = login.fresh(session)   # before each API call
    login.sign_out(session)

How it works: the sign-in form sends the username and password to
Keycloak's token endpoint from the UI server (the OAuth "password" grant
for the UI's public client). Keycloak answers with a short-lived access
token (5 minutes) and a refresh token. The UI keeps both in the Streamlit
session, which lives on the UI server and never reaches the browser, and
sends the access token to the API with every request. Shortly before the
access token expires, the refresh token is used to get a new one.

Why the password grant and not a browser redirect: it keeps the demo to one
small Streamlit page with no callback route. The realm already has the
redirect flow with PKCE switched on for this client, so a deployment can
move to it without changing the API. See docs/ASSUMPTIONS.md.

The UI never decides what the user may do. It reads the role from the API
(`GET /me`) only to show it and to hide tools the role cannot use.
"""

import time
from dataclasses import dataclass
from typing import Any

import httpx

from ui.config import UiSettings

# Refresh this many seconds before the access token expires, so a token
# never runs out half-way through a request.
REFRESH_MARGIN_SECONDS = 30

BAD_CREDENTIALS = "Wrong username or password."
SESSION_ENDED = "Your session has ended. Please sign in again."


class LoginError(Exception):
    """Signing in or refreshing failed. `message` is safe to show to the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Session:
    access_token: str
    refresh_token: str | None
    # Unix times (seconds).
    expires_at: float
    refresh_expires_at: float | None

    def needs_refresh(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return now >= self.expires_at - REFRESH_MARGIN_SECONDS

    def __repr__(self) -> str:
        # Never print the tokens, even by accident in a log or error page.
        return f"Session(expires_at={self.expires_at:.0f})"


class KeycloakLogin:
    def __init__(
        self, settings: UiSettings, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport
        realm = f"{settings.keycloak_url}/realms/{settings.keycloak_realm}"
        self.token_url = f"{realm}/protocol/openid-connect/token"
        self.logout_url = f"{realm}/protocol/openid-connect/logout"

    def sign_in(self, username: str, password: str) -> Session:
        if not username.strip() or not password:
            raise LoginError("Enter your username and password.")
        return self._token(
            {"grant_type": "password", "username": username.strip(), "password": password},
            refused=BAD_CREDENTIALS,
        )

    def fresh(self, session: Session) -> Session:
        """The same session if its access token is still good, or a refreshed one."""
        if not session.needs_refresh():
            return session
        now = time.time()
        if session.refresh_token is None or (
            session.refresh_expires_at is not None and now >= session.refresh_expires_at
        ):
            raise LoginError(SESSION_ENDED)
        return self._token(
            {"grant_type": "refresh_token", "refresh_token": session.refresh_token},
            refused=SESSION_ENDED,
        )

    def sign_out(self, session: Session) -> None:
        """End the session in Keycloak too, so the refresh token stops working.

        Best effort: if Keycloak cannot be reached, the UI forgets the tokens
        anyway, and the access token runs out within minutes.
        """
        if session.refresh_token is None:
            return
        try:
            with self._http() as http:
                http.post(
                    self.logout_url,
                    data={
                        "client_id": self._settings.keycloak_client_id,
                        "refresh_token": session.refresh_token,
                    },
                )
        except httpx.HTTPError:
            pass

    def _token(self, form: dict[str, str], *, refused: str) -> Session:
        data = {"client_id": self._settings.keycloak_client_id, "scope": "openid", **form}
        started = time.time()
        try:
            with self._http() as http:
                response = http.post(self.token_url, data=data)
        except httpx.HTTPError as error:
            raise LoginError(
                f"Cannot reach the sign-in service at {self._settings.keycloak_url}. "
                "Is Keycloak running?"
            ) from error
        if response.status_code in (400, 401):
            # Keycloak says why (wrong password, account locked ...), but the
            # user gets one message, so the form does not reveal which
            # usernames exist.
            raise LoginError(refused)
        if response.status_code != httpx.codes.OK:
            raise LoginError("Sign-in is unavailable right now. Please try again shortly.")
        return _session(response, started)

    def _http(self) -> httpx.Client:
        return httpx.Client(
            transport=self._transport,
            timeout=httpx.Timeout(
                self._settings.read_timeout, connect=self._settings.connect_timeout
            ),
        )


def _session(response: httpx.Response, started: float) -> Session:
    try:
        body: dict[str, Any] = response.json()
        access_token = body["access_token"]
        expires_in = float(body["expires_in"])
    except (ValueError, KeyError, TypeError) as error:
        raise LoginError("The sign-in service sent a reply the app could not read.") from error
    if not isinstance(access_token, str) or not access_token:
        raise LoginError("The sign-in service sent a reply the app could not read.")
    refresh_token = body.get("refresh_token")
    refresh_in = body.get("refresh_expires_in")
    return Session(
        access_token=access_token,
        refresh_token=refresh_token if isinstance(refresh_token, str) else None,
        # Counted from when the request was sent, to stay on the safe side.
        expires_at=started + expires_in,
        refresh_expires_at=started + float(refresh_in) if refresh_in else None,
    )
