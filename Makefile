.DEFAULT_GOAL := help
SCENARIO ?= 1
TRIALS ?= 3
PORT ?= 8000

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

install: ## Install dependencies (uv) and git hooks
	uv sync
	uv run pre-commit install || true

# ---------------------------------------------------------------- run the application
start: ## Start the app (UI + API): live if ANTHROPIC_API_KEY works, otherwise demo
	uv run sdlc serve --port $(PORT)

dev: ## Development server with auto-reload
	uv run sdlc serve --port $(PORT) --reload

prod: ## Production server (bind all interfaces, no reload, live mode required)
	uv run sdlc serve --host 0.0.0.0 --port $(PORT) --mode live

ui: ## Demo mode (no API key needed) and open the browser
	@(sleep 2 && uv run python -m webbrowser -t http://127.0.0.1:$(PORT) >/dev/null) &
	uv run sdlc serve --port $(PORT) --mode demo

run: ## One pipeline run in the terminal (live; SCENARIO=1|2|3)
	uv run sdlc run --scenario $(SCENARIO)

demo: ## One pipeline run in the terminal, replaying the curated Scenario 1 example
	uv run sdlc run --scenario 1 --offline --no-interactive

docker-build: ## Build the production image
	docker build -t robert-walters-ai-orchestrator:latest .

docker-run: ## Run the image (reads .env if present) on :8000
	docker compose up --build

# ---------------------------------------------------------------- evaluation
eval: ## Golden scenarios x TRIALS, live, then splice results into docs
	uv run python -m evals.run_eval --trials $(TRIALS) --update-docs

eval-batch: ## Same as eval but through the Message Batches API (50% cheaper, slower)
	uv run python -m evals.run_eval --trials $(TRIALS) --batch --label batch

eval-ablations: ## Re-run with each cost lever switched off (before/after numbers)
	uv run python -m evals.run_eval --trials $(TRIALS) --label all-tier2 --routing-policy all_tier2
	uv run python -m evals.run_eval --trials $(TRIALS) --label no-cache --no-prompt-caching
	uv run python -m evals.run_eval --trials $(TRIALS) --label full-history --context-policy full_history
	uv run python -m evals.run_eval --trials $(TRIALS) --label flat-judge --no-tiered-evaluator

eval-offline: ## Exercise the eval harness with scripted responses (not a measurement)
	uv run python -m evals.run_eval --offline --trials 2 --label offline-selftest

judge-bench: ## Labeled evaluator benchmark: seeded defects vs known-good artifacts
	uv run python -m evals.fault_injection judge --repeats 3

anti-pattern: ## Inject a REST-API-on-mainframe design into a live Scenario 1 run
	uv run python -m evals.fault_injection pipeline

# ---------------------------------------------------------------- quality
test: ## Offline, deterministic test suite with coverage
	uv run pytest --cov --cov-report=term-missing:skip-covered

lint: ## ruff + mypy (strict)
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format: ## Auto-format
	uv run ruff format .
	uv run ruff check --fix .

clean: ## Remove local runtime data and caches
	rm -rf var .pytest_cache .mypy_cache .ruff_cache .coverage

.PHONY: help install start dev prod ui run demo docker-build docker-run eval eval-batch \
	eval-ablations eval-offline judge-bench anti-pattern test lint format clean
