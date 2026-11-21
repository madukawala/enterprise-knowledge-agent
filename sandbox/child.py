"""The child process that runs one piece of code. Started fresh for every run.

    python -I child.py   < {"code": ..., "mode": ..., "data": ..., "limits": {...}}
                         > {"ok": true, "result": ..., "stdout": "...", "error": null}

Before the code runs, the child:

1. Lowers its own operating-system limits: CPU seconds, memory (Linux),
   open files, child processes (none) and file size (nothing may be
   written to disk). These cannot be raised again from inside.
2. Builds a namespace with only safe built-ins. In analysis mode `import`
   works for the allow-listed standard modules only; in plan mode there
   is no import at all, and the plan functions are provided instead.

The code gets the request's `data` as the variable `data`, and its answer
is whatever it assigns to `result`. `print()` output is captured and
returned (cut to a size limit). The result must be plain JSON data.

This file is started with `python -I`, so it ignores environment variables
and the user's site packages, and it imports nothing from the project.
"""

import builtins
import io
import json
import sys
import types
from contextlib import redirect_stdout, suppress
from typing import Any

try:  # not available on every platform; the container is Linux
    import resource
except ImportError:  # pragma: no cover
    resource = None  # type: ignore[assignment]

ALLOWED_MODULES = frozenset(
    {
        "math", "statistics", "collections", "datetime", "json", "re", "itertools",
        "functools", "operator", "string", "decimal", "fractions",
    }
)  # fmt: skip
SAFE_BUILTINS = (
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float",
    "frozenset", "int", "isinstance", "len", "list", "map", "max", "min", "pow", "range",
    "repr", "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple", "zip",
    "True", "False", "None", "ArithmeticError", "Exception", "IndexError", "KeyError",
    "TypeError", "ValueError", "ZeroDivisionError", "StopIteration",
)  # fmt: skip
MAX_BATCH_ITEMS = 50


def _limit(name: str, value: int) -> None:
    if resource is None:
        return
    # Some limits cannot be set on some systems (macOS ignores memory
    # limits). The container's own limits still apply.
    with suppress(ValueError, OSError, AttributeError):
        resource.setrlimit(getattr(resource, name), (value, value))


def apply_limits(cpu_seconds: int, memory_mb: int) -> None:
    _limit("RLIMIT_CPU", cpu_seconds)
    _limit("RLIMIT_AS", memory_mb * 1024 * 1024)
    _limit("RLIMIT_FSIZE", 0)
    _limit("RLIMIT_NOFILE", 16)
    _limit("RLIMIT_NPROC", 0)
    _limit("RLIMIT_CORE", 0)


_safe_modules: dict[str, types.SimpleNamespace] = {}


def _safe_module(module: types.ModuleType) -> types.SimpleNamespace:
    """A stand-in for `module` with only its public functions, classes and values.

    Real modules point at other modules: `statistics.sys`, `fractions.sys`,
    `json.codecs.builtins`. From `sys.modules` any module, `os` included,
    is one step away. The stand-in leaves out every module and every name
    starting with `_`, so those paths do not exist.
    """
    if module.__name__ not in _safe_modules:
        _safe_modules[module.__name__] = types.SimpleNamespace(
            **{
                name: value
                for name, value in vars(module).items()
                if not name.startswith("_") and not isinstance(value, types.ModuleType)
            }
        )
    return _safe_modules[module.__name__]


def _limited_import(
    name: str,
    globals: Any = None,
    locals: Any = None,
    fromlist: Any = (),
    level: int = 0,
) -> Any:
    top = name.split(".")[0]
    if level or "." in name or top not in ALLOWED_MODULES:
        raise ImportError(f"Importing {name!r} is not allowed in the sandbox.")
    return _safe_module(builtins.__import__(top, None, None, (), 0))


# ---------------------------------------------------------------
# Plan functions (ADR-0011). They only describe work; the API does it.
# ---------------------------------------------------------------


def filter_by(
    department: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Filters for a search: a department and a date range (YYYY-MM-DD)."""
    return {
        key: value
        for key, value in {
            "department": department,
            "date_from": date_from,
            "date_to": date_to,
        }.items()
        if value is not None
    }


def search(
    query: str,
    collections: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """One search the API will run, with the user's role, after the plan finishes."""
    return {
        "op": "search",
        "query": str(query),
        "collections": list(collections or []),
        "filters": dict(filters or {}),
        "label": str(label) if label is not None else str(query),
    }


def fetch_section(doc_id: str, section: str | None = None) -> dict[str, Any]:
    """The chunks of one document (and section), fetched by the API with the user's role."""
    return {"op": "fetch_section", "doc_id": str(doc_id), "section": section}


def batch(items: list[Any], size: int) -> list[list[Any]]:
    """Split `items` into groups of `size`, for sub-agents to work on in parallel."""
    size = max(1, int(size))
    items = list(items)[: MAX_BATCH_ITEMS * size]
    return [items[i : i + size] for i in range(0, len(items), size)]


PLAN_FUNCTIONS = {
    "search": search,
    "filter_by": filter_by,
    "fetch_section": fetch_section,
    "batch": batch,
}


def namespace(mode: str, data: Any, printed: io.StringIO) -> dict[str, Any]:
    safe = {name: getattr(builtins, name) for name in SAFE_BUILTINS}

    def captured_print(*args: Any, **kwargs: Any) -> None:
        kwargs["file"] = printed
        builtins.print(*args, **kwargs)

    safe["print"] = captured_print
    if mode == "analysis":
        safe["__import__"] = _limited_import
    names: dict[str, Any] = {"__builtins__": safe, "data": data, "result": None}
    if mode == "plan":
        names.update(PLAN_FUNCTIONS)
    return names


def run(request: dict[str, Any]) -> dict[str, Any]:
    limits = request.get("limits", {})
    max_output = int(limits.get("max_output_chars", 20_000))
    printed = io.StringIO()
    names = namespace(str(request["mode"]), request.get("data"), printed)
    code = compile(str(request["code"]), "<sandbox>", "exec")
    apply_limits(int(limits.get("cpu_seconds", 5)), int(limits.get("memory_mb", 256)))
    try:
        with redirect_stdout(printed):
            # Running code is the sandbox's job; the walls are in the module docstring.
            exec(code, names)  # noqa: S102  # nosec B102
        result = names.get("result")
        encoded = json.dumps(result, default=str)
        if len(encoded) > max_output:
            return _failure("The result is too large.", printed, max_output)
        return {
            "ok": True,
            "result": json.loads(encoded),
            "stdout": printed.getvalue()[:max_output],
            "error": None,
        }
    except MemoryError:
        return _failure("The code used too much memory.", printed, max_output)
    except RecursionError:
        return _failure("The code recursed too deeply.", printed, max_output)
    except Exception as error:  # the user's code failed: report it, do not crash
        return _failure(f"{type(error).__name__}: {error}"[:500], printed, max_output)


def _failure(message: str, printed: io.StringIO, max_output: int) -> dict[str, Any]:
    return {
        "ok": False,
        "result": None,
        "stdout": printed.getvalue()[:max_output],
        "error": message,
    }


def main() -> None:
    request = json.loads(sys.stdin.read())
    sys.stdout.write(json.dumps(run(request)))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
