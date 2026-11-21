"""The sandbox's AST validator, in both modes, and the plan functions."""

import pytest

from sandbox import child
from sandbox.runner import Limits, run
from sandbox.validator import MAX_CODE_CHARS, Mode, Rejected, validate

PLAN = """
quarters = [("2025-12-01", "2026-02-28"), ("2026-03-01", "2026-05-31")]
searches = [
    search("payment failure outage", ["incidents"],
           filter_by(department="payments", date_from=start, date_to=end),
           label=f"{start} to {end}")
    for start, end in quarters
]
groups = batch(searches, 1)
result = searches + [fetch_section("RB-001", "Steps to fix")]
"""


def test_a_research_plan_is_accepted_and_returns_search_requests() -> None:
    validate(PLAN, Mode.PLAN)
    outcome = run(PLAN, Mode.PLAN, None, Limits(wall_seconds=3))
    assert outcome.ok, outcome
    first, second, fetch = outcome.result
    assert first == {
        "op": "search",
        "query": "payment failure outage",
        "collections": ["incidents"],
        "filters": {"department": "payments", "date_from": "2025-12-01", "date_to": "2026-02-28"},
        "label": "2025-12-01 to 2026-02-28",
    }
    assert second["filters"]["date_from"] == "2026-03-01"
    assert fetch == {"op": "fetch_section", "doc_id": "RB-001", "section": "Steps to fix"}


@pytest.mark.parametrize(
    "code",
    [
        "import math",
        "result = [].append(1)",  # no attribute access in a plan
        "while True:\n    pass",
        "def f():\n    pass",
        "result = lambda: 1",
        "result = open('x')",
        "result = 2 ** 100",  # no powers in a plan
        "try:\n    x = 1\nexcept Exception:\n    pass",
        "result = sorted(data, key=len).copy()",
        "result = data.items()",
    ],
)
def test_a_plan_may_not_do_more_than_plan(code: str) -> None:
    with pytest.raises(Rejected):
        validate(code, Mode.PLAN)


@pytest.mark.parametrize(
    "code",
    [
        "import math\nresult = math.sqrt(16)",
        "from collections import Counter, defaultdict",
        "def total(rows):\n    return sum(r['n'] for r in rows)\nresult = total(data)",
        "result = sorted(data, key=lambda r: r['n'])",
        "n = 0\nwhile n < 3:\n    n += 1",
        "try:\n    x = 1 / 0\nexcept ZeroDivisionError:\n    x = 0",
        'result = f"{len(data)} rows"',
        "result = {k: v for k, v in [('a', 1)]}",
        "result = '__class__'",  # as text it is harmless
    ],
)
def test_ordinary_analysis_code_is_accepted(code: str) -> None:
    validate(code, Mode.ANALYSIS)


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("x = (", "Syntax error on line 1"),
        ("x = 1\n" * (MAX_CODE_CHARS // 4), "longer than"),
        ("result = '" + "a" * 3000 + "'", "too long"),
        ("import os", "importing `os` is not allowed"),
        ("result = data.__dict__", "the attribute `__dict__` is not allowed"),
        ("def f(_x): pass", "the name `_x` is not allowed"),
    ],
)
def test_refusals_say_why(code: str, reason: str) -> None:
    with pytest.raises(Rejected) as caught:
        validate(code, Mode.ANALYSIS)
    assert reason in str(caught.value)


def test_too_many_nodes_is_refused() -> None:
    code = "result = [" + ", ".join(["1"] * 2500) + "]"
    with pytest.raises(Rejected, match="too complex"):
        validate(code, Mode.ANALYSIS)


def test_batch_splits_and_caps() -> None:
    assert child.batch([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert child.batch([1, 2], 0) == [[1], [2]]
    assert len(child.batch(list(range(1000)), 1)) == child.MAX_BATCH_ITEMS


def test_filter_by_leaves_out_what_was_not_given() -> None:
    assert child.filter_by(department="cards") == {"department": "cards"}
