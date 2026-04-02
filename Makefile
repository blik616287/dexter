# Dipsea — DEXTER Alert Monitor
#
# Usage:
#   make warehouse                          # start local alert warehouse
#   make live                               # deploy LEAN live on QC cloud
#   make local                              # run LEAN live locally via Docker
#   make backtest                           # run cloud backtest
#   make alerts                             # query stored alerts
#   make stop                               # stop cloud deployment
#   make down                               # stop warehouse

COMPOSE := docker compose -f docker/docker-compose.yml
WAREHOUSE_URL := http://localhost:8080

# QuantConnect LEAN
LEAN_PROJECT := ShoulderTaps
LEAN_DIR := lean
LEAN_NODE ?= L-MICRO node da88255a

.PHONY: help warehouse down nuke \
        live local backtest push stop liquidate status logs \
        alerts stats latest health \
        test lint lint-fix clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Warehouse
# ---------------------------------------------------------------------------

warehouse: ## Start local alert warehouse
	$(COMPOSE) up -d --build
	@echo ""
	@echo "Warehouse:  $(WAREHOUSE_URL)"
	@echo "Query:      make alerts | make stats"

down: ## Stop warehouse
	$(COMPOSE) down

nuke: ## Stop warehouse + delete stored data
	$(COMPOSE) down -v

# ---------------------------------------------------------------------------
# LEAN — Cloud
# ---------------------------------------------------------------------------

push: ## Push LEAN project to QuantConnect cloud
	cd $(LEAN_DIR) && lean cloud push --project $(LEAN_PROJECT)

backtest: ## Run a cloud backtest
	cd $(LEAN_DIR) && lean cloud backtest $(LEAN_PROJECT) \
		--name "backtest-$$(date +%Y%m%d-%H%M%S)" --push

live: ## Deploy live paper trading on QC cloud
	cd $(LEAN_DIR) && lean cloud live deploy $(LEAN_PROJECT) \
		--brokerage "paper trading" \
		--data-provider-live "quantconnect" \
		--node "$(LEAN_NODE)" \
		--auto-restart true \
		--notify-order-events false \
		--notify-insights false \
		--push

stop: ## Stop cloud live trading (keep positions)
	cd $(LEAN_DIR) && lean cloud live stop $(LEAN_PROJECT)

liquidate: ## Stop cloud live + liquidate all positions
	cd $(LEAN_DIR) && lean cloud live liquidate $(LEAN_PROJECT)

# ---------------------------------------------------------------------------
# LEAN — Local
# ---------------------------------------------------------------------------

local: ## Run LEAN live locally via Docker
	cd $(LEAN_DIR) && lean live $(LEAN_PROJECT) \
		--brokerage "paper trading" \
		--data-provider-live "quantconnect"

local-backtest: ## Run LEAN backtest locally via Docker
	cd $(LEAN_DIR) && lean backtest $(LEAN_PROJECT)

# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

status: ## Show cloud live deployment status
	@echo "Live: https://www.quantconnect.com/project/28278775/live"
	cd $(LEAN_DIR) && lean cloud status $(LEAN_PROJECT) 2>/dev/null || true

logs: ## Show recent live algorithm logs
	@python3 python/qc_logs.py

# ---------------------------------------------------------------------------
# Query warehouse
# ---------------------------------------------------------------------------

alerts: ## List all stored alerts
	@curl -s $(WAREHOUSE_URL)/alerts | python3 -m json.tool

alerts-%: ## List alerts for a symbol (e.g. make alerts-aapl)
	@curl -s "$(WAREHOUSE_URL)/alerts?symbol=$(shell echo $* | tr '[:lower:]' '[:upper:]')" | python3 -m json.tool

stats: ## Show per-symbol aggregate stats
	@curl -s $(WAREHOUSE_URL)/alerts/stats | python3 -m json.tool

latest: ## Most recent alert per symbol
	@curl -s $(WAREHOUSE_URL)/alerts/latest | python3 -m json.tool

health: ## Warehouse health check
	@curl -s $(WAREHOUSE_URL)/health | python3 -m json.tool

# ---------------------------------------------------------------------------
# Development
# ---------------------------------------------------------------------------

test: ## Run tests
	python3 -m pytest tests/ -v --tb=short

lint: ## Lint all Python code
	ruff check python/ tests/

lint-fix: ## Auto-fix lint issues
	ruff check --fix python/ tests/

clean: ## Remove build artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true
	find . -type f -name '.coverage' -delete 2>/dev/null || true
	@echo "Clean."
