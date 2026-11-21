"""Settings for the chat UI, read from environment variables.

The UI never imports backend code, so it cannot use `core.config`. It
needs only a few values, read here:

    API_URL             Where the API is. Default http://localhost:8000.
                        Docker Compose sets http://api:8000.
    UI_CONNECT_TIMEOUT  Seconds to wait for the API to accept a question. Default 5.
    UI_READ_TIMEOUT     Seconds of silence before the UI gives up on an answer.
                        Default 60. The API sends a keep-alive every 15 seconds
                        while it works, so a long silence means it is gone.
    KEYCLOAK_URL        Where the UI signs users in. Default http://localhost:8080.
                        Docker Compose sets http://keycloak:8080.
    KEYCLOAK_REALM      Default helix.
    KEYCLOAK_CLIENT_ID  The UI's client in the realm. Default enterprise-knowledge-ui.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass

# The API refuses longer questions (MAX_QUESTION_CHARS in the input guard).
MAX_QUESTION_CHARS = 2000


@dataclass(frozen=True)
class UiSettings:
    api_url: str = "http://localhost:8000"
    connect_timeout: float = 5.0
    read_timeout: float = 60.0
    keycloak_url: str = "http://localhost:8080"
    keycloak_realm: str = "helix"
    keycloak_client_id: str = "enterprise-knowledge-ui"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "UiSettings":
        env = os.environ if env is None else env
        return cls(
            api_url=_url(env, "API_URL", cls.api_url),
            connect_timeout=_seconds(env, "UI_CONNECT_TIMEOUT", cls.connect_timeout),
            read_timeout=_seconds(env, "UI_READ_TIMEOUT", cls.read_timeout),
            keycloak_url=_url(env, "KEYCLOAK_URL", cls.keycloak_url),
            keycloak_realm=env.get("KEYCLOAK_REALM", "").strip() or cls.keycloak_realm,
            keycloak_client_id=env.get("KEYCLOAK_CLIENT_ID", "").strip() or cls.keycloak_client_id,
        )


def _url(env: Mapping[str, str], name: str, default: str) -> str:
    url = env.get(name, "").strip() or default
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"{name} must start with http:// or https://, got {url!r}.")
    return url.rstrip("/")


def _seconds(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number of seconds, got {raw!r}.") from None
    if value <= 0:
        raise ValueError(f"{name} must be more than 0, got {raw!r}.")
    return value
