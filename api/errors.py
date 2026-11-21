"""Turn exceptions into safe JSON error responses.

Every error response has the same shape:

    {"error": {"code": "not_found", "message": "...", "request_id": "..."}}

Clients can rely on `code`. `message` is safe to show to the user.
Stack traces, exception text and submitted values are never returned; they
are written to the log with the same request ID instead.
"""

from collections.abc import Mapping
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.middleware import REQUEST_ID_HEADER, resolve_request_id
from core.errors import AppError, InvalidRequestError
from core.logging import get_logger

logger = get_logger(__name__)

_HTTP_ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
}


def _request_id(request: Request) -> str:
    # Set by RequestContextMiddleware. The fallback only matters if the
    # middleware is missing, for example in a misconfigured test app.
    request_id = getattr(request.state, "request_id", None)
    return request_id if isinstance(request_id, str) else resolve_request_id(None)


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    body: dict[str, Any] = {"code": code, "message": message, "request_id": request_id}
    if details:
        body["details"] = details
    return JSONResponse(
        status_code=status_code,
        content={"error": body},
        headers={**(headers or {}), REQUEST_ID_HEADER: request_id},
    )


async def handle_app_error(request: Request, exc: Exception) -> JSONResponse:
    error = cast(AppError, exc)
    log = logger.error if error.status_code >= 500 else logger.info
    log("app_error", code=error.code, status_code=error.status_code, error=str(error))
    # A 401 tells the client which kind of login is expected (RFC 6750).
    headers = dict(error.headers)
    if error.status_code == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return error_response(
        request,
        status_code=error.status_code,
        code=error.code,
        message=error.message,
        details=error.details,
        headers=headers,
    )


async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    # Only the field location and the reason are returned. The submitted value
    # is left out on purpose: it may contain a password, token or personal data.
    fields = [
        {"field": ".".join(str(part) for part in error["loc"]), "reason": error["msg"]}
        for error in cast(RequestValidationError, exc).errors()
    ]
    return error_response(
        request,
        status_code=InvalidRequestError.status_code,
        code=InvalidRequestError.code,
        message=InvalidRequestError.default_message,
        details={"fields": fields},
    )


async def handle_http_error(request: Request, exc: Exception) -> JSONResponse:
    error = cast(StarletteHTTPException, exc)
    code = _HTTP_ERROR_CODES.get(error.status_code, "http_error")
    message = (
        error.detail if isinstance(error.detail, str) else "The request could not be completed."
    )
    return error_response(
        request, status_code=error.status_code, code=code, message=message, headers=error.headers
    )


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # Already logged with its stack trace by RequestContextMiddleware.
    return error_response(
        request,
        status_code=AppError.status_code,
        code=AppError.code,
        message=AppError.default_message,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
