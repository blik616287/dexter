# Dexter Alert Monitor
#
# Usage:
#   make up SYMBOLS="AAPL MSFT NVDA"       # start local warehouse + monitors
#   make up SYMBOLS="AAPL MSFT" WEBHOOK_URL="https://alerts-grantsea.pythonanywhere.com/api/alert" WEBHOOK_API_KEY="..."
#                                           # monitors post to external endpoint (no local warehouse)
#   make down                               # tear everything down
#   make test                               # run tests with coverage
#   make lint                               # lint all python code
#   make clean                              # remove build artifacts

SYMBOLS ?= AAPL MSFT
WEBHOOK_URL ?=
WEBHOOK_API_KEY ?=
COMPOSE := docker compose -f docker/docker-compose.yml -f docker/docker-compose.override.yml
OVERRIDE_FILE := docker/docker-compose.override.yml

# If WEBHOOK_URL is set, post to external endpoint (no local warehouse).
# Otherwise, start the local warehouse and post there.
ifdef WEBHOOK_URL
  _ALERT_ENDPOINT := $(WEBHOOK_URL)
  _USE_LOCAL_WAREHOUSE := false
  _PROFILES :=
  WAREHOUSE_QUERY_URL := $(WEBHOOK_URL)
else
  _ALERT_ENDPOINT := http://warehouse:8080/alerts
  _USE_LOCAL_WAREHOUSE := true
  _PROFILES := --profile local
  WAREHOUSE_QUERY_URL := http://localhost:8080
endif

.PHONY: up down status logs alerts stats add remove restart nuke build help test lint clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Generate the override file with monitor services for each symbol
# ---------------------------------------------------------------------------

define generate_override
	@echo "services:" > $(OVERRIDE_FILE)
	@for sym in $(1); do \
		lower=$$(echo $$sym | tr '[:upper:]' '[:lower:]'); \
		echo "  monitor-$$lower:" >> $(OVERRIDE_FILE); \
		echo "    build:" >> $(OVERRIDE_FILE); \
		echo "      context: .." >> $(OVERRIDE_FILE); \
		echo "      dockerfile: docker/Dockerfile.monitor" >> $(OVERRIDE_FILE); \
		echo "    command: [\"$$sym\", \"--webhook-url\", \"$(2)\"]" >> $(OVERRIDE_FILE); \
		if [ -n "$(4)" ]; then \
			echo "    environment:" >> $(OVERRIDE_FILE); \
			echo "      - DEXTER_WEBHOOK_API_KEY=$(4)" >> $(OVERRIDE_FILE); \
		fi; \
		if [ "$(3)" = "true" ]; then \
			echo "    depends_on:" >> $(OVERRIDE_FILE); \
			echo "      warehouse:" >> $(OVERRIDE_FILE); \
			echo "        condition: service_healthy" >> $(OVERRIDE_FILE); \
		fi; \
		echo "    restart: unless-stopped" >> $(OVERRIDE_FILE); \
		echo "" >> $(OVERRIDE_FILE); \
	done
endef

# ---------------------------------------------------------------------------
# Core targets
# ---------------------------------------------------------------------------

build: ## Build images
	$(call generate_override,$(SYMBOLS),$(_ALERT_ENDPOINT),$(_USE_LOCAL_WAREHOUSE),$(WEBHOOK_API_KEY))
	$(COMPOSE) $(_PROFILES) build

up: ## Start monitors (SYMBOLS="AAPL MSFT" WEBHOOK_URL="https://...")
	$(call generate_override,$(SYMBOLS),$(_ALERT_ENDPOINT),$(_USE_LOCAL_WAREHOUSE),$(WEBHOOK_API_KEY))
	@echo ""
ifeq ($(_USE_LOCAL_WAREHOUSE),true)
	@echo "Mode:       local warehouse"
	@echo "Endpoint:   $(_ALERT_ENDPOINT)"
else
	@echo "Mode:       external webhook"
	@echo "Endpoint:   $(_ALERT_ENDPOINT)"
endif
	@echo "Monitors:   $(SYMBOLS)"
	@echo ""
	$(COMPOSE) $(_PROFILES) up -d --build
	@echo ""
ifeq ($(_USE_LOCAL_WAREHOUSE),true)
	@echo "Query:      make alerts | make stats"
endif
	@echo "Logs:       make logs"

down: ## Stop and remove all containers
	$(COMPOSE) --profile local down
	@rm -f $(OVERRIDE_FILE)

nuke: ## Tear down + delete stored alert data
	$(COMPOSE) --profile local down -v
	@rm -f $(OVERRIDE_FILE)

# ---------------------------------------------------------------------------
# Manage individual monitors
# ---------------------------------------------------------------------------

add: ## Add monitors to running stack (SYMBOLS="GOOGL META")
	$(call generate_override,$(SYMBOLS),$(_ALERT_ENDPOINT),$(_USE_LOCAL_WAREHOUSE),$(WEBHOOK_API_KEY))
	$(COMPOSE) $(_PROFILES) up -d --build $(foreach s,$(SYMBOLS),monitor-$(shell echo $(s) | tr '[:upper:]' '[:lower:]'))

remove: ## Remove specific monitors (SYMBOLS="META")
	@for sym in $(SYMBOLS); do \
		lower=$$(echo $$sym | tr '[:upper:]' '[:lower:]'); \
		$(COMPOSE) --profile local rm -fs monitor-$$lower; \
	done

restart: ## Restart specific monitors (SYMBOLS="AAPL")
	@for sym in $(SYMBOLS); do \
		lower=$$(echo $$sym | tr '[:upper:]' '[:lower:]'); \
		$(COMPOSE) --profile local restart monitor-$$lower; \
	done

# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

status: ## Show running containers
	$(COMPOSE) --profile local ps

logs: ## Tail all container logs
	$(COMPOSE) --profile local logs -f --tail=50

logs-%: ## Tail logs for a specific monitor (e.g. make logs-aapl)
	$(COMPOSE) --profile local logs -f --tail=50 monitor-$*

# ---------------------------------------------------------------------------
# Query warehouse
# ---------------------------------------------------------------------------

alerts: ## List all stored alerts
	@curl -s $(WAREHOUSE_QUERY_URL)/alerts | python3 -m json.tool

alerts-%: ## List alerts for a symbol (e.g. make alerts-aapl)
	@curl -s "$(WAREHOUSE_QUERY_URL)/alerts?symbol=$(shell echo $* | tr '[:lower:]' '[:upper:]')" | python3 -m json.tool

stats: ## Show per-symbol aggregate stats
	@curl -s $(WAREHOUSE_QUERY_URL)/alerts/stats | python3 -m json.tool

latest: ## Most recent alert per symbol
	@curl -s $(WAREHOUSE_QUERY_URL)/alerts/latest | python3 -m json.tool

health: ## Warehouse health check
	@curl -s $(WAREHOUSE_QUERY_URL)/health | python3 -m json.tool

# ---------------------------------------------------------------------------
# Development
# ---------------------------------------------------------------------------

test: ## Run tests with coverage
	python3 -m pytest tests/ -v --cov=python --cov-report=term-missing

lint: ## Lint all Python code
	ruff check python/ tests/

lint-fix: ## Auto-fix lint issues
	ruff check --fix python/ tests/

clean: ## Remove build artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true
	find . -type f -name '*.pyo' -delete 2>/dev/null || true
	find . -type f -name '.coverage' -delete 2>/dev/null || true
	rm -rf htmlcov/ .eggs/ *.egg-info/
	rm -f $(OVERRIDE_FILE)
	@echo "Clean."
