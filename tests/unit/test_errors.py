import pytest

from core.errors import (
    AppError,
    AuthError,
    InvalidRequestError,
    LLMUnavailableError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    RetrievalError,
    ToolError,
)


@pytest.mark.parametrize(
    ("error_class", "status_code", "code"),
    [
        (AppError, 500, "internal_error"),
        (AuthError, 401, "unauthorized"),
        (PermissionDeniedError, 403, "forbidden"),
        (NotFoundError, 404, "not_found"),
        (InvalidRequestError, 422, "invalid_request"),
        (RateLimitError, 429, "rate_limited"),
        (ToolError, 502, "tool_error"),
        (RetrievalError, 503, "retrieval_unavailable"),
        (LLMUnavailableError, 503, "llm_unavailable"),
    ],
)
def test_each_error_has_a_status_and_stable_code(
    error_class: type[AppError], status_code: int, code: str
) -> None:
    error = error_class()

    assert isinstance(error, AppError)
    assert error.status_code == status_code
    assert error.code == code
    assert error.message == error_class.default_message


def test_custom_message_and_details() -> None:
    error = NotFoundError("Thread not found.", details={"thread_id": "t-1"})

    assert error.message == "Thread not found."
    assert str(error) == "Thread not found."
    assert error.details == {"thread_id": "t-1"}
