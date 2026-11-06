#!/usr/bin/env bash
# Prove what this demo claims about its front door and its models.
#
#   make check          needs make tunnel running in another terminal, or DOMAIN
#
#   1. the key is enforced: vLLM and the api answer 401 without it or with a
#      wrong one, and 200 with it
#   2. vLLM serves the checkpoint deploy/models.env pins
#   3. every service reports its model loaded, on the card its layout gives it
#   4. nvidia-smi on the node shows memory in use on every card the layout uses
#
# Read-only. It creates nothing and costs nothing. Against the mock stack the
# key checks are the same, and the GPU checks say they do not apply.
set -euo pipefail

# shellcheck source-path=SCRIPTDIR source=lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

load_env
require_cmd curl jq
[ -n "${CARD_API_KEY:-}" ] || die "no CARD_API_KEY in .env. Run make deploy."

GATEWAY="$(node_gateway)"
GPU_COUNT="${GPU_COUNT:-1}"
failures=0
# Set when the gateway turns out to be the mock stack rather than a node.
MOCK_STACK=0

pass() { printf '  \033[32mok\033[0m    %s\n' "$*" >&2; }
fail() {
	printf '  \033[31mFAIL\033[0m  %s\n' "$*" >&2
	failures=$((failures + 1))
}
status() {
	curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$@"
}
expect() {
	local got="$1" want="$2" what="$3"
	if [ "$got" = "$want" ]; then
		pass "$what: $got"
	else
		fail "$what: $got, wanted $want"
	fi
}

# Is anything there at all? /livez is the one open route, so a 200 here with a
# 401 later is a working gateway, and a failure here is a missing tunnel.
if ! curl -sf --max-time 5 -o /dev/null "$GATEWAY/livez"; then
	die "nothing answered $GATEWAY/livez. Run make tunnel in another terminal, or set DOMAIN in .env for domain mode."
fi

# ---- 1. the key -------------------------------------------------------------

log "the key is enforced, at $GATEWAY"
for path in /v1/models /healthz /gpu; do
	expect "$(status "$GATEWAY$path")" 401 "GET $path with no key"
	expect "$(printf 'header = "Authorization: Bearer %s"\n' not-the-key | status --config - "$GATEWAY$path")" 401 "GET $path with a wrong key"
	expect "$(keyed_curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$GATEWAY$path")" 200 "GET $path with the key"
done

# ---- 2. the model vLLM serves ----------------------------------------------

log "the model vLLM serves"
served="$(keyed_curl -sf --max-time 20 "$GATEWAY/v1/models" | jq -r '.data[].id' 2>/dev/null || true)"
# shellcheck source-path=SCRIPTDIR source=../models.env
. "$MODELS_ENV"
if [ "$served" = "$LLM_MODEL" ]; then
	pass "$served, the checkpoint deploy/models.env pins"
elif [ "${served#mock/}" != "$served" ]; then
	# A node runs BACKEND=real and never serves a mock/ model, so this is the
	# mock stack. The key checks above ran the same code the node runs. The GPU
	# checks below do not apply, so they do not count as failures.
	warn "$served: this is the mock stack, not a node. The key checks above are real. The GPU checks below do not apply."
	MOCK_STACK=1
elif [ -n "$served" ]; then
	fail "vLLM serves $served, and deploy/models.env pins $LLM_MODEL. The api asks for the pinned name, and vLLM answers 404 to a name it does not serve."
else
	fail "/v1/models listed no model"
fi

# ---- 3. the services ------------------------------------------------------------

log "what each service says about itself"
health="$(keyed_curl -sf --max-time 15 "$GATEWAY/healthz" || true)"
if [ -z "$health" ]; then
	fail "/healthz answered nothing"
else
	printf '%s\n' "$health" | jq -r '.services | to_entries[] |
		"  \(.key | . + (" " * (7 - length)))  \(.value.status)  \(.value.backend // "-")  on \(.value.ran_on // "-")  \(.value.gpu // "-")  \(.value.vram_mb // "-") MiB"' >&2
	unloaded="$(printf '%s\n' "$health" | jq '[.services[] | select(.loaded != true)] | length')"
	if [ "$unloaded" = 0 ]; then
		pass "every service reports its model loaded"
	else
		fail "$unloaded service(s) do not report loaded"
	fi

	if [ "$MOCK_STACK" = 0 ]; then
		# Each model service names the card it was given. vLLM cannot say, so its
		# label is the one the layout gave the api, which is checked too.
		layout="$REPO_ROOT/deploy/layouts/gpu$GPU_COUNT.env"
		[ -f "$layout" ] || die "GPU_COUNT is $GPU_COUNT, and deploy/layouts/ has no gpu$GPU_COUNT.env."
		for pair in cutout:CUTOUT_GPU art:ART_GPU llm:LLM_GPU; do
			service="${pair%%:*}"
			card="$(sed -n "s/^${pair#*:}=//p" "$layout")"
			ran_on="$(printf '%s\n' "$health" | jq -r --arg s "$service" '.services[$s].ran_on // ""')"
			if [ "$ran_on" = "akamai-gpu$card" ]; then
				pass "$service is on card $card, as gpu$GPU_COUNT.env says"
			else
				fail "$service says ${ran_on:-nothing}, and gpu$GPU_COUNT.env puts it on akamai-gpu$card"
			fi
		done
	fi
fi

# ---- 4. the cards, from the node itself ------------------------------------

log "memory on each card, from nvidia-smi on the node"
if [ "$MOCK_STACK" = 1 ]; then
	warn "skipped: the mock stack has no GPUs. Run this against a node after make deploy."
elif ! has_state; then
	warn "no terraform state, so the nvidia-smi check was skipped. That is expected if GATEWAY is someone else's node."
else
	smi="$(ssh_node 'nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv,noheader,nounits' 2>/dev/null || true)"
	if [ -z "$smi" ]; then
		fail "could not run nvidia-smi on the node over ssh"
	else
		printf '%s\n' "$smi" | awk -F', *' '{ printf "  card %s  %s  %s of %s MiB\n", $1, $2, $3, $4 }' >&2
		# Every card the layout uses, by index. The models are the only thing on
		# them, so a card with under 500 MiB in use holds no model.
		for card in $(seq 0 $((GPU_COUNT - 1))); do
			used="$(printf '%s\n' "$smi" | awk -F', *' -v c="$card" '$1 == c { print $3 + 0 }')"
			if [ "${used:-0}" -gt 500 ]; then
				pass "card $card has $used MiB in use"
			else
				fail "card $card has ${used:-no} MiB in use. A model that loaded is not on it."
			fi
		done
		# Which process holds how much. vLLM's engine, BiRefNet and FLUX each
		# show as a process of their own.
		ssh_node 'nvidia-smi --query-compute-apps=gpu_bus_id,pid,process_name,used_memory --format=csv,noheader' 2>/dev/null \
			| sed 's/^/  /' >&2 || true
	fi
fi

echo >&2
[ "$failures" = 0 ] || die "$failures check(s) failed"
if [ "$MOCK_STACK" = 1 ]; then
	log "the key checks passed. This was the mock stack, so nothing about GPUs was checked."
else
	log "all checks passed"
fi
