"""Calls a streaming endpoint the way a browser does, and reads the SSE events.

httpx's test transport waits for the whole response before returning it,
so it cannot test what happens when the client leaves half-way. This
helper talks to the ASGI app directly: it sends the request, collects the
response piece by piece, and can disconnect as soon as a condition is met.

    reply = await post_stream(app, "/chat", {"message": "hi"},
                              disconnect_when=lambda events: len(events) >= 3)
    reply.status, reply.headers["x-thread-id"], reply.events

The request is signed in as the test viewer unless `role` says otherwise.
Pass `role=None` to send no token at all, or your own `Authorization`
header to send a particular one.
"""

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from starlette.types import ASGIApp, Message

from security.roles import Role
from tests.helpers.tokens import bearer


@dataclass
class StreamedReply:
    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    raw: str = ""

    @property
    def events(self) -> list[dict[str, Any]]:
        return parse_events(self.raw)

    @property
    def types(self) -> list[str]:
        return [event["type"] for event in self.events]

    @property
    def keepalives(self) -> int:
        return self.raw.count(": keep-alive")


def parse_events(raw: str) -> list[dict[str, Any]]:
    """The `data` of every event, checked against its `id` and `event` lines."""
    events = []
    for block in raw.split("\n\n"):
        lines = [line for line in block.split("\n") if line and not line.startswith(":")]
        if not lines:
            continue
        fields = dict(line.split(": ", 1) for line in lines)
        data = json.loads(fields["data"])
        assert fields["event"] == data["type"]
        assert int(fields["id"]) == data["seq"]
        events.append(data)
    return events


async def post_stream(
    app: ASGIApp,
    path: str,
    body: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    role: Role | None = Role.VIEWER,
    disconnect_when: Callable[[Sequence[dict[str, Any]]], bool] | None = None,
    give_up_after: float = 5.0,
) -> StreamedReply:
    reply = StreamedReply()
    payload = json.dumps(body).encode()
    request_sent = False
    disconnected = asyncio.Event()

    login = bearer(role) if role is not None else {}
    all_headers = {"content-type": "application/json", **login, **(headers or {})}
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in all_headers.items()],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "state": {},
    }

    async def receive() -> Message:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            reply.status = message["status"]
            reply.headers = {k.decode().lower(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            reply.raw += message.get("body", b"").decode()
            if disconnect_when is not None and disconnect_when(reply.events):
                disconnected.set()

    async with asyncio.timeout(give_up_after):
        await app(scope, receive, send)  # type: ignore[arg-type]
    return reply
