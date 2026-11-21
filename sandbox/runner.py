"""Runs one piece of code: check it, then run it in a fresh child process.

Every run gets a new process (sandbox/child.py), started with:

- `python -I`: no environment variables are read and no user site packages;
- an **empty environment**, so even a way out of the restrictions finds no
  keys or passwords to read;
- a new empty working folder, removed afterwards;
- a **wall-clock time limit**: the process is killed when it runs out,
  whatever it is doing (sleeping, waiting, looping);
- CPU, memory, file-size and process limits that the child sets on itself
  before the code runs.

Only `RUNS_AT_ONCE` runs happen at the same time; further requests wait.
"""

import json
import signal

# Starts only our own child script, never a shell.
import subprocess  # nosec B404
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sandbox.validator import Mode, Rejected, validate

CHILD = Path(__file__).with_name("child.py")
RUNS_AT_ONCE = 2
_slots = threading.BoundedSemaphore(RUNS_AT_ONCE)

# What the operating system's signal means, when the child was killed.
_STOPPED_BY: dict[int, str] = {
    int(signal.SIGXCPU): "The code used more CPU time than the sandbox allows and was stopped.",
    int(signal.SIGKILL): "The code used more memory than the sandbox allows and was stopped.",
    int(signal.SIGSEGV): "The code crashed the sandbox process and was stopped.",
}


@dataclass(frozen=True)
class Limits:
    wall_seconds: float = 5.0
    cpu_seconds: int = 5
    memory_mb: int = 256
    max_output_chars: int = 20_000


@dataclass(frozen=True)
class Outcome:
    ok: bool
    result: Any = None
    stdout: str = ""
    # Safe to show: the validator's reason, the error type and message, or
    # which limit was hit. Never a traceback from the sandbox itself.
    error: str | None = None
    # rejected (validator), error (the code failed), timeout, crashed
    status: str = "ok"
    duration_ms: int = 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def run(code: str, mode: Mode, data: Any = None, limits: Limits | None = None) -> Outcome:
    limits = limits or Limits()
    started = time.perf_counter()

    def elapsed() -> int:
        return round((time.perf_counter() - started) * 1000)

    try:
        validate(code, mode)
    except Rejected as rejected:
        return Outcome(ok=False, error=rejected.reason, status="rejected", duration_ms=elapsed())

    request = json.dumps(
        {
            "code": code,
            "mode": mode.value,
            "data": data,
            "limits": {
                "cpu_seconds": limits.cpu_seconds,
                "memory_mb": limits.memory_mb,
                "max_output_chars": limits.max_output_chars,
            },
        }
    )
    with _slots, tempfile.TemporaryDirectory(prefix="run-") as workdir:
        try:
            # A fixed command: this Python, isolated mode, our own child script.
            completed = subprocess.run(  # noqa: S603  # nosec B603
                [sys.executable, "-I", str(CHILD)],
                input=request,
                capture_output=True,
                text=True,
                timeout=limits.wall_seconds,
                env={},
                cwd=workdir,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Outcome(
                ok=False,
                error=(
                    f"The code ran for more than {limits.wall_seconds:g} seconds and was stopped."
                ),
                status="timeout",
                duration_ms=elapsed(),
            )

    try:
        reply = json.loads(completed.stdout)
    except json.JSONDecodeError:
        # Killed by a limit (CPU time, memory) or crashed before replying.
        reason = _STOPPED_BY.get(
            -completed.returncode, "The code was stopped because it went over a sandbox limit."
        )
        return Outcome(ok=False, error=reason, status="crashed", duration_ms=elapsed())

    return Outcome(
        ok=bool(reply.get("ok")),
        result=reply.get("result"),
        stdout=str(reply.get("stdout") or ""),
        error=reply.get("error"),
        status="ok" if reply.get("ok") else "error",
        duration_ms=elapsed(),
    )
