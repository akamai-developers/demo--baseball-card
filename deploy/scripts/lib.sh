#!/usr/bin/env bash
# Shared helpers for the deploy scripts. Sourced, never run.
#
# The rule every function here keeps: the API key, the Linode token and the
# Hugging Face token are never printed, never logged, and never an argument of
# a command, where `ps` would show them. They reach curl and ssh on stdin.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
TF_DIR="$REPO_ROOT/deploy/terraform"
MODELS_ENV="$REPO_ROOT/deploy/models.env"
LAYOUTS_DIR="$REPO_ROOT/deploy/layouts"

SSH_DIR="$REPO_ROOT/secrets/ssh"
SSH_KEY="$SSH_DIR/id_ed25519"
KNOWN_HOSTS="$SSH_DIR/known_hosts"

# Where the repo lands on the node, and the tag every resource carries. Both
# are fixed: deploy/terraform/main.tf and deploy/cloud-init/bootstrap.sh hold
# the same two, and tests/security/test_terraform.py holds them equal.
NODE_DIR=/opt/baseball-card
# destroy.sh reads it. shellcheck cannot see across a source.
# shellcheck disable=SC2034
TAG=baseball-card

# What cloud-init leaves on the node. bootstrap.sh writes "installed" before
# its reboot, and bootcmd turns that into "ready" on the boot after.
# shellcheck disable=SC2034
READY_MARKER=/var/lib/baseball-card/ready
# shellcheck disable=SC2034
BOOTSTRAP_LOG=/var/log/baseball-card-bootstrap.log

# Block 2 of .env.example: the defaults a person may change. The node's .env
# carries each one this laptop's .env sets, so the node runs with the same.
NODE_SETTINGS="RETENTION_HOURS SWEEP_EVERY_S STEP_TIMEOUT_S ART_TIMEOUT_S MAX_UPLOAD_MB MAX_PHOTO_PX ART_POOL_SIZE COMPS_COUNT RENDER_CONCURRENCY LOG_LEVEL"

# ---- output -----------------------------------------------------------------
# Everything goes to stderr, so a function can still return a value on stdout.

log() { printf '\033[1m==>\033[0m %s\n' "$*" >&2; }
note() { printf '    %s\n' "$*" >&2; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die() {
	printf '\033[31merror:\033[0m %s\n' "$*" >&2
	exit 1
}

require_cmd() {
	for cmd in "$@"; do
		command -v "$cmd" >/dev/null 2>&1 || die "$cmd is not installed, and this script needs it."
	done
}

# A value that goes into the node's .env or into terraform's variables. Both are
# read by more than one parser, compose, bash and HCL, so only characters all
# three read the same way are let through.
require_plain() {
	local name="$1" value="$2"
	[[ "$value" =~ ^[A-Za-z0-9._:/@+-]*$ ]] \
		|| die "$name has a character this script cannot pass on safely. Letters, digits and . _ : / @ + - only."
}

# ---- .env -------------------------------------------------------------------

# Read the repo's .env into the environment. A value already set in the
# environment wins, so GPU_COUNT=2 make deploy works for one run, and a
# LINODE_TOKEN from a shell profile is not clobbered.
load_env() {
	local file="$REPO_ROOT/.env"
	[ -f "$file" ] || return 0
	local line name value
	while IFS= read -r line || [ -n "$line" ]; do
		case "$line" in '' | '#'*) continue ;; esac
		[[ "$line" == *=* ]] || continue
		name="${line%%=*}"
		value="${line#*=}"
		# Only names that look like environment variables, so a stray line in
		# .env cannot run anything.
		[[ "$name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
		[ -n "${!name:-}" ] && continue
		export "$name=$value"
	done <"$file"
}

# One value from the repo's .env itself, never from the environment. For a
# setting a shell profile may hold for some other purpose.
env_file_value() {
	local name="$1" file="$REPO_ROOT/.env" line value=""
	[ -f "$file" ] || return 0
	while IFS= read -r line || [ -n "$line" ]; do
		case "$line" in "$name="*) value="${line#*=}" ;; esac
	done <"$file"
	printf '%s' "$value"
}

# Create .env from the example, as `make env` does, because deploy.sh is often
# the first thing a reader runs.
ensure_env_file() {
	local file="$REPO_ROOT/.env"
	if [ ! -f "$file" ]; then
		cp "$REPO_ROOT/.env.example" "$file"
		log "created .env from .env.example"
	fi
	chmod 600 "$file"
}

# Put NAME=value in the repo's .env, in place of the line for NAME, or at the
# end when there is none. The value reaches no other process: read and printf
# are bash builtins, and the temp file is mode 600 from the start.
set_env_var() {
	local name="$1" value="$2" file="$REPO_ROOT/.env" tmp line found=""
	# .env.tmp* is gitignored, so a run killed here leaves nothing to commit.
	tmp="$(umask 077 && mktemp "$REPO_ROOT/.env.tmp.XXXXXX")"
	if [ -f "$file" ]; then
		while IFS= read -r line || [ -n "$line" ]; do
			case "$line" in
			"$name="*)
				printf '%s=%s\n' "$name" "$value"
				found=1
				;;
			*) printf '%s\n' "$line" ;;
			esac
		done <"$file" >"$tmp"
	fi
	[ -n "$found" ] || printf '%s=%s\n' "$name" "$value" >>"$tmp"
	mv "$tmp" "$file"
	chmod 600 "$file"
}

