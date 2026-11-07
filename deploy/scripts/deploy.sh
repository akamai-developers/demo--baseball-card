#!/usr/bin/env bash
# Stand up the GPU node and load the models. Safe to run again.
#
#   make deploy
#
# What it does, in order:
#   1. preflight: the cloud-deploy flag, the token, the tools, and gpu-check
#   2. a deploy keypair at secrets/ssh/id_ed25519, and an API key in .env
#   3. terraform apply: one instance and one firewall, both tagged baseball-card
#   4. wait for ssh, then for cloud-init's reboot, then for the GPUs
#   5. rsync the repo to /opt/baseball-card
#   6. write the node's .env at mode 600
#   7. pull 27 GB of weights into the hf-cache volume
#   8. build the torch base image, then bring up the gpu profile
#   9. wait until every service reports its model loaded
#
# The node bills from step 3 until make destroy: 0.67 USD an hour with
# GPU_COUNT=1, 1.05 with GPU_COUNT=2, on the price list of 2026-11-06.
set -euo pipefail

# shellcheck source-path=SCRIPTDIR source=lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

# How long to wait for each stage. The driver install plus a reboot is the long
# one, and a cold start reads 27 GB off the disk and onto the cards.
SSH_WAIT_S="${SSH_WAIT_S:-600}"
BOOTSTRAP_WAIT_S="${BOOTSTRAP_WAIT_S:-1800}"
HEALTH_WAIT_S="${HEALTH_WAIT_S:-1800}"

# How long each stage took, printed at the end.
TIMES=()
stage_started=0
stage() {
	log "$1"
	stage_started=$SECONDS
}
stage_done() { TIMES+=("$1 $((SECONDS - stage_started)) s"); }

# ---- 1. preflight -----------------------------------------------------------

stage "preflight"
require_cmd terraform ssh rsync curl openssl ssh-keygen jq uv
require_cloud_deploy

# A read-only call. If the token is wrong, find out now rather than after
# terraform has half-built something.
linode_api /profile >/dev/null || die "the Linode API refused LINODE_TOKEN. Check the token, and that it can read and write Linodes and Firewalls."
note "LINODE_TOKEN works"

REGION="${LINODE_REGION:-us-ord}"
GPU_COUNT="${GPU_COUNT:-1}"
LINODE_TYPE="${LINODE_TYPE:-}"
DOMAIN="${DOMAIN:-}"
MODE=tunnel
[ -z "$DOMAIN" ] || MODE=domain
# By the names .env gives them, so a refusal names the line to fix.
require_plain LINODE_REGION "$REGION"
require_plain GPU_COUNT "$GPU_COUNT"
require_plain LINODE_TYPE "$LINODE_TYPE"
require_plain DOMAIN "$DOMAIN"

# Can the region sell the plan this minute? It prints the table either way.
# Nothing has been created yet, so stopping here costs nothing.
log "can $REGION sell the plan right now?"
gpu_check=0
LINODE_REGION="$REGION" GPU_COUNT="$GPU_COUNT" LINODE_TYPE="$LINODE_TYPE" \
	uv run --quiet "$SCRIPT_DIR/gpu_check.py" >&2 || gpu_check=$?
case "$gpu_check" in
0) ;;
3) die "$REGION cannot sell the plan right now. The table above names the regions that can. Nothing was created." ;;
2) die "the settings in .env pick no plan this repo can run on. The line above says why. Nothing was created." ;;
*) die "make gpu-check could not answer, so nothing was created. The line above says why." ;;
esac
stage_done preflight

# ---- 2. keys ----------------------------------------------------------------

stage "keys"
ensure_ssh_key
note "ssh key secrets/ssh/id_ed25519"
ensure_api_key
note "CARD_API_KEY $KEY_STATE, in .env"

