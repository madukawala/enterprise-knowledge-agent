"""Streamlit chat app and agent activity panel. See ui/README.md.

The UI talks to the API only over HTTP and server-sent events. It must never
import `api`, `core`, `security` or any other backend package; CI checks
this with import-linter (see `[tool.importlinter]` in pyproject.toml), and
the UI's Docker image does not contain them.
"""
