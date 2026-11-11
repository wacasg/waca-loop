# WACA Loop — developer task runner. Thin wrappers over the commands in README/CLAUDE.md.
.DEFAULT_GOAL := help
PY ?= python
VENV ?= .venv

.PHONY: help setup test test-one lint fmt ddl-dry backfill-dry serve clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## Create venv and install the package (editable) + dev tools
	$(PY) -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -e '.[dev]'
	@echo "Done. Activate with: source $(VENV)/bin/activate"

test: ## Run the full test suite (offline; no cloud creds)
	pytest -q

test-one: ## Run a single test, e.g. make test-one T=tests/test_t02_acl_block.py
	pytest -q $(T)

lint: ## Lint with ruff (must stay clean)
	ruff check .

fmt: ## Autofix lint issues
	ruff check . --fix

ddl-dry: ## Print the resolved BigQuery DDL without applying it
	$(PY) scripts/init_bigquery.py --dry-run

backfill-dry: ## Show what the embedding backfill would do
	$(PY) scripts/backfill_embeddings.py --dry-run

serve: ## Run the MCP server over stdio (needs .env: OAuth + BigQuery)
	waca-loop

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache build dist src/*.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
