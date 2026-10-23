# ---------------------------------------------------------------------------
# Telegram ↔ BeatAPI bot
# ---------------------------------------------------------------------------
VENV ?= .venv
PYTHON ?= $(VENV)/bin/python
PIP ?= $(VENV)/bin/pip
RUFF ?= $(VENV)/bin/ruff
PYTEST ?= $(VENV)/bin/pytest
COMPOSE ?= docker compose

.DEFAULT_GOAL := help
.PHONY: help check-env venv install install-dev run run-polling lint format test cov check docker-build docker-up docker-down logs clean

help: ## Show the available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

check-env: ## Fail fast when .env is missing
	@test -f .env || { echo "❌ .env not found. Run: cp .env.example .env"; exit 1; }

venv: ## Create the virtual environment
	@test -x $(PYTHON) || python3 -m venv $(VENV)
	@$(PIP) install --quiet --upgrade pip

install: venv ## Install runtime dependencies
	@$(PIP) install --quiet -r requirements.txt
	@echo "✅ Runtime dependencies installed"

install-dev: venv ## Install runtime + development dependencies
	@$(PIP) install --quiet -r requirements-dev.txt
	@echo "✅ Development dependencies installed"

run: check-env install ## Run the bot locally (mode comes from .env)
	$(PYTHON) -m app.main

run-polling: check-env install ## Run the bot with MODE=polling
	MODE=polling $(PYTHON) -m app.main

lint: install-dev ## Lint with ruff
	$(RUFF) check app tests

format: install-dev ## Auto-format with ruff
	$(RUFF) format --line-length 120 app tests
	$(RUFF) check --fix app tests

test: install-dev ## Run the unit tests
	$(PYTEST) -q

cov: install-dev ## Run the tests with coverage
	$(PYTEST) --cov=app --cov-report=term-missing

check: lint test ## Lint + test (what CI runs)

docker-build: ## Build the Docker image
	$(COMPOSE) build

docker-up: check-env ## Start the bot with docker compose
	$(COMPOSE) up -d --build

docker-down: ## Stop the bot
	$(COMPOSE) down

logs: ## Follow the container logs
	$(COMPOSE) logs -f --tail=200

clean: ## Remove caches and the local virtualenv
	rm -rf $(VENV) .pytest_cache .ruff_cache .coverage htmlcov
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	@echo "🧹 Cleaned"
