.PHONY: help setup run stop status logs markets trades test lint install-service uninstall-service

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

run:  ## Start bot + web dashboard on http://localhost:8080
	$(UV) run poly-bot run --port 8080

run-live:  ## Start bot in LIVE mode (requires ENABLE_LIVE_TRADING=true in .env)
	$(UV) run poly-bot run --mode live --port 8080

open:  ## Open dashboard in browser
	open http://localhost:8080

markets:  ## List available Polymarket markets
	$(UV) run poly-bot markets --min-liquidity 5000

trades:  ## Show recent trade history
	$(UV) run poly-bot trades

orderbook:  ## Show order book — usage: make orderbook TOKEN=<token_id>
	$(UV) run poly-bot orderbook $(TOKEN)

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
	docker build -t poly-bot:latest .

docker-run:  ## Run bot in Docker (paper mode)
	docker compose up -d
	@echo "✓ Bot running in Docker. Logs: make docker-logs"

docker-stop:  ## Stop Docker container
	docker compose down

docker-logs:  ## Tail Docker logs
	docker compose logs -f

docker-status:  ## Show Docker container status
	docker compose ps

# ─── Development ────────────────────────────────────────────────────────────

test:  ## Run all tests
	$(UV) run pytest tests/ -v

test-unit:  ## Run unit tests only
	$(UV) run pytest tests/unit/ -v

lint:  ## Run ruff linter
	$(UV) run ruff check poly_bot/ tests/

format:  ## Auto-format with ruff
	$(UV) run ruff format poly_bot/ tests/
