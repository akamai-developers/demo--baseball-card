#!/usr/bin/env bash
# Delete everything tagged baseball-card, then ask the API whether anything with
# that tag is left.
#
#   make destroy           asks you to type the instance's label first
#   FORCE=1 make destroy   does not ask
#
# The instance bills by the hour whether or not anything talks to it. This is
# the script that stops the meter. It deletes only what terraform's state in
# deploy/terraform holds, which is what make deploy made, and the check after
# it looks for this repo's tag and no other.
set -euo pipefail

# shellcheck source-path=SCRIPTDIR source=lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_cmd terraform curl jq
require_cloud_deploy

if ! has_state; then
	log "no terraform state, so terraform has nothing to delete"
else
	tf init -input=false -no-color >/dev/null
	label="$(tf output -raw label 2>/dev/null || echo '<unknown>')"
	ip="$(tf output -raw ip_address 2>/dev/null || echo '<unknown>')"

	if [ "${FORCE:-0}" != 1 ]; then
		cat >&2 <<EOF

About to delete:
  instance  $label  $ip
  firewall  $label-fw
  its disk, with every card and photo on it

EOF
		printf 'Type the instance label to confirm: ' >&2
		read -r answer
		[ "$answer" = "$label" ] || die "that was not $label. Nothing was deleted."
	fi

	# Terraform evaluates the config on a destroy too, and two variables have no
	# default, so it needs the values deploy.sh wrote.
	[ -f "$TF_DIR/deploy.auto.tfvars" ] \
		|| die "there is terraform state but no deploy/terraform/deploy.auto.tfvars, which holds the values deploy.sh used and terraform needs. Delete the instance at cloud.linode.com instead."

	log "terraform destroy"
	tf destroy -input=false -auto-approve -no-color
fi

# ---- the read-only check ----------------------------------------------------
# Terraform says it deleted what its state knew about. This asks the account.
# A run interrupted between the apply and the state write leaves a real instance
# terraform has never heard of, and that is the case worth catching, because it
# bills by the hour. The API filters by tag, and each row is checked again for
# the exact tag, so a resource with another tag is never counted, let alone
# touched.
log "asking the API whether anything tagged $TAG is left"
leftovers=0
for path in /linode/instances /networking/firewalls; do
	body="$(linode_api "$path?page_size=500" -H "X-Filter: {\"tags\": \"$TAG\"}")" || {
		warn "could not read $path. Check by hand at cloud.linode.com."
		leftovers=$((leftovers + 1))
		continue
	}
	left="$(printf '%s' "$body" | jq -r --arg tag "$TAG" '.data[]? | select(.tags | index($tag)) | "      \(.id)  \(.label)"')"
	if [ -z "$left" ]; then
		note "$path: nothing tagged $TAG"
	else
		warn "$path: still tagged $TAG:"
		printf '%s\n' "$left" >&2
		leftovers=$((leftovers + $(printf '%s\n' "$left" | wc -l)))
	fi
done

[ "$leftovers" = 0 ] || die "$leftovers resource(s) tagged $TAG may still be there. An instance bills by the hour. Delete it at cloud.linode.com, or run make destroy again."
log "nothing tagged $TAG is left. The meter is stopped."
note "secrets/ssh/ and .env are untouched. The next make deploy reuses both."
