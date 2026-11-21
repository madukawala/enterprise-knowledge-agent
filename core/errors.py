"""The application's error types.

Code raises one of these errors when something goes wrong in a way the user
should hear about. The API turns each error into a JSON response with a
stable `code`, a safe `message` and the request ID (see api/errors.py).

The `message` is shown to the user, so it must never contain internal
details such as stack traces, SQL, file paths or other users' data. Put
those in the log instead.
"""

from typing import Any, ClassVar


class AppError(Exception):
    """Base class for errors that can be shown to the user."""

    status_code: ClassVar[int] = 500
    code: ClassVar[str] = "internal_error"
    default_message: ClassVar[str] = "Something went wrong. Please try again later."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.message = message or self.default_message
        self.details = details or {}
        # Extra response headers, for example Retry-After on a 429.
        self.headers = headers or {}
        super().__init__(self.message)


class AuthError(AppError):
    status_code = 401
    code = "unauthorized"
    default_message = "You need to sign in again."


class AuthUnavailableError(AppError):
    """Keycloak's signing keys could not be fetched, so no token can be checked."""

    status_code = 503
    code = "auth_unavailable"
    default_message = "Sign-in is unavailable right now. Please try again shortly."


class PermissionDeniedError(AppError):
    status_code = 403
    code = "forbidden"
    default_message = "Your role does not allow this action."


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    default_message = "The requested item was not found."


class InvalidRequestError(AppError):
    status_code = 422
    code = "invalid_request"
    default_message = "The request is not valid."


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    default_message = "The request conflicts with one that is still running."


class RateLimitError(AppError):
    status_code = 429
    code = "rate_limited"
    default_message = "Too many requests. Please wait a moment and try again."

    def __init__(
        self, message: str | None = None, *, retry_after: int = 1, limit: str = "requests"
    ) -> None:
        # Whole seconds, at least 1, as the Retry-After header requires.
        self.retry_after = max(1, retry_after)
        super().__init__(
            message,
            details={"retry_after_seconds": self.retry_after, "limit": limit},
            headers={"Retry-After": str(self.retry_after)},
        )


class ToolError(AppError):
    status_code = 502
    code = "tool_error"
    default_message = "A tool needed for this answer did not respond correctly."


class ServiceUnavailableError(AppError):
    status_code = 503
    code = "service_unavailable"
    default_message = "This service is unavailable right now. Please try again shortly."


class RetrievalError(AppError):
    status_code = 503
    code = "retrieval_unavailable"
    default_message = "Document search is unavailable right now."


class LLMUnavailableError(AppError):
    status_code = 503
    code = "llm_unavailable"
    default_message = "The language model service is unavailable right now."
