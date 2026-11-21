import json
from pathlib import Path

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


def test_empty_keys_count_as_not_set() -> None:
    """`.env.example` lists keys with empty values; those must mean "not set"."""
    settings = Settings(
        _env_file=None,
        gemini_api_key="",  # type: ignore[arg-type]
        openrouter_api_key="  ",  # type: ignore[arg-type]
        pinecone_api_key="",  # type: ignore[arg-type]
        langsmith_api_key="",  # type: ignore[arg-type]
        postgres_url="",  # type: ignore[arg-type]
    )
    assert settings.gemini_api_key is None
    assert settings.openrouter_api_key is None
    assert settings.pinecone_api_key is None
    assert settings.langsmith_api_key is None
    assert settings.postgres_url is None


# ---------------------------------------------------------------
# Language model names are chosen in .env, not in code
# ---------------------------------------------------------------


def test_model_names_have_no_default() -> None:
    settings = Settings(_env_file=None)
    assert (settings.gemini_model, settings.openrouter_model) == (None, None)
    assert settings.llm_providers() == []


@pytest.mark.parametrize("provider", ["gemini", "openrouter"])
def test_a_key_without_its_model_stops_startup_with_a_clear_message(provider: str) -> None:
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, **{f"{provider}_api_key": "a-key"})  # type: ignore[arg-type]
    message = str(error.value)
    upper = provider.upper()
    assert f"{upper}_API_KEY is set, so {upper}_MODEL must be set too" in message
    # The key itself is never repeated in the error.
    assert "a-key" not in message


def test_an_empty_model_counts_as_missing() -> None:
    with pytest.raises(ValidationError, match="GEMINI_MODEL must be set"):
        Settings(_env_file=None, gemini_api_key="a-key", gemini_model=" ")  # type: ignore[arg-type]


def test_every_missing_model_is_reported_at_once() -> None:
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, gemini_api_key="g", openrouter_api_key="o")  # type: ignore[arg-type]
    assert "GEMINI_MODEL" in str(error.value)
    assert "OPENROUTER_MODEL" in str(error.value)


def test_a_model_without_its_key_is_allowed_and_unused() -> None:
    settings = Settings(_env_file=None, openrouter_model="or-model")  # type: ignore[arg-type]
    assert settings.llm_providers() == []


def test_providers_with_key_and_model_are_listed_in_failover_order() -> None:
    settings = Settings(
        _env_file=None,
        openrouter_api_key="o",  # type: ignore[arg-type]
        openrouter_model="or-model",
        gemini_api_key="g",  # type: ignore[arg-type]
        gemini_model="gemini-model",
    )
    assert [(name, model) for name, _key, model in settings.llm_providers()] == [
        ("gemini", "gemini-model"),
        ("openrouter", "or-model"),
    ]


def test_the_signing_keys_url_is_built_from_keycloak_and_the_realm() -> None:
    settings = Settings(
        _env_file=None, keycloak_url="http://keycloak:8080/", keycloak_realm="helix"
    )
    assert settings.jwks_url == "http://keycloak:8080/realms/helix/protocol/openid-connect/certs"


def test_login_defaults_match_the_keycloak_realm_file() -> None:
    realm = json.loads((Path(__file__).parents[2] / "infra/keycloak/realm-helix.json").read_text())
    settings = Settings(_env_file=None)
    assert settings.auth_issuer.endswith("/realms/" + realm["realm"])
    mappers = realm["clients"][0]["protocolMappers"]
    audiences = {m["config"].get("included.custom.audience") for m in mappers}
    assert settings.auth_audience in audiences
    # Short-lived tokens: five minutes, refreshed by the UI.
    assert realm["accessTokenLifespan"] <= 300
