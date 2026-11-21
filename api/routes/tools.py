"""`POST /tools/python`: the Python analysis tool, for analysts and admins.

    curl -X POST http://localhost:8000/tools/python \\
      -H "Authorization: Bearer $(make -s token ROLE=analyst)" \\
      -H "Content-Type: application/json" \\
      -d '{"code": "import statistics\\nresult = statistics.mean(data)", "data": [3, 5, 10]}'

The code runs in the sandbox container (sandbox/README.md), never in the
API. It gets `data` as a variable and returns whatever it assigns to
`result`; `print()` output comes back as `stdout`. It may import only a
short list of standard modules, has no network and no files, and is
stopped after 5 seconds.

The answer is 200 whether the code worked or not; `status` says which:
`ok`, `rejected` (the code is not allowed, with the reason), `error` (it
raised an exception), `timeout` or `crashed` (it went over a limit).

- 401 / 403: not signed in, or the role may not use the tool (viewers).
- 429: the caller's rate limit (each run counts as one question; it does
  not touch the token budget, because no language model is called).
- 502: the sandbox did not answer. 503: the sandbox is not configured.
"""

import json
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agents.telemetry import AgentMetrics
from agents.tools.sandbox import CodeRunner, SandboxResult
from api.identity import Caller, requires
from core.errors import ServiceUnavailableError, ToolError
from core.logging import SECURITY_LOGGER, get_logger
from security.policies import Action
from security.rate_limit import RateLimiter

router = APIRouter(prefix="/tools", tags=["tools"])
security_log = get_logger(SECURITY_LOGGER)

MAX_CODE_CHARS = 8_000
MAX_DATA_CHARS = 100_000
NOT_CONFIGURED = "The Python sandbox is not configured on this server."


class PythonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=MAX_CODE_CHARS)
    # Any JSON value, available to the code as `data`.
    data: Any = None

    @field_validator("data")
    @classmethod
    def _not_too_big(cls, value: Any) -> Any:
        if len(json.dumps(value, default=str)) > MAX_DATA_CHARS:
            raise ValueError(f"data is larger than {MAX_DATA_CHARS} characters of JSON.")
        return value


_ERRORS: dict[int | str, dict[str, object]] = {
    status.HTTP_403_FORBIDDEN: {"description": "The role may not use the Python tool."},
    status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Rate limit. See Retry-After."},
    status.HTTP_502_BAD_GATEWAY: {"description": "The sandbox did not answer."},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "The sandbox is not configured."},
}


@router.post("/python", responses=_ERRORS)
async def run_python(
    body: PythonRequest,
    request: Request,
    caller: Annotated[Caller, Depends(requires(Action.PYTHON_ANALYSIS))],
) -> SandboxResult:
    sandbox = cast(CodeRunner | None, request.app.state.sandbox)
    metrics = cast(AgentMetrics, request.app.state.agent_metrics)
    if sandbox is None:
        metrics.tool_runs.labels(tool="python", status="unavailable").inc()
        raise ServiceUnavailableError(NOT_CONFIGURED)

    limiter = cast(RateLimiter, request.app.state.rate_limiter)
    admission = await limiter.admit(caller.pseudonym, caller.role)
    await limiter.refund(admission)  # no model call, so nothing to charge

    try:
        result = await sandbox.run(body.code, mode="analysis", data=body.data)
    except ToolError:
        metrics.tool_runs.labels(tool="python", status="unavailable").inc()
        raise
    metrics.tool_runs.labels(tool="python", status=result.status).inc()
    if result.status in {"rejected", "timeout", "crashed"}:
        # Not necessarily an attack, but worth seeing in one place.
        security_log.info(
            "sandbox_refused",
            status=result.status,
            reason=(result.error or "")[:200],
            user=caller.pseudonym,
            role=caller.role.value,
        )
    return result
