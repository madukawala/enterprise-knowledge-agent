# Common development tasks. Run `make` or `make help` to see them all.
# Every target runs through uv, so it uses the versions pinned in uv.lock.

.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck imports test test-unit test-security test-live \
        audit check run-api clean

UV_RUN := uv run
# Exported from uv.lock for pip-audit. Kept in .cache/, which git ignores.
AUDIT_REQUIREMENTS := .cache/requirements-audit.txt

help: ## Show this list
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

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

# pytest exits with code 5 when no tests match, which is expected until the
# first live tests are added, so that case is not treated as a failure.
test-live: ## Tests that call real cloud services (uses your API keys and quota)
	$(UV_RUN) pytest -m live || [ $$? -eq 5 ]

# ---------------------------------------------------------------
# Security checks
# ---------------------------------------------------------------

audit: ## Security lint (bandit) and known vulnerabilities in dependencies (pip-audit)
	$(UV_RUN) bandit -q -c pyproject.toml -r api core security ui
	@mkdir -p $(dir $(AUDIT_REQUIREMENTS))
	uv export --frozen --all-groups --no-emit-project --format requirements-txt \
		-o $(AUDIT_REQUIREMENTS) -q
	$(UV_RUN) pip-audit --strict --disable-pip -r $(AUDIT_REQUIREMENTS)

check: lint typecheck imports test audit ## Everything CI runs, except gitleaks

# ---------------------------------------------------------------
# Running
# ---------------------------------------------------------------

run-api: ## Start the API on http://localhost:8000 with auto-reload
	$(UV_RUN) uvicorn api.main:app --reload --host 127.0.0.1 --port 8000

clean: ## Remove caches and test output
	rm -rf .cache .pytest_cache .mypy_cache .ruff_cache .import_linter_cache \
		.coverage coverage.xml htmlcov
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
