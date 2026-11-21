"""Talks to the API: sends a question and reads the answer as it streams.

    client = ChatClient(settings)
    profile = client.me(token)       # who is signed in, and what they may use
    with closing(client.ask("Why do card payments fail?", token=token)) as stream:
        for item in stream:
            if isinstance(item, StreamOpened):
                ...  # the thread ID for follow-up questions, and the run ID
            elif isinstance(item, KeepAlive):
                ...  # nothing new yet; the API is still working
            else:
                ...  # one event, as a dict with a "type"

Anything that goes wrong is raised as `ApiError`, with a message that is
safe to show to the user:

- the API refused the question (401, 403, 404, 409, 422, 503 ...): the API's
  own message. A 401 means the login token is no longer accepted, and the
  page asks the user to sign in again;
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

Event = dict[str, Any]

# Shown when the API's error has no message of its own (for example when a
# proxy answered instead of the API).
_STATUS_MESSAGES = {
    401: "You need to sign in again.",
    403: "Your role does not allow this action.",
    404: "This conversation was not found. Please start a new one.",
    409: "This conversation is still answering the previous question. Please wait.",
    429: "You are asking questions too quickly. Please wait a moment and try again.",
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
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.request_id = request_id
        # Seconds to wait before asking again, from Retry-After on a 429.
        self.retry_after = retry_after


@dataclass(frozen=True)
class StreamOpened:
    """The API accepted the question. Comes before the first event."""

    thread_id: str
    run_id: str | None
    request_id: str | None


type StreamItem = StreamOpened | KeepAlive | Event


@dataclass(frozen=True)
class Permission:
    action: str
    label: str
    allowed: bool


@dataclass(frozen=True)
class SavedThread:
    """A conversation saved on the server (`GET /threads`)."""

    thread_id: str
    title: str


@dataclass(frozen=True)
class Budget:
    """Today's language model tokens: used, the daily limit, and seconds until it resets."""

    used: int
    limit: int
    resets_in_seconds: int

    @property
    def share_used(self) -> float:
        return min(1.0, self.used / self.limit) if self.limit else 1.0


@dataclass(frozen=True)
class Profile:
    """The signed-in user, as the API sees them (`GET /me`)."""

    username: str
    name: str
    role: str
    permissions: tuple[Permission, ...]
    access_levels: tuple[str, ...]
    budget: Budget | None = None

    @property
    def allowed(self) -> tuple[Permission, ...]:
        return tuple(p for p in self.permissions if p.allowed)


class ChatClient:
    def __init__(
        self, settings: UiSettings, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._settings = settings
        # Tests pass a fake transport; the app uses the real network.
        self._transport = transport

    def ask(
        self, message: str, *, token: str, thread_id: str | None = None
    ) -> Generator[StreamItem]:
        body: dict[str, str] = {"message": message}
        if thread_id:
            body["thread_id"] = thread_id
        return self._stream("/chat", body, token=token, thread_id=thread_id)

    def approve(self, thread_id: str, *, approve: bool, token: str) -> Generator[StreamItem]:
        """Answer a run that stopped to ask (`approval_required`); streams the rest of it."""
        return self._stream(
            f"/approvals/{thread_id}", {"approve": approve}, token=token, thread_id=thread_id
        )

    def feedback(
        self, *, token: str, run_id: str, thread_id: str, score: str, comment: str | None = None
    ) -> None:
        """Thumbs up or down on one answer (`POST /feedback`)."""
        body = {"run_id": run_id, "thread_id": thread_id, "score": score}
        if comment:
            body["comment"] = comment
        try:
            with self._http() as http:
                response = http.post("/feedback", json=body, headers=_login(token))
        except httpx.HTTPError as error:
            raise ApiError(
                f"Cannot reach the assistant at {self._settings.api_url}. Is the API running?"
            ) from error
        if response.status_code != httpx.codes.CREATED:
            raise _refused(response)

    def _stream(
        self, path: str, body: dict[str, Any], *, token: str, thread_id: str | None
    ) -> Generator[StreamItem]:
        headers = {**_login(token), "Accept": "text/event-stream"}

        opened = False
        try:
            with (
                self._http() as http,
                http.stream("POST", path, json=body, headers=headers) as response,
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

    def me(self, token: str) -> Profile:
        """Who the token belongs to, their role and what they may use."""
        try:
            with self._http() as http:
                response = http.get("/me", headers=_login(token))
        except httpx.HTTPError as error:
            raise ApiError(
                f"Cannot reach the assistant at {self._settings.api_url}. Is the API running?"
            ) from error
        if response.status_code != httpx.codes.OK:
            raise _refused(response)
        try:
            return _profile(response.json())
        except (ValueError, KeyError, TypeError) as error:
            raise ApiError("The assistant sent a reply the app could not read.") from error

    # --- saved conversations and memory ---

    def threads(self, token: str) -> list[SavedThread]:
        """The user's saved conversations, most recent first (`GET /threads`)."""
        body = self._get_json("/threads", token)
        return [
            SavedThread(thread_id=str(t["thread_id"]), title=str(t["title"]))
            for t in body
            if isinstance(t, dict)
        ]

    def thread(self, token: str, thread_id: str) -> list[tuple[str, str]]:
        """One conversation's messages as (role, text), oldest first."""
        body = self._get_json(f"/threads/{thread_id}", token)
        return [(str(m["role"]), str(m["text"])) for m in body.get("messages", [])]

    def memory(self, token: str) -> list[str]:
        """What the assistant remembers about the user: one line per item."""
        body = self._get_json("/me/memory", token)
        return [str(item["text"]) for item in body.get("interactions", [])]

    def forget(self, token: str) -> int:
        """Clear the user's long-term memory. Returns how many items were deleted."""
        try:
            with self._http() as http:
                response = http.delete("/me/memory", headers=_login(token))
        except httpx.HTTPError as error:
            raise ApiError(
                f"Cannot reach the assistant at {self._settings.api_url}. Is the API running?"
            ) from error
        if response.status_code != httpx.codes.OK:
            raise _refused(response)
        return int(response.json().get("deleted", 0))

    def _get_json(self, path: str, token: str) -> Any:
        try:
            with self._http() as http:
                response = http.get(path, headers=_login(token))
        except httpx.HTTPError as error:
            raise ApiError(
                f"Cannot reach the assistant at {self._settings.api_url}. Is the API running?"
            ) from error
        if response.status_code != httpx.codes.OK:
            raise _refused(response)
        try:
            return response.json()
        except ValueError as error:
            raise ApiError("The assistant sent a reply the app could not read.") from error

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


def _login(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _profile(body: dict[str, Any]) -> Profile:
    return Profile(
        username=str(body["username"]),
        name=str(body["name"]),
        role=str(body["role"]),
        permissions=tuple(
            Permission(action=str(p["action"]), label=str(p["label"]), allowed=bool(p["allowed"]))
            for p in body["permissions"]
        ),
        access_levels=tuple(str(level) for level in body["access_levels"]),
        budget=_budget(body.get("budget")),
    )


def _budget(body: Any) -> Budget | None:
    if not isinstance(body, dict):
        return None
    return Budget(
        used=int(body["used"]),
        limit=int(body["limit"]),
        resets_in_seconds=int(body["resets_in_seconds"]),
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
    retry_after = response.headers.get("retry-after", "")
    return ApiError(
        str(message),
        status=status,
        code=error.get("code"),
        request_id=error.get("request_id") or response.headers.get("x-request-id"),
        retry_after=int(retry_after) if retry_after.isdigit() else None,
    )
