# ADR-0009: Docker Compose as the only run mode

- **Status:** Accepted
- **Date:** 2026-11-20

## Context

The system has seven local services (UI, API, MCP server, Python sandbox, Keycloak, Postgres, Redis). Reviewers should be able to start it with one command, and the bonus list includes Docker Compose.

## Decision

- **One `docker compose up`** starts the full stack. No "lite" mode without containers is maintained.
- **Two networks:** `frontend` publishes only the UI (8501), API (8000) and Keycloak (8080); `backend` is internal and holds Postgres, Redis, the sandbox and the MCP server.
- Every service has a **health check** (the API uses `/healthz` and `/readyz`) and `depends_on: condition: service_healthy`, so services start in the right order.
- Images run as **non-root**, use slim base images with pinned tags, and are scanned with Trivy in CI.
- The code is **one Python project** with one `pyproject.toml` and one committed `uv.lock`. The API, UI and MCP server images are built from it.
- The **sandbox image is the exception**. It has its own small dependency file (`sandbox/requirements.txt`, data-analysis libraries only) and copies in only the `sandbox/` folder. It contains no project code, no LLM or vector database clients and no secrets.
- The **UI never imports backend code**. It talks to the API only over HTTP and server-sent events. A CI import check (import-linter, configured in `pyproject.toml`) enforces this.
- An optional **`observability` profile** adds Prometheus and Grafana (`docker compose --profile observability up`).
- Docker Desktop is given 6–8 GB RAM. No model runs locally.
- Secrets come from `.env` (never committed); `.env.example` lists every variable with placeholder values.

## Consequences

- **Positive:** the same stack in the demo, on a reviewer's machine and in end-to-end tests.
- **Positive:** stopping a container is an easy way to show degraded mode live.
- **Negative:** first start downloads several images. Pinned tags keep it repeatable.
- **Negative:** code changes need a rebuild. In development, source folders are mounted and the API runs with reload.

## Alternatives considered

| Option | Why not |
|---|---|
| Run services directly on the Mac | Different setup per reviewer; Keycloak and Postgres installs are slow and fragile |
| Kubernetes (kind / minikube) | Too heavy for a laptop demo; Compose is enough for one host |
| uv workspace with a separate package per service | Cleaner dependency boundaries, but more setup than this project needs. The two boundaries that matter (a minimal sandbox image and a UI separated from the backend) are kept without it. It can be adopted later without changing the design |

## Related

[02-containers](../architecture/02-containers.drawio.svg)
