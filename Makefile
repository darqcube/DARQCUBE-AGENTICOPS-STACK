# DARQCUBE-AGENTICOPS-STACK
# Every command the stack needs. `make` on its own lists them.

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE := docker compose
SOT     := $(COMPOSE) exec -T infrahub-server
AUTO    := $(COMPOSE) exec -T automation
AUTOMATION_PORT ?= 8100

# Load .env so recipes can use its values directly (e.g. $(INFRAHUB_ADMIN_TOKEN)).
ifneq (,$(wildcard .env))
include .env
export
endif

.PHONY: install help preflight up down restart ps logs schema seed render \
        config-get config-put state check snapshot config-parsed \
        test test-templates test-devices verify clean

install: ## Install everything: preflight, configure, build, start, verify
	@python3 install.py

help: ## Show this help
	@echo "DARQCUBE-AGENTICOPS-STACK"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "First run:  cp .env.example .env  &&  edit it  &&  make preflight up schema seed render"

# --- lifecycle -----------------------------------------------------------

preflight: ## Check host prerequisites and .env before starting
	@./scripts/preflight.sh

up: ## Start the stack and WAIT until every container is healthy
	$(COMPOSE) up -d --wait --wait-timeout 300

down: ## Stop the stack (volumes are kept)
	$(COMPOSE) down

restart: ## Restart one service:  make restart SVC=telegraf
	$(COMPOSE) restart $(SVC)

ps: ## Show container status, grouped by function
	@$(COMPOSE) ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'

logs: ## Follow logs, optionally for one service:  make logs SVC=logstash
	$(COMPOSE) logs -f --tail=100 $(SVC)

# --- source of truth -----------------------------------------------------

schema: ## Load the Infrahub schema (source-of-truth/schema/)
	@./source-of-truth/scripts/load-schema.sh

seed: ## Apply source-of-truth/devices/*.yml to Infrahub (idempotent)
	$(SOT) python /scripts/seed.py

render: ## Infrahub -> Telegraf + Logstash configs. Run after any device change.
	$(SOT) python /scripts/render-inventory.py

# --- devices: get and put ------------------------------------------------

config-get: ## Fetch a running config:  make config-get DEV=cr1
	@curl -sf localhost:$(AUTOMATION_PORT)/device/$(DEV)/config | tee automation/configs/$(DEV).cfg

config-put: ## Push config lines:  make config-put DEV=cr1 FILE=change.txt
	@jq -Rs '{lines: split("\n") | map(select(length > 0))}' < $(FILE) \
	  | curl -sf -X POST localhost:$(AUTOMATION_PORT)/device/$(DEV)/config \
	      -H 'Content-Type: application/json' -d @-

state: ## Parsed operational state:  make state DEV=mt-01
	@curl -sf localhost:$(AUTOMATION_PORT)/device/$(DEV)/state | jq .

check: ## Run the assurance rules:  make check DEV=cr1
	@curl -sf -X POST localhost:$(AUTOMATION_PORT)/device/$(DEV)/check | jq .

snapshot: ## Point-in-time state, for pre/post comparison:  make snapshot DEV=cr1
	@curl -sf localhost:$(AUTOMATION_PORT)/device/$(DEV)/snapshot | jq .

config-parsed: ## Running config parsed with TTP:  make config-parsed DEV=cr1
	@curl -sf localhost:$(AUTOMATION_PORT)/device/$(DEV)/config/structured | jq .

# --- tests ---------------------------------------------------------------

test-templates: ## TextFSM templates vs captured samples. No stack, no devices.
	pytest automation/tests/test_templates.py -v

test: ## Stack tests: containers running, services answering, components wired
	pytest automation/tests -v -m "not devices"

test-devices: ## Tests that need real network devices
	pytest automation/tests/test_devices.py -v -m devices

verify: ## Run exactly what docs/INSTALL.md tells you to verify
	@./scripts/verify.sh

# --- housekeeping --------------------------------------------------------

clean: ## Stop the stack and DELETE ALL DATA (volumes included)
	@read -p "Delete all volumes — metrics, logs, Infrahub graph? [y/N] " ok; \
	 [[ $$ok == "y" ]] && $(COMPOSE) down -v || echo "cancelled"
