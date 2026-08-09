.PHONY: help install init backfill run once dashboard test lint clean docker

VENV   ?= .venv
PYTHON ?= $(VENV)/bin/python

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Create the venv and install dependencies
	python3 -m venv $(VENV)
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt

init:  ## Create or verify the DuckDB schema
	$(PYTHON) -m market_pulse_engine.db.init

backfill:  ## Pull real intraday history into Bronze
	$(PYTHON) -m market_pulse_engine.cli backfill

run:  ## Start the pipeline and dashboard
	$(PYTHON) run.py

once:  ## Run a single pipeline cycle
	$(PYTHON) run.py --once

dashboard:  ## Serve the dashboard only
	$(PYTHON) run.py --dashboard-only

status:  ## Row counts and last run
	$(PYTHON) -m market_pulse_engine.cli status

test:  ## Run the test suite
	$(PYTHON) -m pytest

clean:  ## Remove the database, logs and caches
	rm -rf data/*.duckdb data/*.duckdb.wal logs/*.log
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache

docker:  ## Build and start via docker compose
	docker compose up --build
