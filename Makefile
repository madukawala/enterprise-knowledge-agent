# Common development tasks. Run `make` or `make help` to see them all.
# Every target runs through uv, so it uses the versions pinned in uv.lock.

.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck imports test test-unit test-security test-live \
        audit check run-api clean env-file up up-dev up-observability down down-volumes \
        ps logs compose-check scan-image ingest ingest-check search eval-retrieval \
        test-pinecone-local ask chat diagrams

UV_RUN := uv run
# Exported from uv.lock for pip-audit. Kept in .cache/, which git ignores.
AUDIT_REQUIREMENTS := .cache/requirements-audit.txt

COMPOSE := docker compose
API_IMAGE := enterprise-knowledge-agent/api:local
# Trivy runs from its official image, so nothing extra is installed locally.
TRIVY_IMAGE := aquasec/trivy:0.76.2
# Every optional Compose profile, so `make down`, `ps` and `logs` see all services.
ALL_PROFILES := --profile observability --profile pinecone-local

help: ## Show this list
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install Python 3.12, all dependencies and the git pre-commit hooks
	uv sync --all-groups
	$(UV_RUN) pre-commit install

# ---------------------------------------------------------------
# Code quality
# ---------------------------------------------------------------

lint: ## Check style and common mistakes (ruff)
	$(UV_RUN) ruff check .
	$(UV_RUN) ruff format --check .

format: ## Fix formatting and the lint issues ruff can fix itself
	$(UV_RUN) ruff format .
	$(UV_RUN) ruff check --fix .

typecheck: ## Strict type checks (mypy)
	$(UV_RUN) mypy

imports: ## Check package boundaries, e.g. the UI must not import backend code
	$(UV_RUN) lint-imports

# ---------------------------------------------------------------
# Tests
# ---------------------------------------------------------------

test: ## Unit, integration, graph, security and resilience tests, with the coverage gate
	$(UV_RUN) pytest --cov --cov-report=term-missing --cov-report=xml

test-unit: ## Fast unit tests only
	$(UV_RUN) pytest -m unit

test-security: ## Security tests only
	$(UV_RUN) pytest -m security

# pytest exits with code 5 when no tests match (for example when every live
# test is filtered out), so that case is not treated as a failure.
test-live: ## Tests that call real cloud services (uses your API keys and quota)
	$(UV_RUN) pytest -m live || [ $$? -eq 5 ]

# Starts Pinecone's emulator in Docker, runs the search tests against it,
# then stops it. No account or key needed.
test-pinecone-local: ## Search tests against Pinecone Local (Docker, no account needed)
	$(COMPOSE) --profile pinecone-local up -d --wait pinecone-local
	PINECONE_LOCAL_HOST=http://localhost:5080 $(UV_RUN) pytest tests/integration/test_pinecone_local.py -v; \
		status=$$?; $(COMPOSE) --profile pinecone-local stop pinecone-local; exit $$status

# ---------------------------------------------------------------
# Security checks
# ---------------------------------------------------------------

audit: ## Security lint (bandit) and known vulnerabilities in dependencies (pip-audit)
	$(UV_RUN) bandit -q -c pyproject.toml -r api core security rag ingestion agents evals ui
	@mkdir -p $(dir $(AUDIT_REQUIREMENTS))
	uv export --frozen --all-groups --no-emit-project --format requirements-txt \
		-o $(AUDIT_REQUIREMENTS) -q
	$(UV_RUN) pip-audit --strict --disable-pip -r $(AUDIT_REQUIREMENTS)

check: lint typecheck imports test audit ## Everything CI runs, except gitleaks and Trivy

scan-image: ## Scan the API image for known vulnerabilities (Trivy, same rules as CI)
	$(COMPOSE) build api
	docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
		-v trivy-cache:/root/.cache/trivy $(TRIVY_IMAGE) image \
		--severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 $(API_IMAGE)

# ---------------------------------------------------------------
# Documents: load the sample documents into Pinecone
# ---------------------------------------------------------------

