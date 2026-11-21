"""Talks to the Python sandbox container (sandbox/server.py).

    sandbox = SandboxClient("http://sandbox:8100", timeout_seconds=10)
    result = await sandbox.run("import statistics\\nresult = statistics.mean(data)",
                               mode="analysis", data=[1, 2, 3])
    result.ok, result.result, result.stdout, result.error, result.status

The sandbox checks and runs the code; this client only carries it there and
back. It never runs code itself. If the sandbox cannot be reached or
answers with something unexpected, `ToolError` (502) is raised, with a
message that is safe to show.

`CodeRunner` is the interface the rest of the app depends on, so tests can
plug in a runner that calls the sandbox code directly.
"""

from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ValidationError

from core.errors import ToolError
from core.logging import get_logger

log = get_logger(__name__)

SandboxMode = Literal["analysis", "plan"]
SANDBOX_UNREACHABLE = "The Python sandbox did not respond. Please try again shortly."


class SandboxResult(BaseModel):
    ok: bool
    result: Any = None
    stdout: str = ""
    error: str | None = None
    # ok, rejected (the code is not allowed), error (it failed), timeout, crashed
    status: Literal["ok", "rejected", "error", "timeout", "crashed"]
    duration_ms: int = 0


class CodeRunner(Protocol):
    async def run(self, code: str, *, mode: SandboxMode, data: Any = None) -> SandboxResult: ...

    async def aclose(self) -> None: ...


class SandboxClient:
    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=url.rstrip("/"), timeout=timeout_seconds, transport=transport
        )

    async def run(self, code: str, *, mode: SandboxMode, data: Any = None) -> SandboxResult:
        try:
            response = await self._http.post(
                "/run", json={"code": code, "mode": mode, "data": data}
            )
            response.raise_for_status()
            return SandboxResult.model_validate(response.json())
        except (httpx.HTTPError, ValueError, ValidationError) as error:
            log.warning("sandbox_unreachable", error=type(error).__name__)
            raise ToolError(SANDBOX_UNREACHABLE) from error

    async def aclose(self) -> None:
        await self._http.aclose()
