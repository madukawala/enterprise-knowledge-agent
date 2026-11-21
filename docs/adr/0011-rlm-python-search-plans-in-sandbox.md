# ADR-0011: RLM search plans as restricted Python run in the sandbox

- **Status:** Accepted
- **Date:** 2026-11-20

## Context

The RLM requirement says the research agent should "generate Python-based search plans", decompose large tasks, retrieve targeted sections and call sub-agents recursively. Running LLM-written Python is dangerous: it could read files, call the network or loop forever.

## Decision

- The LLM writes a **short Python plan** that may call only four functions: `search`, `filter_by`, `batch` and `fetch_section`. They return plain dictionaries.
- Before it runs, an **AST validator** allows only assignments, calls to those four functions, literals, list comprehensions and simple arithmetic. Imports, attribute access, `exec`/`eval`, `while` loops and dunder names are rejected.
- The plan runs in the **Python sandbox container**: non-root, read-only file system, no network, CPU, memory and time limits, output size limit. The four functions call back to the API through a narrow internal endpoint that applies the caller's role filters.
- Recursion limits: **depth 3, fan-out 5**, and a token budget per run. When a limit is hit, splitting stops and the agent works with what it has.
- The same sandbox serves the Python analysis tool for Analysts and Admins.

## Changes during implementation (`feature/python-sandbox`)

- **Plan functions return search requests instead of calling the API.** `search()` and `fetch_section()` return dictionaries that describe the work. The sandbox hands the list back, and the API runs each search itself with the user's role filter. The sandbox therefore needs no network at all (not even to the API), and there is no internal endpoint to protect.
- **Two modes share one validator.** Plans use the strict rules above; the Python analysis tool allows ordinary Python with a short list of standard modules. Both refuse names and attributes starting with `_`, `str.format`, and the usual escape functions.
- **Allowed modules are handed out as stand-ins** that hold their functions and values but no other modules, because `statistics.sys`, `fractions.sys` and `json.codecs` lead to `sys.modules` and from there to `os`.
- **The image is built from `sandbox/` alone**, with its own empty `requirements.txt` (standard library only), and import-linter checks that the sandbox and the app never import each other.

## Consequences

- **Positive:** the assignment's "Python-based search plans" are real, visible in traces, and safe.
- **Positive:** permission filters still apply inside the plan, because the functions call the same retrieval layer.
- **Negative:** the plan language is small on purpose. Plans the validator rejects are re-generated once, then the agent falls back to a fixed plan (batch by quarter). This is noted in [ASSUMPTIONS.md](../ASSUMPTIONS.md).
- **Negative:** low depth and fan-out limit how deep research can go on free-tier quotas.

## Alternatives considered

| Option | Why not |
|---|---|
| JSON plans instead of Python | Doesn't meet "Python-based search plans" |
| Run plans in the API process with `exec` | Unsafe; one bad plan could reach secrets or the network |
| A general code interpreter for the LLM | Too much power for the task; larger attack surface |

## Related

[05-rlm-research](../architecture/05-rlm-research.md) · sandbox escape tests in [TESTING.md](../TESTING.md) · [THREAT_MODEL.md](../security/THREAT_MODEL.md)
