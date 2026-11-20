"""Structured logging with structlog.

Every log line is a JSON object (or a readable console line in local
development). Values bound with `bind_contextvars`, such as the request ID,
are added to every line logged while handling that request.

Logs from other libraries that use the standard `logging` module (uvicorn,
httpx and so on) go through the same pipeline, so the whole service writes
one consistent format.

Secrets are removed before anything is written. Masking of personal data
(Presidio) is added in the guardrails branch.
"""

import logging
import sys
from collections.abc import Mapping
from typing import Any

import structlog
from structlog.types import EventDict, Processor, WrappedLogger

from core.config import LogFormat, LogLevel

REDACTED = "[REDACTED]"

# A log field is redacted when its name contains one of these words.
_SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "access_token",
    "refresh_token",
    "id_token",
    "jwt",
)

SECURITY_LOGGER = "security"
"""Logger name for security events: blocked prompts, denied access, rate limits."""


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return lowered == "token" or any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: REDACTED if _is_sensitive(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact(item) for item in value]
    return value


def redact_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Replace the value of any field that looks like a secret, including nested fields."""
    for key in list(event_dict):
        if _is_sensitive(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact(event_dict[key])
    return event_dict


class _StdoutHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Writes to whatever `sys.stdout` is at the moment of logging.

    A plain StreamHandler keeps the stream it was created with, which breaks
    output capture in tests and tools that swap `sys.stdout` later.
    """

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stdout
        super().emit(record)


def configure_logging(level: LogLevel = "INFO", log_format: LogFormat = LogFormat.JSON) -> None:
    """Set up structlog and route standard library logging through it.

    Safe to call more than once. The latest call wins, which tests rely on.
    """
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_secrets,
    ]

    renderer: Processor
    if log_format is LogFormat.JSON:
        renderer = structlog.processors.JSONRenderer()
        shared_processors.append(structlog.processors.format_exc_info)
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # Not cached, so reconfiguring (for example in tests) takes effect at once.
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = _StdoutHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # uvicorn adds its own handlers; send its logs through ours instead.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # The API logs one line per request itself (see api/middleware.py),
    # so uvicorn's access log would only repeat it.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a logger. Pass `__name__`, or `SECURITY_LOGGER` for security events."""
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger
