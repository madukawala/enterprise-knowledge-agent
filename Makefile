# Common development tasks. Run `make` or `make help` to see them all.
# Every target runs through uv, so it uses the versions pinned in uv.lock.

.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck imports test test-unit test-security test-live \
        audit check run-api clean env-file up up-dev up-observability down down-volumes \
        ps logs compose-check scan-image ingest ingest-check search eval-retrieval \
        test-pinecone-local ask chat diagrams stream run-ui token test-keycloak rate-limits test-sandbox eval-answers e2e load

UV_RUN := uv run
# The API that `make stream` and `make run-ui` talk to.
API_URL ?= http://localhost:8000
# Keycloak, for `make token`, `make stream` and `make run-ui`. The demo users
# are in infra/keycloak/realm-helix.json; each password is "<role>-demo-pass".
KEYCLOAK_URL ?= http://localhost:8080
ROLE ?= viewer
DEMO_USER_viewer := vera.viewer
DEMO_USER_analyst := adam.analyst
DEMO_USER_admin := ada.admin
# Exported from uv.lock for pip-audit. Kept in .cache/, which git ignores.
AUDIT_REQUIREMENTS := .cache/requirements-audit.txt

COMPOSE := docker compose
API_IMAGE := enterprise-knowledge-agent/api:local
UI_IMAGE := enterprise-knowledge-agent/ui:local
SANDBOX_IMAGE := enterprise-knowledge-agent/sandbox:local
MCP_IMAGE := enterprise-knowledge-agent/mcp:local
# Trivy runs from its official image, so nothing extra is installed locally.
TRIVY_IMAGE := aquasec/trivy:0.76.2
# Every optional Compose profile, so `make down`, `ps` and `logs` see all services.
ALL_PROFILES := --profile observability --profile pinecone-local

help: ## Show this list
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | \
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

# Runs escape attempts inside the real sandbox container, where the
# container's own walls apply too (no network, read-only files, limits).
test-sandbox: env-file ## Escape tests against the running sandbox container
	$(COMPOSE) up -d --wait --build sandbox
	$(UV_RUN) pytest -m e2e tests/e2e/test_sandbox_container.py -v

# Needs the stack running (make up). Uses no model quota.
e2e: ## Smoke tests against the running stack, including the chat UI in a real browser
	$(UV_RUN) pytest -m e2e tests/e2e -v

# 20 users for one minute; the report is written to reports/ (git-ignored).
load: ## Optional load test against the running stack (Locust, run through uvx)
	@mkdir -p reports
	uvx --from locust==2.37.6 locust -f tests/load/locustfile.py --host $(API_URL) \
		--headless -u 20 -r 5 -t 60s --html reports/locust-report.html

# Signs the demo users in to the real Keycloak and checks their tokens with
# the API's own code. Starts Keycloak first if it is not running.
test-keycloak: env-file ## Login tests against the real Keycloak in Docker
	$(COMPOSE) up -d --wait keycloak
	$(UV_RUN) pytest -m e2e tests/e2e/test_keycloak_login.py -v

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
	$(UV_RUN) bandit -q -c pyproject.toml -r api core security rag ingestion agents evals ui sandbox mcp_server
	@mkdir -p $(dir $(AUDIT_REQUIREMENTS))
	uv export --frozen --all-groups --no-emit-project --format requirements-txt \
		-o $(AUDIT_REQUIREMENTS) -q
	$(UV_RUN) pip-audit --strict --disable-pip -r $(AUDIT_REQUIREMENTS)

check: lint typecheck imports test audit ## Everything CI runs, except gitleaks and Trivy

scan-image: ## Scan the API, UI, sandbox and MCP images for known vulnerabilities (Trivy, same rules as CI)
	$(COMPOSE) build api ui sandbox mcp
	@for image in $(API_IMAGE) $(UI_IMAGE) $(SANDBOX_IMAGE) $(MCP_IMAGE); do \
		docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
			-v trivy-cache:/root/.cache/trivy \
			-v $(CURDIR)/trivy-secret.yaml:/trivy-secret.yaml:ro $(TRIVY_IMAGE) image \
			--secret-config /trivy-secret.yaml \
			--severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 $$image || exit 1; \
	done

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

