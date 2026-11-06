# baseball-card
#
# Laptop: make up-mock, then make card-mock.
# Akamai Cloud: make gpu-check, deploy, tunnel, check, smoke, then destroy.
#
# `make help` prints the targets in the groups below. "On a laptop" needs no
# GPU and no cloud account. "On Akamai Cloud" makes a GPU node that bills by the
# hour until make destroy. "Development" is for changing this repo.

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

# `make card` reads these. PHOTO is the one you must give.
PHOTO ?=
STATS ?= examples/stats.json
STYLE ?=
OUT ?= out

# The torch base images: CUDA for the node, CPU wheels for BiRefNet on a laptop.
VISION_BASE := baseball-card-vision-base:local
VISION_BASE_CPU := baseball-card-vision-base-cpu:local

.PHONY: help env up-mock down logs ps card-mock card card-fixture up-cpu-cutout \
	card-cpu-cutout gpu-check deploy tunnel check smoke rotate-key destroy ssh gpu-logs \
	web-vendor vision-base vision-base-cpu test test-slow lint

# ---- On a laptop ----------------------------------------------------------
# The mock stack: every service in Docker with stub models, behind Caddy on
# localhost:GATEWAY_PORT. No GPU and no cloud account.

help: ## Show the targets
	@awk 'BEGIN { FS = ":.*## " } \
		/^# ---- / { t = $$0; sub(/^# ---- /, "", t); sub(/ -+$$/, "", t); printf "%s%s\n", (n++ ? "\n" : ""), t; next } \
		/^[a-z][a-zA-Z0-9_-]*:.*## / { printf "  \033[1m%-16s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

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

# A card through the running stack: cardctl posts the photo and the stats to
# the gateway, waits, and writes the card into OUT. On a laptop that is the
# mock stack. With make tunnel open, it is the node.
card-mock: ## The test photo's card through the stack, into out/. STYLE=
	uv run cardctl make --photo tests/fixtures/portrait.jpg --stats examples/stats.json \
		--out out --host $(GATEWAY) $(if $(STYLE),--style $(STYLE),)

card: ## Your card through the stack. PHOTO= (required) STATS= STYLE= OUT=
	@if [ -z "$(PHOTO)" ]; then echo "make card needs a photo: make card PHOTO=me.jpg"; exit 2; fi
	uv run cardctl make --photo "$(PHOTO)" --stats "$(STATS)" --out "$(OUT)" \
		--host $(GATEWAY) $(if $(STYLE),--style $(STYLE),)

# The card on its own: no stack, no models. Playwright's Chromium draws it, and
# the first run downloads it into Playwright's cache. STYLE=akamai or classic.
card-fixture: ## Render the test photo's card into out/. No stack, no models. STYLE=
	uv run playwright install chromium
	uv run python -m card.render --fixture $(if $(STYLE),--style $(STYLE),)

# BiRefNet is the one real model a laptop can run: 444 MB of weights. This is
# the mock stack with the real cutout service in place of the matte, built on
# the CPU torch image. The ledger says local-cpu.
up-cpu-cutout: vision-base-cpu ## The mock stack with the real BiRefNet on this machine's CPU
	@echo "BiRefNet on the CPU. The first run downloads 444 MB of weights into the hf-cache volume."
	CUTOUT_DOCKERFILE=services/cutout/Dockerfile.real \
	CUTOUT_BACKEND=real \
	CUTOUT_GPU_LABEL=local-cpu \
	CUTOUT_START_PERIOD=900s \
	WAIT_S=900 $(MAKE) --no-print-directory up-mock

card-cpu-cutout: up-cpu-cutout ## A card cut out by the real BiRefNet on the CPU, into out/. PHOTO=
	@$(MAKE) --no-print-directory card PHOTO="$(or $(PHOTO),tests/fixtures/portrait.jpg)"

# ---- On Akamai Cloud ------------------------------------------------------
# One GPU node, tagged baseball-card. deploy, rotate-key and destroy refuse to
# run without ALLOW_CLOUD_DEPLOY=1 in .env, and need LINODE_TOKEN in your shell.
# The node bills from make deploy until make destroy: 0.67 USD an hour with
# GPU_COUNT=1, 1.05 with GPU_COUNT=2.

gpu-check: ## Which regions can sell the GPU plan right now, and its price. Read-only
	uv run --quiet deploy/scripts/gpu_check.py

deploy: ## Stand up the GPU node and load the three models. Safe to run again
	./deploy/scripts/deploy.sh

tunnel: ## Forward localhost:GATEWAY_PORT to the node's gateway. Runs until ctrl-c
	./deploy/scripts/tunnel.sh

check: ## Prove the key is enforced and the models hold their GPUs. Read-only
	./deploy/scripts/check.sh

smoke: ## A card on the node, checked to have run on its GPUs, into out/smoke. PHOTO= STATS=
	PHOTO="$(PHOTO)" STATS="$(STATS)" ./deploy/scripts/smoke.sh

rotate-key: ## A new CARD_API_KEY on the node and in .env, and a 401 for the old one
	./deploy/scripts/rotate-key.sh

destroy: ## Delete everything tagged baseball-card, then check nothing is left. FORCE=1
	./deploy/scripts/destroy.sh

ssh: ## A shell on the node, with the repo's own key
	./deploy/scripts/ssh.sh

gpu-logs: ## Follow the node's container logs, or the driver install while it runs
	./deploy/scripts/ssh.sh logs

# ---- Development ----------------------------------------------------------
# For changing this repo: the face detector's files, the torch base images, the
# tests and the lint.

# The page's face detector is not ours. web/vendor.lock pins each file by
# sha256, and this puts them in web/vendor/ for an api run from the checkout.
# The api image downloads its own copy when it is built.
web-vendor: ## Download the face detector into web/vendor/, checked by sha256
	uv run python web/vendor.py

# The base the gpu profile's cutout and art images are built FROM. The node
# builds it before compose does. 15 GB, most of it CUDA libraries.
vision-base: ## Build the CUDA torch base image the node's model services use
	$(DOCKER) build -f services/vision-base.Dockerfile -t $(VISION_BASE) .

vision-base-cpu: ## Build the same base on CPU wheels, for up-cpu-cutout
	$(DOCKER) build -f services/vision-base.Dockerfile -t $(VISION_BASE_CPU) \
		--build-arg BASE_IMAGE=python:3.12-slim-bookworm \
		--build-arg TORCH_INDEX=https://download.pytorch.org/whl/cpu .

test: ## Run the tests. The integration tests skip until make up-mock
	uv run playwright install chromium
	uv run pytest -q -m "not slow"

# torch and BiRefNet's other dependencies go into .venv the first time, about
# 1 GB, and the weights into Hugging Face's cache in your home directory.
test-slow: ## Run BiRefNet on this machine's CPU against the test photo
	uv run --group real-cpu pytest -q -m slow

# shellcheck -x follows the `. lib.sh` in each script. terraform init fetches
# the Linode provider the lock file pins, once, and validate needs it.
lint: ## ruff, shellcheck, terraform fmt and terraform validate
	uv run ruff check .
	uv run ruff format --check .
	shellcheck -x deploy/scripts/*.sh deploy/cloud-init/bootstrap.sh
	terraform -chdir=deploy/terraform fmt -check -recursive
	terraform -chdir=deploy/terraform init -input=false -backend=false -no-color >/dev/null
	terraform -chdir=deploy/terraform validate -no-color
