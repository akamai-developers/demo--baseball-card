#!/usr/bin/env bash
# A shell on the node, or its logs, with this repo's key and pinned host key.
#
#   make ssh        a shell, in the deploy user's home. The repo is /opt/baseball-card
#   make gpu-logs   the containers' logs, or the driver install while it runs
set -euo pipefail

# shellcheck source-path=SCRIPTDIR source=lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

load_env
require_cmd terraform ssh
has_state || die "no terraform state, so there is no node. Run make deploy first."
set_ssh_opts
target="$(tf_output deploy_user)@$(node_ip)"

case "${1:-}" in
"")
	exec ssh "${SSH_OPTS[@]}" "$target"
	;;
logs)
	# -t so ctrl-c reaches the remote side and stops the follow there too.
	# shellcheck disable=SC2029
	exec ssh -t "${SSH_OPTS[@]}" "$target" "
		if [ -f $READY_MARKER ]; then
			cd $NODE_DIR && docker compose logs -f --tail=100
		else
			echo 'the driver is still installing. Following the bootstrap log:'
			sudo tail -f $BOOTSTRAP_LOG
		fi"
	;;
*)
	die "usage: ssh.sh [logs]"
	;;
esac