# Uses real model quota: about 3 calls per question. Writes docs/evals/answers.md.
eval-answers: ## Answer quality: right kind of answer, citations and key facts, per question
	$(UV_RUN) python -m evals.answers

eval-retrieval: ## Hit rate and MRR for several alpha values; saves docs/evals/retrieval.md
	$(UV_RUN) python -m evals.retrieval --report docs/evals/retrieval.md

# ---------------------------------------------------------------
# Agents: ask questions from the terminal
# ---------------------------------------------------------------

# Example: make ask Q="Why did card payments fail in March?" ROLE=analyst
# Needs ANTHROPIC_API_KEY or GEMINI_API_KEY, and the index from `make ingest`.
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
# Running: the API and the UI without Docker
# ---------------------------------------------------------------

run-api: ## Start only the API on http://localhost:8000 with auto-reload (no Docker)
	$(UV_RUN) uvicorn api.main:app --reload --host 127.0.0.1 --port 8000

# Needs the API running (make run-api in another terminal, or make up), and
# Keycloak for sign-in (make up, or docker compose up -d keycloak).
# API_URL picks a different API, e.g. make run-ui API_URL=http://localhost:9000
run-ui: export API_URL := $(API_URL)
run-ui: export KEYCLOAK_URL := $(KEYCLOAK_URL)
run-ui: ## Start the chat UI on http://localhost:8501; reloads when ui/ changes (no Docker)
	$(UV_RUN) streamlit run ui/app.py --server.address 127.0.0.1 --server.port 8501 \
		--server.runOnSave true

# Example: make token ROLE=analyst
# Signs a demo user in to Keycloak (needs `make up`) and prints their access
# token, for trying the API by hand:
#   curl -H "Authorization: Bearer $(make -s token)" http://localhost:8000/me
token: ## Print a 5-minute access token for a demo user: make token ROLE=viewer|analyst|admin
	@test -n "$(DEMO_USER_$(ROLE))" || { echo 'ROLE must be viewer, analyst or admin'; exit 1; }
	@curl -sS -X POST "$(KEYCLOAK_URL)/realms/helix/protocol/openid-connect/token" \
		-d client_id=enterprise-knowledge-ui -d grant_type=password -d scope=openid \
		-d username=$(DEMO_USER_$(ROLE)) -d password=$(ROLE)-demo-pass | \
		$(UV_RUN) python -c 'import json, sys; body = json.load(sys.stdin); print(body.get("access_token") or sys.exit("Sign-in failed: " + str(body.get("error_description", body))))'

# Needs the stack running (make up). Signs in as the demo admin.
rate-limits: ## Show the rate limits in force and whether Redis holds the counts
	@curl -sS -H "Authorization: Bearer $$($(MAKE) -s token ROLE=admin)" "$(API_URL)/admin/rate-limits" | \
		$(UV_RUN) python -m json.tool

# Example: make stream Q="Why did card payments fail in March?" ROLE=analyst
# Needs the stack running (make up). Signs in as the demo user for ROLE,
# then prints the raw server-sent events, as the UI receives them. The
# question is turned into JSON by Python, so quotes in it cannot break the
# request.
stream: export Q := $(Q)
stream: ## Watch an answer stream from the running API: make stream Q="your question" ROLE=analyst
	@test -n "$$Q" || { echo 'Usage: make stream Q="your question" [ROLE=viewer|analyst|admin]'; exit 1; }
	@TOKEN="$$($(MAKE) -s token ROLE=$(ROLE))" && \
	$(UV_RUN) python -c 'import json, os; print(json.dumps({"message": os.environ["Q"]}))' | \
		curl -sSN -X POST "$(API_URL)/chat" -H "Content-Type: application/json" \
		-H "Authorization: Bearer $$TOKEN" --data-binary @-

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