ingest: ## Load data/sample_docs into Pinecone (needs PINECONE_API_KEY). Safe to run again
	$(UV_RUN) python -m ingestion

ingest-check: ## Check and chunk the documents without calling Pinecone (no key needed)
	$(UV_RUN) python -m ingestion --dry-run

# Example: make search Q="why do card payments fail?" ROLE=analyst
# Q and ROLE reach the command as environment variables ("$$Q"), so quotes
# or $(...) in a question are passed on as text, never run by the shell.
ROLE ?= viewer
search: export Q := $(Q)
search: export ROLE := $(ROLE)
search: ## Search the index as a role: make search Q="your question" ROLE=analyst
	@test -n "$$Q" || { echo 'Usage: make search Q="your question" [ROLE=viewer|analyst|admin]'; exit 1; }
	$(UV_RUN) python -m rag.cli "$$Q" --role "$$ROLE"

eval-retrieval: ## Hit rate and MRR for several alpha values; saves docs/evals/retrieval.md
	$(UV_RUN) python -m evals.retrieval --report docs/evals/retrieval.md

# ---------------------------------------------------------------
# Agents: ask questions from the terminal
# ---------------------------------------------------------------

# Example: make ask Q="Why did card payments fail in March?" ROLE=analyst
# Needs GEMINI_API_KEY or OPENROUTER_API_KEY, and the index from `make ingest`.
# Q and ROLE are passed as environment variables, as in `make search`.
ask: export Q := $(Q)
ask: export ROLE := $(ROLE)
ask: ## Ask the agents one question as a role: make ask Q="your question" ROLE=analyst
	@test -n "$$Q" || { echo 'Usage: make ask Q="your question" [ROLE=viewer|analyst|admin]'; exit 1; }
	$(UV_RUN) python -m agents.cli "$$Q" --role "$$ROLE"

chat: export ROLE := $(ROLE)
chat: ## A conversation with the agents in the terminal (follow-up questions work)
	$(UV_RUN) python -m agents.cli --chat --role "$$ROLE"

diagrams: ## Draw the agent graph from the code into docs/architecture/03-agent-graph.generated.md
	$(UV_RUN) python -m agents.diagram

# ---------------------------------------------------------------
# Running: the API on its own
# ---------------------------------------------------------------

run-api: ## Start only the API on http://localhost:8000 with auto-reload (no Docker)
	$(UV_RUN) uvicorn api.main:app --reload --host 127.0.0.1 --port 8000

# ---------------------------------------------------------------
# Running: the full stack with Docker Compose
# ---------------------------------------------------------------

env-file:
	@test -f .env || { echo "No .env file. Create one first:  cp .env.example .env"; exit 1; }

up: env-file ## Build and start the stack, and wait until every service is healthy
	$(COMPOSE) up -d --build --wait

up-dev: env-file ## Same as up, but the API reloads when you edit code on your machine
	$(COMPOSE) -f compose.yaml -f compose.dev.yaml up -d --build --wait

up-observability: env-file ## Same as up, plus Prometheus (:9090) and Grafana (:3000)
	$(COMPOSE) --profile observability up -d --build --wait

down: ## Stop the stack and keep the data
	$(COMPOSE) $(ALL_PROFILES) down

down-volumes: ## Stop the stack and delete all local data (database, Keycloak, metrics)
	$(COMPOSE) $(ALL_PROFILES) down -v

ps: ## Show each service and its health
	$(COMPOSE) $(ALL_PROFILES) ps

logs: ## Follow the logs of every service (Ctrl+C to stop)
	$(COMPOSE) $(ALL_PROFILES) logs -f --tail=100

compose-check: env-file ## Check compose.yaml for mistakes without starting anything
	$(COMPOSE) config --quiet
	$(COMPOSE) $(ALL_PROFILES) config --quiet
	$(COMPOSE) -f compose.yaml -f compose.dev.yaml config --quiet

clean: ## Remove caches and test output
	rm -rf .cache .pytest_cache .mypy_cache .ruff_cache .import_linter_cache \
		.coverage coverage.xml htmlcov
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
