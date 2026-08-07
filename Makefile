# Homestead Digital Twin -- developer and operator entry points.
#
# POSIX-portable: no GNU-only functions, no bashisms, one shell per recipe line.
# `make` with no target prints this file's own help.

.POSIX:
.DEFAULT_GOAL := help

# --- Configuration ---------------------------------------------------------
PYTHON      ?= python3
PIP         ?= $(PYTHON) -m pip
PYTEST      ?= $(PYTHON) -m pytest
RUFF        ?= $(PYTHON) -m ruff
TWIN        ?= PYTHONPATH=src $(PYTHON) -m homestead_twin.cli
COMPOSE     ?= docker compose
COMPOSE_FILE ?= deploy/docker-compose.yml
COMPOSE_SECONDARY ?= deploy/docker-compose.secondary.yml
IMAGE       ?= homestead-twin:local
HOST        ?= 127.0.0.1
PORT        ?= 8000

# Arguments forwarded to `make simulate` and `make cli`:
#   make simulate ARGS="--profile sunny --duration 60"
ARGS ?=

.PHONY: help install install-postgres test test-cov lint format validate \
        init-db load load-registry status export backup retention run serve \
        simulate cli build up down restart logs ps shell secondary-up \
        secondary-down clean distclean ci

# ---------------------------------------------------------------------------
help: ## Show this help
	@echo "Homestead Digital Twin"
	@echo ""
	@echo "Usage: make <target>"
	@echo ""
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | sed -e 's/:.*## /|/' \
	  | awk -F'|' '{ printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2 }'
	@echo ""
	@echo "Quickstart:  make install && make init-db && make load && make run"
	@echo "Variables:   PYTHON, COMPOSE_FILE, IMAGE, HOST, PORT, ARGS"

# --- Development -----------------------------------------------------------
install: ## Install the package and dev extras in editable mode
	$(PIP) install -e ".[dev]"

install-postgres: ## Install the PostgreSQL driver extra as well
	$(PIP) install -e ".[dev,postgres]"

test: ## Run the test suite
	$(PYTEST) -q

test-cov: ## Run the test suite with coverage (needs pytest-cov)
	$(PYTEST) --cov=homestead_twin --cov-report=term-missing

lint: ## Check formatting and lint rules (ruff)
	$(RUFF) check src tests
	$(RUFF) format --check src tests

format: ## Apply ruff formatting and autofixes
	$(RUFF) check --fix src tests
	$(RUFF) format src tests

validate: ## Validate the machine-readable design package against its schemas
	$(PYTHON) tools/validate_bundle.py --no-report

validate-report: ## Validate and refresh the tracked validation_report.json
	$(PYTHON) tools/validate_bundle.py

# --- Platform --------------------------------------------------------------
init-db: ## Create every database table
	$(TWIN) init-db

load-registry: ## Load the design package into the registry
	$(TWIN) load-registry

load: ## Load registry + load schedule + alarm definitions
	$(TWIN) load-all

status: ## Print node role, counts, EMS state and active alarms
	$(TWIN) status

export: ## Export the registry as JSON to var/export.json
	@mkdir -p var
	$(TWIN) export --include registry --output var/export.json

backup: ## Write a portable registry + configuration archive
	$(TWIN) backup

retention: ## Dry-run the data-retention policy (add ARGS="--apply" to commit)
	$(TWIN) retention $(ARGS)

run: serve
serve: ## Run the API on $(HOST):$(PORT) with reload
	$(TWIN) serve --host $(HOST) --port $(PORT) --reload

simulate: ## Run the simulator: make simulate ARGS="--help"
	$(TWIN) simulate -- $(ARGS)

cli: ## Run any CLI subcommand: make cli ARGS="status --json"
	$(TWIN) $(ARGS)

# --- Containers ------------------------------------------------------------
build: ## Build the container image
	docker build -f deploy/Dockerfile -t $(IMAGE) .

up: ## Start the primary-node stack
	@test -f deploy/.env || { echo "deploy/.env is missing. Run: cp deploy/.env.example deploy/.env"; exit 1; }
	$(COMPOSE) -f $(COMPOSE_FILE) up -d

down: ## Stop the primary-node stack (volumes are kept)
	$(COMPOSE) -f $(COMPOSE_FILE) down

restart: ## Restart the primary-node stack
	$(COMPOSE) -f $(COMPOSE_FILE) restart

logs: ## Follow the stack logs
	$(COMPOSE) -f $(COMPOSE_FILE) logs -f --tail=200

ps: ## Show container and health status
	$(COMPOSE) -f $(COMPOSE_FILE) ps

shell: ## Open a shell in the twin container
	$(COMPOSE) -f $(COMPOSE_FILE) exec twin sh

secondary-up: ## Start the secondary control node stack
	@test -f deploy/.env || { echo "deploy/.env is missing. Run: cp deploy/.env.example deploy/.env"; exit 1; }
	$(COMPOSE) -f $(COMPOSE_SECONDARY) --env-file deploy/.env up -d

secondary-down: ## Stop the secondary control node stack
	$(COMPOSE) -f $(COMPOSE_SECONDARY) --env-file deploy/.env down

# --- Housekeeping ----------------------------------------------------------
clean: ## Remove build, cache and test artefacts
	rm -rf .pytest_cache .ruff_cache build dist htmlcov .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name '*.egg-info' -prune -exec rm -rf {} +

distclean: clean ## Also remove the local database and exports under var/
	rm -rf var/homestead.db var/homestead.db-wal var/homestead.db-shm var/export.json

# --- CI --------------------------------------------------------------------
# The gating checks, matching .github/workflows/ci.yml. Lint is deliberately not
# here: it is advisory in CI while subsystems land in parallel, and a target that
# claims to mirror CI while failing on a formatting nit would be a lie. Run
# `make lint` separately.
ci: validate test ## Run the checks that gate CI (validate + tests)
	@echo "CI checks passed. Run 'make lint' separately -- lint is advisory in CI."
