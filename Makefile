.PHONY: help up down logs restart build clean trigger sh-orchestrator sh-redis dashboard-open lint

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' Makefile | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

up: ## Start the stack (orchestrator + dashboard, using Redis Cloud from .env)
	docker compose up --build

up-d: ## Start the stack detached
	docker compose up --build -d

up-local-redis: ## Start the stack including a local Redis Stack (instead of Redis Cloud)
	docker compose --profile local-redis up --build

down: ## Stop the stack
	docker compose down

logs: ## Tail logs for all services
	docker compose logs -f

restart: ## Restart orchestrator (keeps Redis state)
	docker compose restart orchestrator

build: ## Rebuild images
	docker compose build

clean: ## Stop and remove all data
	docker compose down -v

trigger: ## Fire a demo trigger
	curl -sX POST http://localhost:8080/trigger/demo | jq .

sh-orchestrator: ## Shell into orchestrator container
	docker compose exec orchestrator bash

sh-redis: ## Open redis-cli inside the redis container
	docker compose exec redis redis-cli

dashboard-open: ## Open the Streamlit dashboard
	open http://localhost:8501
