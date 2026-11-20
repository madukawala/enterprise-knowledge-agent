"""Settings for the chat UI, read from environment variables.

The UI never imports backend code, so it cannot use `core.config`. It
needs only a few values, read here:

    API_URL             Where the API is. Default http://localhost:8000.
                        Docker Compose sets http://api:8000.
    UI_CONNECT_TIMEOUT  Seconds to wait for the API to accept a question. Default 5.
    UI_READ_TIMEOUT     Seconds of silence before the UI gives up on an answer.
                        Default 60. The API sends a keep-alive every 15 seconds
                        while it works, so a long silence means it is gone.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass

# The roles the API knows (security/roles.py). Used by the role picker until
# login is added with `feature/auth-rbac`; the API checks the value itself.
ROLES = ("viewer", "analyst", "admin")

# The API refuses longer questions (MAX_QUESTION_CHARS in the input guard).
MAX_QUESTION_CHARS = 2000


@dataclass(frozen=True)
class UiSettings:
    api_url: str = "http://localhost:8000"
    connect_timeout: float = 5.0
    read_timeout: float = 60.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "UiSettings":
        env = os.environ if env is None else env
        api_url = env.get("API_URL", "").strip() or cls.api_url
        if not api_url.startswith(("http://", "https://")):
            raise ValueError(f"API_URL must start with http:// or https://, got {api_url!r}.")
        return cls(
            api_url=api_url.rstrip("/"),
            connect_timeout=_seconds(env, "UI_CONNECT_TIMEOUT", cls.connect_timeout),
            read_timeout=_seconds(env, "UI_READ_TIMEOUT", cls.read_timeout),
        )


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