# Reuse the key in .env when it is long enough, otherwise make one and write it
# there. Exports CARD_API_KEY, and sets KEY_STATE to "reused" or "generated" so
# the caller can say which without ever seeing the value. Not run in $(...):
# a key made in a subshell would never reach the caller.
# deploy.sh reads KEY_STATE.
# shellcheck disable=SC2034
KEY_STATE=""
# shellcheck disable=SC2034
ensure_api_key() {
	ensure_env_file
	load_env
	if [ "${#CARD_API_KEY}" -ge 32 ]; then
		KEY_STATE=reused
		return 0
	fi
	require_cmd openssl
	CARD_API_KEY="$(openssl rand -hex 32)"
	export CARD_API_KEY
	set_env_var CARD_API_KEY "$CARD_API_KEY"
	KEY_STATE=generated
}

# ---- the node's .env --------------------------------------------------------

node_line() {
	require_plain "$1" "$2"
	printf '%s=%s\n' "$1" "$2"
}

# The node's .env, on stdout. It holds the key, so deploy.sh keeps it in a
# variable and sends it over ssh's stdin, and it lands at mode 600. Reads
# CARD_API_KEY, DOMAIN, GPU_COUNT and block 2 from the environment, where
# load_env puts .env, HF_TOKEN from .env alone, the LLM_ pins from
# deploy/models.env, and the GPU layout from deploy/layouts/.
node_env() {
	local count="${GPU_COUNT:-1}" line name
	case "$count" in
	1 | 2) ;;
	*) die "GPU_COUNT is $count. It is 1 or 2: deploy/layouts/ has a file for one card and one for two." ;;
	esac
	[ "${#CARD_API_KEY}" -ge 32 ] || die "CARD_API_KEY is shorter than 32 characters. Run make env."
	# shellcheck source-path=SCRIPTDIR source=../models.env
	. "$MODELS_ENV"

	echo "# Written by make deploy from the laptop's .env. Mode 600, owned by the deploy user."
	echo "# make rotate-key replaces CARD_API_KEY, and the next make deploy writes it all again."
	node_line CARD_API_KEY "$CARD_API_KEY"
	# Compose reads these two itself, so every docker compose on the node runs
	# the stack as it was deployed, one typed after make ssh included: the gpu
	# profile, and in domain mode the file that opens 80 and 443.
	node_line COMPOSE_PROFILES gpu
	if [ -n "${DOMAIN:-}" ]; then
		node_line COMPOSE_FILE docker-compose.yml:docker-compose.domain.yml
	fi
	node_line DOMAIN "${DOMAIN:-}"
	node_line BACKEND real
	# The api's own steps, comps, compose and render, run on the node's CPU, and
	# the ledger says so. The model services carry their card's label instead.
	node_line GPU_LABEL akamai-cpu
	# vLLM is a stock image and says nothing about itself. The api sends
	# LLM_MODEL in every request, and vLLM answers 404 to a name it does not
	# serve. The licence goes on the back of the card.
	node_line LLM_MODEL "$LLM_MODEL"
	node_line LLM_REVISION "$LLM_REVISION"
	node_line LLM_LICENSE "$LLM_LICENSE"
	# From .env only. An HF_TOKEN a shell profile exports for other work stays on
	# this laptop. The pinned models need none.
	node_line HF_TOKEN "$(env_file_value HF_TOKEN)"
	echo "# The GPU layout, deploy/layouts/gpu$count.env: which card each model is on."
	while IFS= read -r line || [ -n "$line" ]; do
		case "$line" in '' | '#'*) continue ;; esac
		node_line "${line%%=*}" "${line#*=}"
	done <"$LAYOUTS_DIR/gpu$count.env"
	echo "# Block 2 of .env.example, as this laptop's .env has it."
	for name in $NODE_SETTINGS; do
		if [ -n "${!name:-}" ]; then
			node_line "$name" "${!name}"
		fi
	done
}

