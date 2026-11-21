import json
import logging

import pytest
import structlog

from core.config import LogFormat
from core.logging import REDACTED, SECURITY_LOGGER, configure_logging, get_logger, redact_secrets


def _last_json_line(output: str) -> dict[str, object]:
    line = output.strip().splitlines()[-1]
    parsed: dict[str, object] = json.loads(line)
    return parsed


def test_redacts_secret_fields_at_any_depth() -> None:
    event = {
        "event": "calling_provider",
        "api_key": "sk-123",
        "token": "abc",
        "headers": {"Authorization": "Bearer abc", "accept": "json"},
        "attempts": [{"password": "hunter2"}],
        "tokens_used": 42,
    }

    result = redact_secrets(None, "info", event)

    assert result["api_key"] == REDACTED
    assert result["token"] == REDACTED
    assert result["headers"] == {"Authorization": REDACTED, "accept": "json"}
    assert result["attempts"] == [{"password": REDACTED}]
    # Counters that merely mention tokens are kept.
    assert result["tokens_used"] == 42


def test_json_lines_include_context_and_hide_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", LogFormat.JSON)
    structlog.contextvars.bind_contextvars(request_id="req-1")
    try:
        get_logger("tests").info("user_signed_in", role="viewer", access_token="eyJ...")
    finally:
        structlog.contextvars.clear_contextvars()

    line = _last_json_line(capsys.readouterr().out)

    assert line["event"] == "user_signed_in"
    assert line["request_id"] == "req-1"
    assert line["level"] == "info"
    assert line["logger"] == "tests"
    assert line["access_token"] == REDACTED
    assert "timestamp" in line


def test_standard_library_logs_use_the_same_format(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", LogFormat.JSON)

    logging.getLogger("uvicorn.error").info("server started")

    line = _last_json_line(capsys.readouterr().out)
    assert line["event"] == "server started"
    assert line["logger"] == "uvicorn.error"


def test_security_events_have_their_own_logger(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", LogFormat.JSON)

    get_logger(SECURITY_LOGGER).warning("access_denied", tool="admin_reindex")

    line = _last_json_line(capsys.readouterr().out)
    assert line["logger"] == "security"
    assert line["level"] == "warning"


def test_level_filters_out_quieter_lines(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING", LogFormat.JSON)

    get_logger("tests").info("not_shown")

    assert "not_shown" not in capsys.readouterr().out


def test_console_format_is_readable_text(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", LogFormat.CONSOLE)

    get_logger("tests").info("hello_console")

    output = capsys.readouterr().out
    assert "hello_console" in output
    assert not output.lstrip().startswith("{")
