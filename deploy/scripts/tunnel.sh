#!/usr/bin/env bash
# One ssh local forward to the node's gateway. Runs in the foreground until you
# stop it, because that is the honest way to show that closing it closes the
# only way in.
#
#   make tunnel
#
# While it runs, http://localhost:GATEWAY_PORT on this machine is the node's
# gateway, 8443 by default, the same address the mock stack uses. Caddy on the
# node binds 127.0.0.1 only, so the forward is the only route to it, and SSH is
# what encrypts the traffic.
set -euo pipefail

# shellcheck source-path=SCRIPTDIR source=lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

load_env
require_cmd terraform ssh
LOCAL_PORT="${GATEWAY_PORT:-8443}"

has_state || die "no terraform state, so there is no node. Run make deploy first."

if [ -n "${DOMAIN:-}" ]; then
	die "DOMAIN is set, so the node is in domain mode: https://$DOMAIN is the gateway, and nothing on the node listens where a tunnel would go."
fi

IP="$(node_ip)"
USER_NAME="$(tf_output deploy_user)"
set_ssh_opts

# Fail rather than forward nothing when the port is taken. Without ss, as on a
# Mac, ExitOnForwardFailure below says so instead.
if command -v ss >/dev/null 2>&1 && ss -ltn "sport = :$LOCAL_PORT" 2>/dev/null | grep -q ":$LOCAL_PORT"; then
	die "something is already listening on localhost:$LOCAL_PORT. If it is the mock stack, run make down. Or set GATEWAY_PORT in .env to a free port."
fi

log "forwarding localhost:$LOCAL_PORT to the node's gateway on $IP"
note "the gateway is http://localhost:$LOCAL_PORT while this runs"
note "ctrl-c closes it, and then nothing can reach the node but ssh"

# -N: no remote command, a forward and nothing else. ExitOnForwardFailure: if
# the forward cannot be set up, say so instead of sitting there connected.
exec ssh -N \
	-o ExitOnForwardFailure=yes \
	-o ServerAliveInterval=30 \
	"${SSH_OPTS[@]}" \
	-L "$LOCAL_PORT:127.0.0.1:8443" \
	"$USER_NAME@$IP"