# ---- addresses --------------------------------------------------------------

# An IPv4 address as one number. 10# so an octet with a leading zero is read
# as decimal, not octal.
ipv4_int() {
	local a b c d
	IFS=. read -r a b c d <<<"$1"
	printf '%d' $(((10#$a << 24) | (10#$b << 16) | (10#$c << 8) | 10#$d))
}

# Is the IPv4 address inside the CIDR block? Plain bash, because this runs on
# a Mac too, where there is no ipcalc and no python to count on.
ip_in_cidr() {
	local ip="$1" cidr="$2" bits mask
	bits="${cidr#*/}"
	mask=$((bits == 0 ? 0 : (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF))
	[ $(($(ipv4_int "$ip") & mask)) -eq $(($(ipv4_int "${cidr%/*}") & mask)) ]
}

# ---- ssh --------------------------------------------------------------------

# A keypair that belongs to this repo and nothing else. Never one from ~/.ssh:
# this one has no passphrase so the scripts can run unattended, and that is only
# acceptable for a key that reaches one demo node.
ensure_ssh_key() {
	mkdir -p "$SSH_DIR"
	chmod 700 "$SSH_DIR"
	if [ ! -f "$SSH_KEY" ]; then
		require_cmd ssh-keygen
		log "making a deploy keypair at secrets/ssh/id_ed25519"
		ssh-keygen -t ed25519 -N '' -C "baseball-card" -f "$SSH_KEY" -q
	fi
	chmod 600 "$SSH_KEY"
}

# Fills SSH_OPTS. An array set by a function, not printed and read back with
# mapfile, because macOS ships bash 3.2 and mapfile arrived in bash 4.
# accept-new learns the node's host key on the first connection, into this
# repo's own known_hosts, and refuses a different key after that.
SSH_OPTS=()
set_ssh_opts() {
	SSH_OPTS=(
		-i "$SSH_KEY"
		-o IdentitiesOnly=yes
		-o UserKnownHostsFile="$KNOWN_HOSTS"
		-o StrictHostKeyChecking=accept-new
		-o ConnectTimeout=10
		-o LogLevel=ERROR
	)
}

# Run a command on the node. Anything on stdin is forwarded, which is how the
# node's .env is written without the key touching a command line.
ssh_node() {
	local ip user
	ip="$(node_ip)"
	user="$(tf_output deploy_user)"
	set_ssh_opts
	# The command expands on this side before ssh sends it, which is what every
	# caller wants: each builds the remote command as one deliberate string.
	# shellcheck disable=SC2029
	ssh "${SSH_OPTS[@]}" "$user@$ip" "$@"
}

# ---- terraform --------------------------------------------------------------

tf() { terraform -chdir="$TF_DIR" "$@"; }

tf_output() {
	local value
	value="$(tf output -raw "$1" 2>/dev/null)" || die "no terraform output named $1. Has make deploy run?"
	[ -n "$value" ] || die "terraform output $1 is empty."
	printf '%s' "$value"
}

# Cached, because three scripts call it in a loop.
NODE_IP=""
node_ip() {
	[ -n "$NODE_IP" ] || NODE_IP="$(tf_output ip_address)"
	printf '%s' "$NODE_IP"
}

has_state() {
	[ -f "$TF_DIR/terraform.tfstate" ] && [ -n "$(tf state list 2>/dev/null)" ]
}

# The instance in terraform's state as "id address", or nothing when there is
# none. Needs `terraform init` first.
node_in_state() {
	tf show -json -no-color 2>/dev/null | jq -r '
		.values.root_module.resources[]?
		| select(.address == "linode_instance.node")
		| "\(.values.id) \(.values.ipv4 | first)"' 2>/dev/null || true
}

# ---- the Linode API ---------------------------------------------------------

# Nothing in this repo creates, changes or deletes a cloud resource without
# this. The default is off, and it is off in .env.example.
require_cloud_deploy() {
	load_env
	[ "${ALLOW_CLOUD_DEPLOY:-0}" = "1" ] || die "ALLOW_CLOUD_DEPLOY is not 1 in .env. It is what stands between this script and a GPU node that bills by the hour. Set it on purpose."
	[ -n "${LINODE_TOKEN:-}" ] || die "LINODE_TOKEN is not set. Export it in your shell, not in .env. Terraform reads it from the environment."
}

# The API root. A test points it at a stand-in. Nothing else changes it.
LINODE_API_BASE="${LINODE_API_BASE:-https://api.linode.com/v4}"

# A read-only call: the token check before an apply, the price, and the count
# of what destroy left. Extra arguments go to curl. The token reaches curl on
# stdin as a config line, never in its arguments.
linode_api() {
	local path="$1"
	shift
	require_cmd curl
	printf 'header = "Authorization: Bearer %s"\n' "$LINODE_TOKEN" \
		| curl --config - -sf --max-time 30 "$@" "${LINODE_API_BASE}${path}"
}

# What a plan costs an hour, from the price list.
plan_price() {
	linode_api "/linode/types/$1" | jq -r '.price.hourly // empty' 2>/dev/null || true
}

# ---- the gateway --------------------------------------------------------------

# Where this laptop reaches the node. https://DOMAIN in domain mode. Otherwise
# the tunnel, on GATEWAY_PORT like the mock stack, so one of the two at a time
# is on that port, and cardctl and the app find either at the same address.
node_gateway() {
	if [ -n "${DOMAIN:-}" ]; then
		printf 'https://%s' "$DOMAIN"
	else
		printf 'http://localhost:%s' "${GATEWAY_PORT:-8443}"
	fi
}

# curl through the gateway with the key. The key goes in on stdin as a curl
# config line, so it is never in argv, never in the shell history, and never in
# a log line. printf is a bash builtin, so it does not fork either.
keyed_curl() {
	[ -n "${CARD_API_KEY:-}" ] || die "CARD_API_KEY is not set. Run make env, or make deploy."
	printf 'header = "Authorization: Bearer %s"\n' "$CARD_API_KEY" | curl --config - "$@"
}

# How the node reaches its own gateway: Caddy on its loopback 8443 in tunnel
# mode. In domain mode Caddy listens on 443 with a certificate for DOMAIN, so
# the request goes to that name, pinned to the loopback with --resolve rather
# than trusting whatever DNS says.
node_url_args() {
	local path="$1"
	if [ -n "${DOMAIN:-}" ]; then
		printf "%s" "--resolve $DOMAIN:443:127.0.0.1 'https://$DOMAIN$path'"
	else
		printf "%s" "'http://127.0.0.1:8443$path'"
	fi
}

# The status code the node's gateway gives one path with a given key. Used by
# rotate-key.sh to prove the old key is dead. The key travels on the remote
# curl's stdin, so it is in neither the ssh command line nor the node's process
# list.
node_status_with_key() {
	local key="$1" path="$2"
	printf 'header = "Authorization: Bearer %s"\n' "$key" \
		| ssh_node "curl --config - -s -o /dev/null -w '%{http_code}' --max-time 20 $(node_url_args "$path")"
}

# The api's /healthz, asked inside its own container on the node, so it answers
# before Caddy has a certificate in domain mode. The key is the container's own
# CARD_API_KEY, read by Python inside it. The script goes in on stdin.
node_healthz() {
	ssh_node "cd $NODE_DIR && docker compose exec -T api python -" <<'PY' 2>/dev/null || true
import os
import urllib.request

request = urllib.request.Request(
    "http://127.0.0.1:8080/healthz",
    headers={"Authorization": "Bearer " + os.environ["CARD_API_KEY"]},
)
try:
    print(urllib.request.urlopen(request, timeout=10).read().decode())
except Exception:
    print("{}")
PY
}
