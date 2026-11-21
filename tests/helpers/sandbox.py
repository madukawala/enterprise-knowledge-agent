"""A code runner for tests that uses the real sandbox code, without the container.

The app talks to the sandbox over HTTP (agents/tools/sandbox.py). Tests
plug this in instead: it calls `sandbox.runner.run` directly, which still
validates the code and starts a real child process with the same limits.
Only the HTTP hop and the container walls are missing; those are checked by
`make test-sandbox` against the running container.
"""

import asyncio
from typing import Any

from agents.tools.sandbox import SandboxMode, SandboxResult
from sandbox.runner import Limits, run
from sandbox.validator import Mode


class LocalSandbox:
    def __init__(self, limits: Limits | None = None) -> None:
        self.limits = limits or Limits(wall_seconds=3, cpu_seconds=2)
        self.calls: list[tuple[str, str, Any]] = []

    async def run(self, code: str, *, mode: SandboxMode, data: Any = None) -> SandboxResult:
        self.calls.append((code, mode, data))
        outcome = await asyncio.to_thread(run, code, Mode(mode), data, self.limits)
        return SandboxResult.model_validate(outcome.to_json())

    async def aclose(self) -> None:
        return None
