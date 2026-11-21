"""The enterprise data tools: calls to the MCP server (mcp_server/README.md).

The agent side keeps its own copy of each tool's arguments as a Pydantic
model with the same limits as the server. Arguments written by the model
are checked here, before anything is sent: a wrong type, an unknown status
or an unknown tool is refused without calling the server. The server then
checks them again.

    tools = EnterpriseTools("http://mcp:8200/mcp", timeout_seconds=10)
    result = await tools.call("service_owner", {"service": "PayHub"})
    result.ok, result.data, result.error

Tests pass the server object itself (`EnterpriseTools(build_server())`), and
the MCP client talks to it in memory.
"""

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Literal

from mcp.client.client import Client
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.logging import get_logger

log = get_logger(__name__)

ToolName = Literal["find_person", "service_owner", "list_incidents"]
MAX_RESULT_CHARS = 4_000


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FindPersonArgs(_Args):
    name: str = Field(min_length=2, max_length=60)


class ServiceOwnerArgs(_Args):
    service: str = Field(min_length=2, max_length=60)


class ListIncidentsArgs(_Args):
    service: str | None = Field(default=None, max_length=60)
    status: Literal["open", "monitoring", "resolved"] | None = None
    limit: int = Field(default=10, ge=1, le=20)


ARGUMENTS: dict[str, type[_Args]] = {
    "find_person": FindPersonArgs,
    "service_owner": ServiceOwnerArgs,
    "list_incidents": ListIncidentsArgs,
}

# What each tool is for, for the model choosing one.
DESCRIPTIONS: dict[str, str] = {
    "find_person": "find_person(name): look up staff by name: title, team, location, e-mail.",
    "service_owner": "service_owner(service): the team, service owner and on-call for a service.",
    "list_incidents": (
        "list_incidents(service=None, status=None, limit=10): current incident records, newest "
        "first. status is one of open, monitoring, resolved."
    ),
}


@dataclass(frozen=True)
class ToolResult:
    tool: str
    arguments: dict[str, Any]
    ok: bool
    data: Any = None
    # Safe to show: why the call was refused or failed.
    error: str | None = None
    duration_ms: int = 0

    @property
    def items(self) -> int:
        """How many records came back (for the activity panel)."""
        if isinstance(self.data, dict) and isinstance(self.data.get("count"), int):
            return int(self.data["count"])
        return 0

    def as_text(self) -> str:
        """The result for the answer prompt: compact JSON, cut to a size limit."""
        return json.dumps(self.data, separators=(",", ":"), default=str)[:MAX_RESULT_CHARS]


def check_arguments(tool: str, arguments: Any) -> dict[str, Any]:
    """The arguments, checked against the tool's model. Raises ValueError if they are wrong."""
    model = ARGUMENTS.get(tool)
    if model is None:
        raise ValueError(f"unknown tool {tool!r}")
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    try:
        return model.model_validate(arguments).model_dump(exclude_none=True)
    except ValidationError as error:
        fields = ", ".join(
            ".".join(str(p) for p in e["loc"]) or "arguments" for e in error.errors()
        )
        raise ValueError(f"bad arguments ({fields})") from None


class EnterpriseTools:
    def __init__(self, server: Any, *, timeout_seconds: float = 10.0) -> None:
        # A URL (the MCP server in Docker) or an MCPServer object (tests).
        self.server = server
        self.timeout_seconds = timeout_seconds

    async def call(self, tool: str, arguments: Any) -> ToolResult:
        """Check the arguments, then call the tool. Never raises; failures are in the result."""
        started = time.perf_counter()

        def elapsed() -> int:
            return round((time.perf_counter() - started) * 1000)

        try:
            checked = check_arguments(tool, arguments)
        except ValueError as error:
            raw = arguments if isinstance(arguments, dict) else {}
            return ToolResult(tool, raw, ok=False, error=str(error), duration_ms=elapsed())
        try:
            async with asyncio.timeout(self.timeout_seconds), Client(self.server) as client:
                reply = await client.call_tool(tool, checked)
        except Exception as error:  # the server is down or broke the protocol
            log.warning("mcp_call_failed", tool=tool, error=type(error).__name__)
            return ToolResult(
                tool,
                checked,
                ok=False,
                error="the enterprise data service did not respond",
                duration_ms=elapsed(),
            )
        text = "".join(getattr(part, "text", "") for part in reply.content)
        if reply.is_error:
            return ToolResult(tool, checked, ok=False, error=text[:200], duration_ms=elapsed())
        try:
            data = json.loads(text)
        except ValueError:
            data = {"text": text[:MAX_RESULT_CHARS]}
        return ToolResult(tool, checked, ok=True, data=data, duration_ms=elapsed())
