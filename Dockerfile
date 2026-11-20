# syntax=docker/dockerfile:1
#
# Image for the API. The UI and MCP server get their own targets in this file
# when they are built; the Python sandbox has a separate Dockerfile because it
# must not contain any project code (see ADR-0009).
#
# Two stages:
#   builder  installs the locked dependencies and the project into /opt/venv
#   api      copies only /opt/venv into a clean image and runs as a non-root user
#
# Build: docker compose build api   (or: docker build --target api -t eka-api .)

ARG PYTHON_IMAGE=python:3.12.16-slim-trixie
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.24

FROM ${UV_IMAGE} AS uv

# ---------------------------------------------------------------
# builder
# ---------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /usr/local/bin/uv

# Compile .pyc files at build time, copy files instead of hard-linking them
# (the cache is a separate mount), and use the image's Python, never a download.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /src

# Dependencies first, in their own layer, so code changes do not reinstall them.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-dev --no-install-project

# Then the project itself, installed as a normal package (not editable), so
# the final image needs only /opt/venv and no source folder.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY api ./api
COPY core ./core
COPY security ./security
COPY rag ./rag
COPY agents ./agents
# Ingestion runs on the host (`make ingest`), but it is part of the same
# Python package, so it is copied to keep the build complete. It is small
# and needs no secrets inside the image.
COPY ingestion ./ingestion
COPY ui ./ui
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---------------------------------------------------------------
# api
# ---------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS api

LABEL org.opencontainers.image.title="enterprise-knowledge-agent-api" \
      org.opencontainers.image.source="https://github.com/madukawala/enterprise-knowledge-agent" \
      org.opencontainers.image.licenses="MIT"

# Install Debian security fixes released after the base image was built
# (the Trivy scan in CI fails on fixable high and critical issues), then
# create a fixed, unprivileged user. The same IDs are used in compose.yaml.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    LOG_FORMAT=json \
    API_HOST=0.0.0.0 \
    API_PORT=8000

USER 10001:10001
WORKDIR /tmp
EXPOSE 8000

# Healthy only when /readyz says the API can serve requests. Python's own
# urllib is used because the slim image has no curl; a 503 raises an error,
# which makes the check fail.
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=4)"]

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
