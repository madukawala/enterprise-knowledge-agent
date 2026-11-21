"""Request context middleware.

For every HTTP request this middleware:

- picks the request ID: the caller's `X-Request-ID` if it is safe, otherwise a new one;
- binds it to the logging context, so every log line for the request carries it;
- returns it in the `X-Request-ID` response header;
- logs one line when the request finishes, with the status and duration.

Once the login token has been checked (api/identity.py), the pseudonymised
user and the role are bound to the same logging context, so the
`request_finished` line and every line before it say who asked.

It is plain ASGI middleware rather than Starlette's `BaseHTTPMiddleware`,
so it does not buffer streaming responses (the chat endpoint streams with SSE).
"""

import re
import time
import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.logging import get_logger

REQUEST_ID_HEADER = "X-Request-ID"

# Caller-supplied IDs are accepted only if they are short and plain, so they
# cannot be used to inject text into headers or log lines.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Logging fields that belong to one request and are cleared when it ends.
# `user` and `role` are bound by api/identity.py after the token is checked.
_REQUEST_LOG_FIELDS = ("request_id", "user", "role")

logger = get_logger(__name__)


def resolve_request_id(incoming: str | None) -> str:
    """Return the caller's request ID if it is safe, otherwise a new random one."""
    if incoming and _SAFE_REQUEST_ID.fullmatch(incoming):
        return incoming
    return uuid.uuid4().hex


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        incoming = headers.get(REQUEST_ID_HEADER.lower().encode("latin-1"))
        request_id = resolve_request_id(incoming.decode("latin-1") if incoming else None)

        # Exception handlers read it from request.state, because they can run
        # after this middleware has finished (see api/errors.py).
        scope.setdefault("state", {})["request_id"] = request_id

        status_code = 500
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            # Logged here, while the request context is still bound. The
            # generic exception handler then returns a safe 500 response.
            logger.exception("request_failed", method=scope["method"], path=scope["path"])
            raise
        finally:
            logger.info(
                "request_finished",
                method=scope["method"],
                path=scope["path"],
                status_code=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            structlog.contextvars.unbind_contextvars(*_REQUEST_LOG_FIELDS)
