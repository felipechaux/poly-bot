.PHONY: help setup run open cycle dry-run prices positions trades notify-test test lint install-service uninstall-service docker-build docker-run docker-stop docker-logs docker-status tunnel-url

SHELL := /bin/bash
UV := uv
BOT_DIR := $(shell pwd)
LAUNCHD_PLIST := $(HOME)/Library/LaunchAgents/com.polybot.plist
DATA_DIR := $(BOT_DIR)/data

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*##' Makefile | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ─── Setup ──────────────────────────────────────────────────────────────────

setup:  ## Install dependencies and create .env
	@$(UV) sync
	@if [ ! -f .env ]; then cp .env.example .env; echo "✓ Created .env — fill in your settings"; fi
	@mkdir -p $(DATA_DIR)
	@echo "✓ Setup complete. Edit .env then run: make run"

# ─── Local run ──────────────────────────────────────────────────────────────

run:  ## Start scheduler + dashboard on http://localhost:8080
	$(UV) run poly-bot run --port 8080

open:  ## Open dashboard in browser
	open http://localhost:8080

cycle:  ## Run today's cycle now (executes + notifies)
	$(UV) run poly-bot cycle

dry-run:  ## Simulate today's cycle without executing anything
	$(UV) run poly-bot cycle --dry-run

prices:  ## Universe quotes + trend — usage: make prices REGION=CO
	$(UV) run poly-bot prices $(if $(REGION),--region $(REGION),)

positions:  ## Show paper portfolio
	$(UV) run poly-bot positions

trades:  ## Show recent transactions
	$(UV) run poly-bot trades

notify-test:  ## Send a test ntfy notification
	$(UV) run poly-bot notify-test

# ─── macOS background service (launchd) ─────────────────────────────────────

install-service:  ## Install as macOS background service (auto-starts on login)
	@mkdir -p $(DATA_DIR)
	@sed "s|/opt/homebrew/bin/uv|$(shell which uv)|g" com.polybot.plist > $(LAUNCHD_PLIST)
	@launchctl load -w $(LAUNCHD_PLIST)
	@echo "✓ Service installed. Bot is running in background."
	@echo "  Logs: $(DATA_DIR)/bot.log"
	@echo "  Stop: make uninstall-service"

uninstall-service:  ## Stop and remove the background service
	@launchctl unload -w $(LAUNCHD_PLIST) 2>/dev/null || true
	@rm -f $(LAUNCHD_PLIST)
	@echo "✓ Service removed."

service-status:  ## Check if background service is running
	@launchctl list | grep com.polybot || echo "Service not running"

logs:  ## Tail the background service logs
	@tail -f $(DATA_DIR)/bot.log

logs-errors:  ## Tail error logs from background service
	@tail -f $(DATA_DIR)/bot.error.log

# ─── Docker ─────────────────────────────────────────────────────────────────

docker-build:  ## Build Docker image
	docker compose build

docker-run:  ## Run bot in Docker (paper mode)
	docker compose --profile tunnel up -d --build
	@echo "✓ Bot running in Docker. Logs: make docker-logs"

docker-stop:  ## Stop Docker container
	docker compose down

docker-logs:  ## Tail Docker logs
	docker compose logs -f

docker-status:  ## Show Docker container status
	docker compose ps

tunnel-url:  ## Print the public dashboard URL (Cloudflare quick tunnel)
	@docker compose logs tunnel 2>/dev/null | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1

# ─── Development ────────────────────────────────────────────────────────────

test:  ## Run all tests
	$(UV) run python -m pytest tests/ -v

test-unit:  ## Run unit tests only
	$(UV) run python -m pytest tests/unit/ -v

lint:  ## Run ruff linter
	$(UV) run python -m ruff check poly_bot/ tests/

format:  ## Auto-format with ruff
	$(UV) run python -m ruff format poly_bot/ tests/
