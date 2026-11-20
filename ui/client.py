"""Talks to the API: sends a question and reads the answer as it streams.

    client = ChatClient(settings)
    with closing(client.ask("Why do card payments fail?", role="analyst")) as stream:
        for item in stream:
            if isinstance(item, StreamOpened):
                ...  # the thread ID for follow-up questions, and the run ID
            elif isinstance(item, KeepAlive):
                ...  # nothing new yet; the API is still working
            else:
                ...  # one event, as a dict with a "type"

Anything that goes wrong is raised as `ApiError`, with a message that is
safe to show to the user:

- the API refused the question (404, 409, 422, 503 ...): the API's own message;
- the API cannot be reached, or the connection dropped half-way.

Closing the stream early closes the connection, and the API then stops
the run (see api/sse.py). The UI relies on this when the user presses Stop.
"""

import json
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any

import httpx

from ui.config import UiSettings
from ui.sse import KeepAlive, read_sse

# Until login is added (`feature/auth-rbac`) the API takes the role from
# this header. A production API refuses it.
DEV_ROLE_HEADER = "X-Dev-Role"

Event = dict[str, Any]

# Shown when the API's error has no message of its own (for example when a
# proxy answered instead of the API).
_STATUS_MESSAGES = {
    401: "You need to sign in again.",
    404: "This conversation was not found. Please start a new one.",
    409: "This conversation is still answering the previous question. Please wait.",
    422: "The question could not be sent. Please check it and try again.",
    503: "The assistant is not available right now. Please try again later.",
}


class ApiError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.request_id = request_id


@dataclass(frozen=True)
class StreamOpened:
    """The API accepted the question. Comes before the first event."""

    thread_id: str
    run_id: str | None
    request_id: str | None


type StreamItem = StreamOpened | KeepAlive | Event


class ChatClient:
    def __init__(
        self, settings: UiSettings, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._settings = settings
        # Tests pass a fake transport; the app uses the real network.
        self._transport = transport

    def ask(
        self, message: str, *, role: str, thread_id: str | None = None
    ) -> Generator[StreamItem]:
        body: dict[str, str] = {"message": message}
        if thread_id:
            body["thread_id"] = thread_id
        headers = {DEV_ROLE_HEADER: role, "Accept": "text/event-stream"}

        opened = False
        try:
            with (
                self._http() as http,
                http.stream("POST", "/chat", json=body, headers=headers) as response,
            ):
                if response.status_code != httpx.codes.OK:
                    response.read()
                    raise _refused(response)
                opened = True
                yield StreamOpened(
                    thread_id=response.headers.get("x-thread-id", thread_id or ""),
                    run_id=response.headers.get("x-run-id"),
                    request_id=response.headers.get("x-request-id"),
                )
                for item in read_sse(response.iter_lines()):
                    yield item if isinstance(item, KeepAlive) else _event(item.data)
        except httpx.TimeoutException as error:
            raise ApiError(
                "The assistant took too long to respond. Please try again."
                if opened
                else f"The assistant at {self._settings.api_url} did not answer in time."
            ) from error
        except httpx.TransportError as error:
            raise ApiError(
                "The connection to the assistant was lost before the answer was finished."
                if opened
                else f"Cannot reach the assistant at {self._settings.api_url}. Is the API running?"
            ) from error

    def is_up(self) -> bool:
        """True when the API answers its liveness check."""
        try:
            with self._http() as http:
                return http.get("/healthz").status_code == httpx.codes.OK
        except httpx.HTTPError:
            return False

    def _http(self) -> httpx.Client:
        settings = self._settings
        return httpx.Client(
            base_url=settings.api_url,
            transport=self._transport,
            # `read` is the longest wait between two pieces of the stream.
            timeout=httpx.Timeout(settings.read_timeout, connect=settings.connect_timeout),
        )


def _event(data: str) -> Event:
    try:
        event = json.loads(data)
    except json.JSONDecodeError:
        event = None
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise ApiError("The assistant sent a reply the app could not read.")
    return event


def _refused(response: httpx.Response) -> ApiError:
    """The API's own error, when it sent one: {"error": {"code", "message", "request_id"}}."""
    try:
        error = response.json().get("error") or {}
    except (json.JSONDecodeError, AttributeError):
        error = {}
    if not isinstance(error, dict):
        error = {}
    status = response.status_code
    message = error.get("message") or _STATUS_MESSAGES.get(
        status, f"The assistant could not take the question (HTTP {status})."
    )
    return ApiError(
        str(message),
        status=status,
        code=error.get("code"),
        request_id=error.get("request_id") or response.headers.get("x-request-id"),
    )