# The addresses allowed to reach port 22: ADMIN_CIDR from .env, one or more
# IPv4 blocks separated by commas, or else this machine's public address as a
# /32. Akamai's own echo service says which, over IPv4 because the rule is
# IPv4. It is asked even when .env pins ADMIN_CIDR, because every ssh below
# comes from this machine: a list with no block for it is a firewall that
# drops this script's own connections, found out ten minutes into a node that
# bills by the hour. Seen on 2026-11-07, with a laptop's address pinned in the
# .env of the box the deploy ran on.
detected="$(curl -4 -sf --max-time 10 https://whatismyip.akamai.com || true)"
[[ "$detected" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || detected=""
ADMIN_BLOCKS=()
if [ -z "${ADMIN_CIDR:-}" ]; then
	[ -n "$detected" ] \
		|| die "could not find this machine's public IPv4 address. Set ADMIN_CIDR in .env, for example ADMIN_CIDR=203.0.113.4/32."
	ADMIN_CIDR="$detected/32"
	ADMIN_BLOCKS=("$ADMIN_CIDR")
	note "ADMIN_CIDR $ADMIN_CIDR, detected. Put it in .env to pin it."
else
	# A space after a comma is a habit, not an error.
	ADMIN_CIDR="${ADMIN_CIDR// /}"
	require_plain ADMIN_CIDR "${ADMIN_CIDR//,/}"
	IFS=, read -r -a ADMIN_BLOCKS <<<"$ADMIN_CIDR"
	[ "${#ADMIN_BLOCKS[@]}" -gt 0 ] || die "ADMIN_CIDR in .env is only commas. Nothing was created."
	for block in "${ADMIN_BLOCKS[@]}"; do
		[ -n "$block" ] || die "ADMIN_CIDR in .env has an empty entry between two commas. Nothing was created."
		[[ "$block" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/([0-9]|[12][0-9]|3[0-2])$ ]] \
			|| die "ADMIN_CIDR in .env holds $block, which is not an IPv4 CIDR block such as 203.0.113.4/32. Blocks are separated by commas. Nothing was created."
	done
	if [ -z "$detected" ]; then
		warn "could not check ADMIN_CIDR against this machine's address: the echo did not answer. If ssh times out below, no block in $ADMIN_CIDR holds this machine."
		note "ADMIN_CIDR $ADMIN_CIDR, from .env"
	else
		inside=""
		for block in "${ADMIN_BLOCKS[@]}"; do
			if ip_in_cidr "$detected" "$block"; then
				inside="$block"
				break
			fi
		done
		[ -n "$inside" ] \
			|| die "ADMIN_CIDR in .env is $ADMIN_CIDR, and this machine's public IPv4 address is $detected. No block in it holds $detected, and every ssh in this script comes from $detected, so the firewall would drop it and the wait for ssh would time out. Add $detected/32 to ADMIN_CIDR, or set ADMIN_CIDR= empty in .env to use $detected/32 alone. Nothing was created."
		note "ADMIN_CIDR $ADMIN_CIDR, from .env. This machine is $detected, inside $inside."
	fi
fi
# The blocks as an HCL list, for the tfvars below.
admin_hcl=""
for block in "${ADMIN_BLOCKS[@]}"; do
	admin_hcl="$admin_hcl${admin_hcl:+, }\"$block\""
done
stage_done keys

# ---- 3. terraform -----------------------------------------------------------

stage "terraform"

# Written rather than passed with -var, for three reasons: destroy.sh needs the
# same values and two of them have no default, a reader can run terraform plan
# by hand in deploy/terraform and see the real plan, and a rerun with the same
# .env is a plan with no changes. It is gitignored by the *.tfvars rule. None of
# it is secret: a public key, the admin addresses and four settings.
cat >"$TF_DIR/deploy.auto.tfvars" <<EOF
# Written by deploy/scripts/deploy.sh from .env. Edit .env, not this file.
region         = "$REGION"
gpu_count      = $GPU_COUNT
instance_type  = "$LINODE_TYPE"
domain         = "$DOMAIN"
admin_cidrs    = [$admin_hcl]
ssh_public_key = "$(cat "$SSH_KEY.pub")"
EOF

tf init -input=false -no-color >/dev/null
before="$(node_in_state)"
applied=1
tf apply -input=false -auto-approve -no-color || applied=0
after="$(node_in_state)"

# Linode hands out addresses again. Destroy a node and deploy another, and the
# new one can land on the address the old one had, with a different host key,
# and then every script here stops with REMOTE HOST IDENTIFICATION HAS CHANGED.
# Seen on 2026-09-21: the fourth instance of the day got the first one's
# address.
#
# So when this apply made the instance, or replaced it, forget the address
# before the first connection. A rerun against the same instance keeps the key
# it learned the first time, and a node whose key changes under it stops every
# script, as it should. This runs even when the apply failed, because an apply
# can make the instance and then fail on something else, and the next run would
# see no change. secrets/ssh/known_hosts is this repo's own file. Your
# ~/.ssh/known_hosts is never touched.
if [ -n "$after" ] && [ "$after" != "$before" ]; then
	new_ip="${after#* }"
	if [ -f "$KNOWN_HOSTS" ] && ssh-keygen -F "$new_ip" -f "$KNOWN_HOSTS" >/dev/null 2>&1; then
		ssh-keygen -R "$new_ip" -f "$KNOWN_HOSTS" >/dev/null 2>&1
		note "forgot the host key for $new_ip. The instance is new, and an older node had its address."
	fi
fi
[ "$applied" = 1 ] || die "terraform apply failed. The lines above say why. Fix it and run make deploy again."

IP="$(node_ip)"
NODE_USER="$(tf_output deploy_user)"
TYPE="$(tf_output instance_type)"
log "node $(tf_output label) at $IP, $TYPE in $(tf_output region)"
if [ "$MODE" = domain ]; then
	note "point DNS at it now: $DOMAIN A $IP. Caddy asks Let's Encrypt for a certificate when the stack starts, about 20 minutes from now."
fi
stage_done terraform

# ---- 4. wait for the node ---------------------------------------------------

stage "waiting for ssh (up to $((SSH_WAIT_S / 60)) min)"
deadline=$((SECONDS + SSH_WAIT_S))
ssh_err=""
until ssh_err="$(ssh_node true 2>&1 >/dev/null)"; do
	[ "$SECONDS" -lt "$deadline" ] || {
		note "last ssh error: ${ssh_err:-none}"
		die "no ssh on $IP after $((SSH_WAIT_S / 60)) minutes. If the error above is a timeout, the address this machine leaves by is not in ADMIN_CIDR ($ADMIN_CIDR). Anything else is a problem on this side."
	}
	sleep 5
done
note "ssh is up"
stage_done ssh

# cloud-init installs the driver, writes the "installed" marker, then reboots.
# bootcmd turns that into "ready" on the next boot. Waiting for "ready" rather
# than for cloud-init to finish is what keeps this from copying the repo onto a
# box that is about to go down.
stage "waiting for the driver install and the reboot (up to $((BOOTSTRAP_WAIT_S / 60)) min)"
note "watch it with: make gpu-logs"
deadline=$((SECONDS + BOOTSTRAP_WAIT_S))
until ssh_node "test -f $READY_MARKER" 2>/dev/null; do
	[ "$SECONDS" -lt "$deadline" ] || {
		warn "cloud-init did not finish. The end of its log:"
		ssh_node "sudo tail -30 $BOOTSTRAP_LOG 2>/dev/null; sudo cloud-init status --long 2>/dev/null" >&2 || true
		die "the node never reported ready."
	}
	sleep 10
done
note "the driver is in and the node has rebooted"

gpus="$(ssh_node 'nvidia-smi -L 2>/dev/null | wc -l')"
gpus="${gpus//[!0-9]/}"
[ "${gpus:-0}" -ge "$GPU_COUNT" ] || {
	ssh_node 'nvidia-smi 2>&1 | head -20' >&2 || true
	die "nvidia-smi lists ${gpus:-0} GPUs, and GPU_COUNT=$GPU_COUNT needs $GPU_COUNT."
}
ssh_node 'nvidia-smi -L' >&2
stage_done "driver and reboot"

# ---- 5. the repo ------------------------------------------------------------

stage "copying the repo to $NODE_DIR"
set_ssh_opts
# rsync splits -e on spaces and keeps what is quoted, so each option is quoted.
rsh="ssh$(printf " '%s'" "${SSH_OPTS[@]}")"
# What git ignores stays here: .env (the node gets its own), secrets/, the
# terraform state, weights, and everything a laptop built. --delete removes
# what is no longer in the repo, so a deploy cannot leave a stale file behind.
# An excluded file on the node is never deleted, so the node's .env survives.
rsync -az --delete -e "$rsh" \
	--exclude=.git/ \
	--exclude-from="$REPO_ROOT/.gitignore" \
	"$REPO_ROOT/" "$NODE_USER@$IP:$NODE_DIR/"
stage_done "copy"

# ---- 6. the node's .env -----------------------------------------------------

stage "writing the node's .env"
# Built in a variable, not a file, and sent over ssh's stdin. A setting node_env
# refuses stops the run here, before the node's .env is touched.
body="$(node_env)"
printf '%s\n' "$body" | ssh_node "umask 077 && cat > $NODE_DIR/.env.tmp && mv $NODE_DIR/.env.tmp $NODE_DIR/.env"
body=""
note "$NODE_DIR/.env, mode 600, owned by $NODE_USER"
stage_done ".env"

# ---- 7. weights -------------------------------------------------------------

stage "pulling the model weights, 27 GB the first time"
ssh_node "cd $NODE_DIR && ./deploy/scripts/pull-models.sh" >&2
stage_done weights

# ---- 8. build and start -----------------------------------------------------

# cutout and art use this as a FROM, and compose does not order builds by FROM.
stage "building the images. The torch base is 15 GB the first time"
ssh_node "cd $NODE_DIR && docker build -f services/vision-base.Dockerfile -t baseball-card-vision-base:local ." >&2
ssh_node "cd $NODE_DIR && docker compose build" >&2
stage_done images

stage "starting the stack"
ssh_node "cd $NODE_DIR && docker compose up -d" >&2
# The Caddyfile is a bind mount, and compose does not recreate a container when
# a mounted file changes. On a node that is already running, a new route would
# sit in the file and never be served. A restart, not `caddy reload`, because
# the Caddyfile sets `admin off`. The certificate lives in the caddy-data
# volume, so no new one is asked for.
ssh_node "cd $NODE_DIR && docker compose restart caddy" >&2

# ---- 9. wait for the models -------------------------------------------------

log "waiting for every service to report its model loaded (up to $((HEALTH_WAIT_S / 60)) min)"
note "vLLM captures CUDA graphs on the way up, and FLUX reads 16 GB"
deadline=$((SECONDS + HEALTH_WAIT_S))
while true; do
	health="$(node_healthz)"
	case "$health" in *'"ok":true'*) break ;; esac
	if [ "$SECONDS" -ge "$deadline" ]; then
		warn "not every service came up. This is what /healthz said:"
		printf '%s\n' "$health" >&2
		ssh_node "cd $NODE_DIR && docker compose ps" >&2 || true
		die "the stack did not report healthy. Try: make gpu-logs"
	fi
	sleep 10
done
printf '%s\n' "$health" | jq -r '.services | to_entries[] |
	"    \(.key)  \(.value.model)  on \(.value.ran_on // "-")  \(.value.vram_mb // "-") MiB"' >&2 2>/dev/null || true
stage_done "models"

# The gateway, from the node itself. In domain mode this is the first moment
# Caddy can have a certificate, and it waits for DNS.
gateway_code="$(ssh_node "curl -s -o /dev/null -w '%{http_code}' --max-time 20 $(node_url_args /livez)" || true)"
if [ "$gateway_code" != 200 ] && [ "$MODE" = domain ]; then
	warn "https://$DOMAIN does not answer yet. Point $DOMAIN's A record at $IP. Caddy keeps asking Let's Encrypt, and make gpu-logs shows it."
elif [ "$gateway_code" != 200 ]; then
	warn "the gateway on the node answered $gateway_code to /livez. Try: make gpu-logs"
fi

# ---- done -------------------------------------------------------------------

price="$(plan_price "$TYPE")"
cat >&2 <<EOF

$(printf '\033[1m')the node is up$(printf '\033[0m')  $IP  $MODE mode  $TYPE  ${price:-?} USD an hour

took: $(printf '%s, ' "${TIMES[@]}")$((SECONDS / 60)) min in all

next:
$(if [ "$MODE" = domain ]; then
	echo "  https://$DOMAIN is the gateway. No tunnel."
else
	echo "  make tunnel     in another terminal. Then localhost:${GATEWAY_PORT:-8443} is the node"
fi)
  make check      prove the key is enforced and the models hold their GPUs
  make smoke      make a card on the node and check every model step ran on a GPU
  make destroy    delete everything tagged $TAG. The node bills until you do

EOF
