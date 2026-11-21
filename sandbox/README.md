# sandbox

The Python sandbox: a separate container that runs short pieces of Python written by analysts (the Python analysis tool) and by the research agent (search plans, ADR-0011). Nothing else in the system ever runs code that a person or a model wrote.

## How a run is protected

There are four walls. Each one alone would stop most attacks; together they leave very little room.

| Wall | What it does | Where |
|---|---|---|
| 1. Check before running | An AST check refuses imports outside a short list, any name or attribute starting with `_`, `eval`/`exec`/`open`/`getattr` and similar, `str.format`, `global`, `class` and `with`. Plans are stricter still: no imports, no attribute access, no `while`, no functions | [validator.py](validator.py) |
| 2. A restricted interpreter | The code runs with only safe built-ins. `import` works for the allowed modules only, and gives back a stand-in that holds the module's functions but none of the modules it points at (so `statistics.sys` does not exist) | [child.py](child.py) |
| 3. A fresh process with limits | Every run is a new process with an empty environment (no keys to find), its own empty folder, CPU, memory, file-size and process limits, and a 5-second wall-clock kill | [runner.py](runner.py), [child.py](child.py) |
| 4. The container | Non-root user (10002), read-only file system, every Linux capability dropped, 256 MB and half a CPU, at most 32 processes, and an internal network shared with the API alone: no internet, no Postgres, Redis or Keycloak | [Dockerfile](Dockerfile), [compose.yaml](../compose.yaml) |

The image is built from this folder alone (`context: ./sandbox`), so it holds no project code, none of the project's libraries and no `.env`. Its own [requirements.txt](requirements.txt) is empty: everything it uses is in Python's standard library.

## Using it

The API calls it; nothing else can reach it.

```
POST /run   {"code": "...", "mode": "analysis" | "plan", "data": <any JSON>}
         -> {"ok", "result", "stdout", "error", "status", "duration_ms"}
GET /healthz
```

The code gets `data` as a variable and answers by setting `result`. `print()` output comes back as `stdout`. `status` is `ok`, `rejected` (wall 1, with the reason), `error` (the code raised an exception), `timeout` or `crashed` (a limit was hit).

**Analysis mode** (the Python tool, `POST /tools/python` on the API, analysts and admins only):

```python
from collections import Counter
import statistics

result = {
    "by_service": Counter(row["service"] for row in data).most_common(),
    "mean_minutes": statistics.mean(row["minutes"] for row in data),
}
```

Allowed modules: `math`, `statistics`, `collections`, `datetime`, `json`, `re`, `itertools`, `functools`, `operator`, `string`, `decimal`, `fractions`.

**Plan mode** (the research agent). The plan functions do not search: they return search requests as data, and the API runs them afterwards with the user's role filter. So a plan never needs the network, and it cannot widen what the user may read.

```python
quarters = [("2025-12-01", "2026-02-28"), ("2026-03-01", "2026-05-31")]
result = [
    search(
        "payment failure outage",
        ["incidents"],
        filter_by(department="payments", date_from=start, date_to=end),
    )
    for start, end in quarters
]
```

| Function | Returns |
|---|---|
| `search(query, collections, filters, label)` | `{"op": "search", ...}` |
| `filter_by(department, date_from, date_to)` | a filters dict |
| `fetch_section(doc_id, section)` | `{"op": "fetch_section", ...}` |
| `batch(items, size)` | the items in groups of `size` (at most 50 groups) |

## Running and testing

```bash
make up                 # starts the sandbox with the rest of the stack
make test-sandbox       # escape tests inside the running container
```

| Tests | What they prove |
|---|---|
| `tests/unit/test_sandbox_validator.py` | Both modes: what is allowed, what is refused and why; the plan functions |
| `tests/security/test_sandbox_escapes.py` | 26 escapes refused before running; module paths to `sys`/`os` closed; endless loops, huge numbers, deep recursion and huge output stopped; empty environment |
| `tests/integration/test_python_tool_api.py` | The API endpoint: roles, bad requests, sandbox missing or down, rate limit, metrics |
| `tests/e2e/test_sandbox_container.py` | In the real container: no network, read-only files, user 10002, no keys, no project code, memory bomb stopped |

The memory limit is enforced by Linux; on macOS the local memory test is skipped, and the container test covers it.
