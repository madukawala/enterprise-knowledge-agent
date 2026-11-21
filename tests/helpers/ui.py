"""Test helpers for the Streamlit UI: real API replies, played back to the UI.

The UI's tests do not invent the stream format. They run the real API
(with scripted agents), record what it sends, and play it back to the UI's
HTTP client. If the API's events change in a way the UI does not handle,
these tests fail.

    reply = record(chat_app(settings, answering_model("Raise the pool [1].")), "pool?")
    client = ChatClient(UiSettings(), transport=playback(reply))
"""

import asyncio
import time
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from streamlit.testing.v1 import AppTest

from api.main import create_app
from core.config import Environment, Settings
from security.roles import Role
from tests.helpers.sse import post_stream
from tests.helpers.tokens import TOKENS, bearer
from ui.auth import Session
from ui.client import ChatClient, Profile
from ui.config import UiSettings

APP_FILE = Path(__file__).parents[2] / "ui" / "app.py"
API_URL = "http://localhost:8000"


def record(
    app: FastAPI,
    message: str,
    *,
    thread_id: str | None = None,
    headers: Mapping[str, str] | None = None,
    role: Role | None = Role.VIEWER,
) -> httpx.Response:
    """Ask the real API one question, signed in as `role`, and keep its whole reply."""
    body: dict[str, Any] = {"message": message}
    if thread_id is not None:
        body["thread_id"] = thread_id
    reply = asyncio.run(post_stream(app, "/chat", body, headers=headers, role=role))
    return httpx.Response(reply.status, headers=reply.headers, content=reply.raw.encode())


def replay(response: httpx.Response) -> httpx.Response:
    """A fresh copy of a recorded reply, so it can be read again."""
    return httpx.Response(response.status_code, headers=response.headers, content=response.content)


class Playback(httpx.MockTransport):
    """Answers each request with the next recorded reply, and keeps the requests."""

    def __init__(self, *replies: httpx.Response) -> None:
        self.requests: list[httpx.Request] = []
        self._replies = list(replies)
        super().__init__(self._answer)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return replay(self._replies.pop(0))


def record_me(app: FastAPI, role: Role) -> httpx.Response:
    """The real API's `GET /me` reply for the test user with `role`."""

    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=API_URL) as http:
            response = await http.get("/me", headers=bearer(role))
            await response.aread()
            return response

    reply = asyncio.run(call())
    return httpx.Response(reply.status_code, headers=reply.headers, content=reply.content)


def profile_for(app: FastAPI, role: Role) -> Profile:
    """The profile the UI builds from the real `GET /me` reply."""
    return ChatClient(UiSettings(), transport=Playback(record_me(app, role))).me("token")


def signed_in_session(role: Role = Role.VIEWER) -> Session:
    """A UI session holding a real test token that will not need a refresh."""
    return Session(
        access_token=TOKENS.token(role),
        refresh_token="test-refresh-token",
        expires_at=time.time() + 3600,
        refresh_expires_at=time.time() + 7200,
    )


@cache
def profile_of(role: Role) -> Profile:
    """What the real API says about the test user with `role` (GET /me)."""
    settings = Settings(_env_file=None, app_env=Environment.TEST)
    return profile_for(create_app(settings, signing_keys=TOKENS.signing_keys), role)


def page_for(role: Role | None = Role.VIEWER) -> AppTest:
    """The Streamlit page, already signed in as `role` (or at the sign-in form for None)."""
    page = AppTest.from_file(str(APP_FILE), default_timeout=30)
    if role is not None:
        page.session_state["session"] = signed_in_session(role)
        page.session_state["profile"] = profile_of(role)
    return page.run()
