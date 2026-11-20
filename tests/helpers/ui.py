"""Test helpers for the Streamlit UI: real API replies, played back to the UI.

The UI's tests do not invent the stream format. They run the real API
(with scripted agents), record what it sends, and play it back to the UI's
HTTP client. If the API's events change in a way the UI does not handle,
these tests fail.

    reply = record(chat_app(settings, answering_model("Raise the pool [1].")), "pool?")
    client = ChatClient(UiSettings(), transport=playback(reply))
"""

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from tests.helpers.sse import post_stream

APP_FILE = Path(__file__).parents[2] / "ui" / "app.py"
API_URL = "http://localhost:8000"


def record(
    app: FastAPI,
    message: str,
    *,
    thread_id: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """Ask the real API one question and keep its whole reply."""
    body: dict[str, Any] = {"message": message}
    if thread_id is not None:
        body["thread_id"] = thread_id
    reply = asyncio.run(post_stream(app, "/chat", body, headers=headers))
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
