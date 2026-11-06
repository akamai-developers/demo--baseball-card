#!/usr/bin/env bash
# Replace CARD_API_KEY on the node and in .env, and prove the old one is dead.
#
#   make rotate-key
#
# One key guards every service, vLLM included, so rotating it is one write and
# one restart. The proof at the end is the point: a rotation that leaves the old
# key working is worse than none, because you would think you were covered.
set -euo pipefail

# shellcheck source-path=SCRIPTDIR source=lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_cmd terraform openssl ssh
require_cloud_deploy
has_state || die "no terraform state, so there is no node to rotate a key on."

OLD_KEY="${CARD_API_KEY:-}"
[ "${#OLD_KEY}" -ge 32 ] || die "there is no usable CARD_API_KEY in .env, so there is nothing to rotate. Run make deploy."

IP="$(node_ip)"
log "rotating the key on $IP"

# The old key has to work first. Rotating away from a key that was already
# broken would hide the real problem.
before="$(node_status_with_key "$OLD_KEY" /healthz)"
[ "$before" = 200 ] || die "the current key gets $before from /healthz already. Fix that before rotating, or you cannot tell whether the rotation worked."
note "the current key works"

NEW_KEY="$(openssl rand -hex 32)"

# ---- the node first ---------------------------------------------------------
# If this fails, .env here still holds the key that works.
log "writing the new key to the node's .env"
printf 'CARD_API_KEY=%s\n' "$NEW_KEY" | ssh_node "
	set -eu
	cd $NODE_DIR
	umask 077
	new=\$(cat)
	tmp=\$(mktemp .env.tmp.XXXXXX)
	grep -v '^CARD_API_KEY=' .env > \"\$tmp\" || true
	printf '%s\n' \"\$new\" >> \"\$tmp\"
	mv \"\$tmp\" .env
"

# ---- restart ----------------------------------------------------------------
# up -d recreates every container whose settings changed, which is all of the
# ones that check the key: the api, cutout, art and gpu-stats read it from .env,
# and vLLM gets it as VLLM_API_KEY and reads it only when it starts.
log "restarting the services"
ssh_node "cd $NODE_DIR && docker compose up -d" >&2

# ---- prove it ---------------------------------------------------------------
log "waiting for the stack to come back"
deadline=$((SECONDS + 900))
until [ "$(node_status_with_key "$NEW_KEY" /healthz)" = 200 ]; do
	[ "$SECONDS" -lt "$deadline" ] || die "the new key never got a 200 from /healthz. The node's .env has the new key and .env here still has the old one. Try: make gpu-logs"
	sleep 10
done
note "the new key works"

# The node answers to the new key now, so .env here is the stale copy. Write it
# before the proofs rather than after: a proof that fails would otherwise leave
# the node holding a key nobody has.
set_env_var CARD_API_KEY "$NEW_KEY"
note "the new key is in .env"

old_code="$(node_status_with_key "$OLD_KEY" /healthz)"
[ "$old_code" = 401 ] || die "the old key still gets $old_code from /healthz. It has not been revoked. Check that every container was recreated: make ssh, then docker compose ps."
note "the old key gets 401 from the api"

# vLLM checks the key itself, so it is the one a restart can leave behind. It
# also takes minutes to load the model again, and until it listens Caddy answers
# /v1/* with 502, which is not a 401. Waiting for the new key to get a 200 first
# is what makes the check below mean "refused" and not "not up yet".
log "waiting for vLLM to load the model again"
until [ "$(node_status_with_key "$NEW_KEY" /v1/models)" = 200 ]; do
	[ "$SECONDS" -lt "$deadline" ] || die "vLLM never served /v1/models again. Both .env files hold the new key, so nothing is lost. Check the container: make gpu-logs"
	sleep 10
done
old_vllm="$(node_status_with_key "$OLD_KEY" /v1/models)"
[ "$old_vllm" = 401 ] || die "the old key still gets $old_vllm from /v1/models. vLLM was not restarted with the new VLLM_API_KEY."
note "the old key gets 401 from vLLM too"

log "rotated. The new key is in .env and on the node, and the old one is dead."
note "an open tunnel keeps working: the key changed, not the forward. An open browser tab signs out on its next call."
