"""Checks Python code before the sandbox runs it.

Two modes:

- **analysis**: the Python analysis tool for analysts and admins. Ordinary
  Python for working on data: loops, functions, comprehensions, and
  imports from a short list of standard modules (math, statistics,
  collections, datetime, json, re, itertools, functools, operator).
- **plan**: research search plans written by the language model (ADR-0011).
  Much smaller: assignments, `for` loops, comprehensions, simple
  arithmetic and calls to the plan functions (`search`, `filter_by`,
  `batch`, `fetch_section`) and a few safe built-ins. No imports, no
  attribute access at all, no `while` loops, no function definitions.

Both modes reject the usual ways out of a Python sandbox: any name or
attribute starting with `_` (this is what blocks
`().__class__.__bases__[0].__subclasses__()`), `eval`, `exec`, `compile`,
`open`, `getattr` and friends, `str.format` (it reads attributes at run
time), `global`, `async`, and `with` blocks.

The validator is the first wall. The child process (child.py) is the
second: it only has safe built-ins and allow-listed imports. The operating
system limits and the container are the third.
"""

import ast
from dataclasses import dataclass
from enum import StrEnum


class Mode(StrEnum):
    ANALYSIS = "analysis"
    PLAN = "plan"


MAX_CODE_CHARS = 8_000
MAX_NODES = 2_000

ALLOWED_MODULES = frozenset(
    {
        "math",
        "statistics",
        "collections",
        "datetime",
        "json",
        "re",
        "itertools",
        "functools",
        "operator",
        "string",
        "decimal",
        "fractions",
    }
)

PLAN_FUNCTIONS = frozenset({"search", "filter_by", "batch", "fetch_section"})
SAFE_CALLS = frozenset(
    {
        "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int",
        "isinstance", "len", "list", "map", "max", "min", "print", "range", "reversed",
        "round", "set", "sorted", "str", "sum", "tuple", "zip",
    }
)  # fmt: skip
FORBIDDEN_NAMES = frozenset(
    {
        "eval", "exec", "compile", "open", "input", "breakpoint", "globals", "locals",
        "vars", "getattr", "setattr", "delattr", "hasattr", "memoryview", "help", "exit",
        "quit", "dir", "type", "object", "super", "classmethod", "staticmethod",
        "property", "id", "hash", "__import__", "__builtins__", "__loader__", "__spec__",
    }
)  # fmt: skip

FORBIDDEN_ATTRIBUTES = frozenset({"format", "format_map"})

# Nodes never allowed, in either mode.
_ALWAYS_BANNED: tuple[type[ast.AST], ...] = (
    ast.Global,
    ast.Nonlocal,
    ast.AsyncFunctionDef,
    ast.AsyncFor,
    ast.AsyncWith,
    ast.Await,
    ast.With,
    ast.Yield,
    ast.YieldFrom,
    ast.ClassDef,
    ast.Delete,
)

# Everything a plan may contain.
_PLAN_NODES: tuple[type[ast.AST], ...] = (
    ast.Module, ast.Expr, ast.Assign, ast.AugAssign, ast.Name, ast.Load, ast.Store,
    ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set, ast.ListComp, ast.DictComp,
    ast.comprehension, ast.Call, ast.keyword, ast.BinOp, ast.Add, ast.Sub, ast.Mult,
    ast.Div, ast.FloorDiv, ast.Mod, ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
    ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp,
    ast.Not, ast.USub, ast.If, ast.IfExp, ast.For, ast.Subscript, ast.Slice,
    ast.JoinedStr, ast.FormattedValue, ast.Pass,
)  # fmt: skip


@dataclass(frozen=True)
class Rejected(Exception):  # noqa: N818 - reads better at the call site: `except Rejected`
    """The code may not run. `reason` is safe to show and to give back to a model."""

    reason: str

    def __str__(self) -> str:
        return self.reason


def validate(code: str, mode: Mode) -> ast.Module:
    """Parse and check `code`. Returns the tree, or raises Rejected."""
    if len(code) > MAX_CODE_CHARS:
        raise Rejected(f"The code is longer than {MAX_CODE_CHARS} characters.")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as error:
        raise Rejected(f"Syntax error on line {error.lineno}: {error.msg}.") from None

    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        raise Rejected("The code is too long or too complex.")
    for node in nodes:
        _check(node, mode)
    return tree


def _check(node: ast.AST, mode: Mode) -> None:
    line = getattr(node, "lineno", "?")
    if isinstance(node, _ALWAYS_BANNED):
        raise Rejected(f"Line {line}: `{type(node).__name__}` is not allowed.")
    if mode is Mode.PLAN and not isinstance(node, _PLAN_NODES):
        raise Rejected(f"Line {line}: `{type(node).__name__}` is not allowed in a search plan.")

    if isinstance(node, ast.Name):
        if node.id.startswith("_") or node.id in FORBIDDEN_NAMES:
            raise Rejected(f"Line {line}: the name `{node.id}` is not allowed.")
    elif isinstance(node, ast.Attribute):
        if node.attr.startswith("_"):
            raise Rejected(f"Line {line}: the attribute `{node.attr}` is not allowed.")
        if node.attr in FORBIDDEN_ATTRIBUTES:
            # "{0.__class__}".format(x) reads attributes when it runs, where
            # this check cannot see them. f-strings are checked and do the same job.
            raise Rejected(f"Line {line}: `.{node.attr}()` is not allowed; use an f-string.")
    elif isinstance(node, ast.Import | ast.ImportFrom):
        _check_import(node, line)
    elif isinstance(node, ast.Call) and mode is Mode.PLAN:
        if not isinstance(node.func, ast.Name) or node.func.id not in PLAN_FUNCTIONS | SAFE_CALLS:
            raise Rejected(
                f"Line {line}: a search plan may only call "
                f"{', '.join(sorted(PLAN_FUNCTIONS))} and simple built-ins."
            )
    elif isinstance(node, ast.FunctionDef | ast.Lambda):
        for arg in ast.walk(node.args):
            if isinstance(arg, ast.arg) and arg.arg.startswith("_"):
                raise Rejected(f"Line {line}: the name `{arg.arg}` is not allowed.")
    elif isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) > 2_000:
        # Strings such as "__class__" are fine as data, but not huge ones.
        raise Rejected(f"Line {line}: a text value is too long.")


def _check_import(node: ast.Import | ast.ImportFrom, line: object) -> None:
    if isinstance(node, ast.ImportFrom):
        names = [node.module or ""]
        if node.level:
            raise Rejected(f"Line {line}: relative imports are not allowed.")
        if any(alias.name.startswith("_") or alias.name == "*" for alias in node.names):
            raise Rejected(f"Line {line}: that import is not allowed.")
    else:
        names = [alias.name for alias in node.names]
    for name in names:
        if name.split(".")[0] not in ALLOWED_MODULES:
            raise Rejected(
                f"Line {line}: importing `{name}` is not allowed. Allowed modules: "
                f"{', '.join(sorted(ALLOWED_MODULES))}."
            )
