# syntax=docker/dockerfile:1
#
# The Python sandbox image. Built from the sandbox/ folder only (the build
# context in compose.yaml is ./sandbox), so the image cannot contain any
# other project code, the project's libraries, or a .env file.
#
#   docker compose build sandbox

ARG PYTHON_IMAGE=python:3.12.16-slim-trixie
FROM ${PYTHON_IMAGE}

LABEL org.opencontainers.image.title="enterprise-knowledge-agent-sandbox" \
      org.opencontainers.image.source="https://github.com/madukawala/enterprise-knowledge-agent" \
      org.opencontainers.image.licenses="MIT"

# Security fixes, then an unprivileged user with its own IDs (different
# from the API's, so the two never share file permissions).
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10002 sandbox \
 && useradd --system --uid 10002 --gid sandbox --no-create-home --shell /usr/sbin/nologin sandbox

COPY requirements.txt /tmp/requirements.txt
# Installs only if the file lists something (it is empty today).
RUN if grep -qvE '^[[:space:]]*(#|$)' /tmp/requirements.txt; then \
        pip install --no-cache-dir --no-compile -r /tmp/requirements.txt; \
    fi \
 && rm /tmp/requirements.txt

# The code is owned by root and read-only for the sandbox user.
COPY __init__.py child.py runner.py server.py validator.py /app/sandbox/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SANDBOX_PORT=8100

USER 10002:10002
WORKDIR /app
EXPOSE 8100

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/healthz', timeout=2)"]

# -s: no user site packages; -E: ignore PYTHON* variables.
CMD ["python", "-s", "-E", "-m", "sandbox.server"]
