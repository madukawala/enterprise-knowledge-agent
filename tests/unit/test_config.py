import pytest
from pydantic import ValidationError

from core.config import Environment, LogFormat, Settings, get_settings


def test_defaults_are_safe_for_local_use() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_env is Environment.LOCAL
    assert settings.log_format is LogFormat.JSON
    # Outside Docker the API should only listen on the local machine.
    assert settings.api_host == "127.0.0.1"
    assert not settings.is_production


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("API_PORT", "9000")

    settings = Settings(_env_file=None)

    assert settings.is_production
    assert settings.log_level == "WARNING"
    assert settings.api_port == 9000


@pytest.mark.parametrize(
    ("name", "value"),
    [("API_PORT", "0"), ("API_PORT", "70000"), ("LOG_LEVEL", "LOUD"), ("APP_ENV", "staging")],
)
def test_invalid_values_are_rejected_at_startup(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_get_settings_reads_the_environment_once() -> None:
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()


def test_connection_strings_are_hidden_when_settings_are_printed() -> None:
    settings = Settings(_env_file=None, postgres_url="postgresql://app:s3cret@postgres:5432/app")

    assert "s3cret" not in repr(settings)
    assert "s3cret" not in str(settings.model_dump())
