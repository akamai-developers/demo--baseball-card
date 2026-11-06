#!/usr/bin/env bash
# Download the three model repos into the hf-cache volume, before compose starts
# the services. Runs on the node. deploy.sh calls it over ssh.
#
#   ./deploy/scripts/pull-models.sh
#
# Why first, rather than letting each service pull its own weights: compose's
# healthchecks give each service a start_period measured in minutes, and FLUX
# alone is 16 GB. Left to compose, the first `up -d` looks like a hang, and a
# healthcheck-driven restart throws away a part-finished download. Here the
# progress shows and a rerun resumes.
#
# The download runs in vLLM's container, with `docker compose run`: the image
# compose runs, which has Python and huggingface_hub in it already, the
# hf-cache volume compose mounts, and the HF_TOKEN compose hands vLLM. So
# nothing is installed to download, and the token is on no command line.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

# The service whose image, volume and token the download borrows. --no-deps
# starts nothing else, and --rm leaves no container behind.
run() { docker compose run --rm --no-deps "$@"; }

echo "==> downloading the weights into the hf-cache volume"
echo "    BiRefNet 0.4 GB, FLUX.2 klein 16.0 GB, Gemma 4 10.3 GB, at the commits in deploy/models.env"
run --entrypoint python3 -v "$REPO_ROOT/deploy:/deploy:ro" llm-gpu \
	/deploy/scripts/pull_models.py /deploy/models.env

# ---- let the services read and write it -------------------------------------
# vLLM's container runs as root, so everything above landed root-owned at 0755.
# cutout runs as uid 10002 and art as uid 10003, and BiRefNet is loaded with
# trust_remote_code=True, which makes transformers write the module it fetched
# into /hf-cache/modules. Without this the cutout service starts, fails to
# load, and reports:
#
#   PermissionError: [Errno 13] Permission denied: '/hf-cache/modules'
#
# Measured on a node on 2026-09-21. a+rwX and not a chown because two services
# with two different uids share this volume, so there is no one owner to pick.
# X is a capital on purpose: directories become traversable, files do not
# become executable.
echo "==> making the cache writable by the services' users"
run --entrypoint chmod llm-gpu -R a+rwX /hf-cache

echo "==> the weights are in the hf-cache volume"
run --entrypoint du llm-gpu -sh /hf-cache
