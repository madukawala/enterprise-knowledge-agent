"""Trying to get out of the Python sandbox, and being stopped.

Each attack runs through `sandbox.runner.run`: the validator, then a real
child process with its limits. The container adds more walls (no network,
read-only files, memory and process limits); `make test-sandbox` runs the
container checks in tests/e2e/test_sandbox_container.py.
"""

import subprocess
import sys
from typing import Any

import pytest

from sandbox import child, runner
from sandbox.runner import Limits, run
from sandbox.validator import Mode

FAST = Limits(wall_seconds=3, cpu_seconds=2)


def _run(code: str, mode: Mode = Mode.ANALYSIS, data: Any = None) -> runner.Outcome:
    return run(code, mode, data, FAST)


# Code that must be refused before it runs: (name, code).
REFUSED_BEFORE_RUNNING = [
    ("read a file", "result = open('/etc/passwd').read()"),
    ("write a file", "open('/tmp/x', 'w').write('x')"),
    ("import os", "import os\nresult = os.listdir('/')"),
    ("import subprocess", "import subprocess\nsubprocess.run(['id'])"),
    ("import socket", "import socket\nsocket.create_connection(('example.com', 80))"),
    ("import urllib", "import urllib.request\nurllib.request.urlopen('http://example.com')"),
    ("import sys", "import sys\nresult = sys.modules"),
    ("from import os", "from os import system"),
    ("importlib", "import importlib\nimportlib.import_module('os')"),
    ("__import__", "result = __import__('os')"),
    ("class hierarchy", "result = ().__class__.__bases__[0].__subclasses__()"),
    ("function globals", "def f(): pass\nresult = f.__globals__"),
    ("builtins dict", "result = __builtins__"),
    ("eval", "result = eval('1+1')"),
    ("exec", "exec('import os')"),
    ("compile", "result = compile('1', 'x', 'eval')"),
    ("getattr", "result = getattr((), '__class__')"),
    ("format reads attributes", "result = '{0.__class__}'.format(1)"),
    ("format_map", "result = '{x}'.format_map({'x': 1})"),
    ("global", "global data"),
    ("class", "class A: pass"),
    ("with", "with x: pass"),
    ("type()", "result = type(1)"),
    ("relative import", "from . import runner"),
    ("star import", "from math import *"),
    ("private name", "_x = 1"),
]


@pytest.mark.parametrize(
    ("name", "code"), REFUSED_BEFORE_RUNNING, ids=[n for n, _ in REFUSED_BEFORE_RUNNING]
)
def test_escapes_are_refused_before_anything_runs(name: str, code: str) -> None:
    outcome = _run(code)
    assert outcome.status == "rejected", outcome
    assert not outcome.ok
    assert outcome.duration_ms < 1_000


@pytest.mark.parametrize(
    "code",
    [
        "import statistics\nresult = statistics.sys.modules['os']",
        "import fractions\nresult = fractions.sys",
        "import json\nresult = json.codecs.builtins",
        "import re\nresult = re.enum.sys",
        "from statistics import sys",
    ],
)
def test_allowed_modules_do_not_lead_to_other_modules(code: str) -> None:
    outcome = _run(code)
    assert outcome.status == "error"
    assert outcome.result is None


def test_an_endless_loop_is_stopped() -> None:
    outcome = _run("while True:\n    pass")
    assert outcome.status in {"crashed", "timeout"}
    assert outcome.duration_ms < 5_000


def test_a_long_computation_is_stopped() -> None:
    outcome = _run("x = 0\nfor i in range(10**12):\n    x += i")
    assert outcome.status in {"crashed", "timeout"}


def test_a_huge_number_is_stopped() -> None:
    outcome = _run("result = 9 ** 9 ** 9")
    assert outcome.status in {"crashed", "timeout"}


@pytest.mark.skipif(sys.platform != "linux", reason="memory limits are enforced on Linux only")
def test_a_memory_bomb_is_stopped() -> None:
    outcome = run("x = 'a' * (10 ** 10)", Mode.ANALYSIS, None, Limits(memory_mb=128))
    assert not outcome.ok


def test_deep_recursion_is_reported_not_fatal() -> None:
    outcome = _run("def f(n): return f(n + 1)\nresult = f(0)")
    assert outcome.status == "error"
    assert "recursed too deeply" in (outcome.error or "")


def test_a_result_that_is_too_large_is_refused() -> None:
    outcome = run("result = 'x' * 100000", Mode.ANALYSIS, None, Limits(max_output_chars=1_000))
    assert outcome.error == "The result is too large."


def test_printed_output_is_cut() -> None:
    outcome = run(
        "for i in range(5000):\n    print('line', i)",
        Mode.ANALYSIS,
        None,
        Limits(max_output_chars=500),
    )
    assert len(outcome.stdout) <= 500


def test_the_child_runs_with_an_empty_environment_and_its_own_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a way out of the restrictions finds no keys to read."""
    seen: dict[str, Any] = {}
    real_run = subprocess.run

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs, command=args[0])
        return real_run(*args, **kwargs)

    monkeypatch.setenv("GEMINI_API_KEY", "must-not-leak")
    monkeypatch.setattr(runner.subprocess, "run", spy)

    _run("result = 1")

    assert seen["env"] == {}
    assert seen["command"][1] == "-I"  # ignores PYTHON* variables and user site packages
    assert "run-" in seen["cwd"]


def test_the_child_namespace_has_only_safe_builtins() -> None:
    import io

    names = child.namespace("analysis", None, io.StringIO())
    builtins = names["__builtins__"]
    for dangerous in ("open", "eval", "exec", "compile", "getattr", "globals", "input", "type"):
        assert dangerous not in builtins
    with pytest.raises(ImportError):
        builtins["__import__"]("os")
    with pytest.raises(ImportError):
        builtins["__import__"]("collections.abc")
    assert "__import__" not in child.namespace("plan", None, io.StringIO())["__builtins__"]


def test_a_safe_module_stand_in_has_functions_but_no_modules() -> None:
    import statistics
    import types

    safe = child._safe_module(statistics)
    assert callable(safe.mean)
    assert not any(isinstance(v, types.ModuleType) for v in vars(safe).values())


# ---------------------------------------------------------------
# What does work
# ---------------------------------------------------------------


def test_ordinary_analysis_works() -> None:
    code = (
        "from collections import Counter\n"
        "import statistics\n"
        "counts = Counter(row['service'] for row in data)\n"
        "print('rows:', len(data))\n"
        "result = {'top': counts.most_common(1), 'mean_minutes': statistics.mean("
        "row['minutes'] for row in data)}"
    )
    data = [
        {"service": "PayHub", "minutes": 40},
        {"service": "PayHub", "minutes": 20},
        {"service": "Cards", "minutes": 30},
    ]
    outcome = _run(code, data=data)
    assert outcome.ok, outcome
    assert outcome.result == {"top": [["PayHub", 2]], "mean_minutes": 30}
    assert outcome.stdout == "rows: 3\n"


def test_a_failing_program_says_why() -> None:
    outcome = _run("result = data['missing']", data={})
    assert outcome.status == "error"
    assert outcome.error == "KeyError: 'missing'"
