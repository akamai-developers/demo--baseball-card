# baseball-card
#
# Laptop: make up-mock, then make test.
#
# `make help` prints the targets in the groups below. "On a laptop" needs no
# GPU and no cloud account. "Development" is for changing this repo.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# The docker command. Plain docker unless you say otherwise. Where this user
# cannot reach the Docker socket without sudo, run make with
# DOCKER="sudo -E docker". -E matters: compose reads GATEWAY_PORT from the
# environment, and plain sudo drops it.
DOCKER ?= docker
COMPOSE := $(DOCKER) compose

# The gateway's port on this machine: the environment's GATEWAY_PORT, else the
# one in .env, else 8443. The mock stack binds it on 127.0.0.1.
GATEWAY_PORT ?= $(or $(shell sed -n 's/^GATEWAY_PORT=//p' .env 2>/dev/null | tail -n 1),8443)
GATEWAY ?= http://localhost:$(GATEWAY_PORT)

# Compose and the tests read these.
export DOCKER GATEWAY_PORT GATEWAY

# How long `up-mock` waits for every service to report loaded. The mock stack
# is up in seconds.
WAIT_S ?= 90

.PHONY: help env up-mock down logs ps test lint

# ---- On a laptop ----------------------------------------------------------
# The mock stack: every service in Docker with stub models, behind Caddy on
# localhost:GATEWAY_PORT. No GPU and no cloud account.

help: ## Show the targets
	@awk 'BEGIN { FS = ":.*## " } \
		/^# ---- / { t = $$0; sub(/^# ---- /, "", t); sub(/ -+$$/, "", t); printf "%s%s\n", (n++ ? "\n" : ""), t; next } \
		/^[a-z][a-zA-Z0-9_-]*:.*## / { printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

env: ## Create .env from .env.example if it is missing, with a new CARD_API_KEY
	@if [ ! -f .env ]; then cp .env.example .env; echo "created .env from .env.example"; fi
	@# umask 077: the temp file holds the key for a moment, and is nobody else's.
	@# printf and read are bash builtins, so the key is on no command line.
	@umask 077; if ! grep -qE '^CARD_API_KEY=.{32,}$$' .env; then \
		key=$$(openssl rand -hex 32); found=; \
		while IFS= read -r line || [ -n "$$line" ]; do \
			case "$$line" in \
				CARD_API_KEY=*) printf 'CARD_API_KEY=%s\n' "$$key"; found=1 ;; \
				*) printf '%s\n' "$$line" ;; \
			esac; \
		done < .env > .env.tmp; \
		[ -n "$$found" ] || printf 'CARD_API_KEY=%s\n' "$$key" >> .env.tmp; \
		mv .env.tmp .env; \
		echo "generated a new CARD_API_KEY into .env"; \
	fi
	@chmod 600 .env

up-mock: env ## Start the mock stack behind Caddy on localhost:GATEWAY_PORT
	$(COMPOSE) --profile mock up -d --build
	@# The Caddyfile is a bind mount, and up does not recreate Caddy when only
	@# the file changed. Caddy reads it at start, so a new route needs a restart.
	$(COMPOSE) --profile mock restart caddy
	@# The key reaches curl on stdin, never on its command line.
	@key=$$(sed -n 's/^CARD_API_KEY=//p' .env | tail -n 1); \
	for i in $$(seq 1 $(WAIT_S)); do \
		body=$$(printf 'Authorization: Bearer %s\n' "$$key" | \
			curl -sf -H @- http://localhost:$(GATEWAY_PORT)/healthz || true); \
		case "$$body" in *'"ok":true'*) echo "stack up at http://localhost:$(GATEWAY_PORT)"; exit 0;; esac; \
		sleep 1; \
	done; \
	echo "the stack did not report healthy in $(WAIT_S)s. try: make logs"; exit 1

down: ## Stop the stack and remove its containers. Its volumes stay
	$(COMPOSE) --profile mock --profile gpu down

logs: ## Follow the container logs
	$(COMPOSE) --profile mock --profile gpu logs -f --tail=100

ps: ## What is running
	$(COMPOSE) --profile mock --profile gpu ps

# ---- Development ----------------------------------------------------------
# For changing this repo: the tests and the lint.

test: ## Run the tests. The integration tests skip until make up-mock
	uv run pytest -q -m "not slow"

lint: ## ruff, the rules and the format
	uv run ruff check .
	uv run ruff format --check .
