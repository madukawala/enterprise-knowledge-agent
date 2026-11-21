"""The Python sandbox: runs short pieces of Python that a person or a model wrote.

It runs in its own container (see sandbox/README.md), and this package must
not import anything from the rest of the project: the image is built from
this folder alone, so it holds no project code, no model or database
clients and no keys. import-linter checks the rule in CI.

- validator.py: checks the code before it runs (AST rules).
- runner.py: runs it in a fresh child process with hard limits.
- child.py: the child process itself: restricted built-ins and imports.
- server.py: the small HTTP server the API calls.
"""
