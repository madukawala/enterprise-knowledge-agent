"""Streamlit chat app and agent activity panel (built in `feature/streamlit-ui`).

The UI talks to the API only over HTTP and server-sent events. It must never
import `api`, `core`, `security` or any other backend package; CI checks
this with import-linter (see `[tool.importlinter]` in pyproject.toml).
"""
