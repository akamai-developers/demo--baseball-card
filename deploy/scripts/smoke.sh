#!/usr/bin/env bash
# Make one card on the node and check that it was made there.
#
#   make smoke                                  the test photo
#   make smoke PHOTO=me.jpg STATS=stats.json    your own
#
# Checks, in this order:
#   1. both PNGs came out at exactly 1500 x 2100
#   2. every model step, cutout, art and text, finished and reports ran_on
#      starting akamai-gpu
#   3. prints the ledger and the wall clock
#
# A card that renders from fallbacks is not a pass. The claim of this repo is
# that three open-weight models ran on Akamai Cloud GPUs, and step 2 is the
# only thing that proves it. Against the mock stack it fails, as it should.
set -euo pipefail

# shellcheck source-path=SCRIPTDIR source=lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

load_env
require_cmd curl jq uv
[ -n "${CARD_API_KEY:-}" ] || die "no CARD_API_KEY in .env. Run make deploy."

GATEWAY="$(node_gateway)"
PHOTO="${PHOTO:-$REPO_ROOT/tests/fixtures/portrait.jpg}"
STATS="${STATS:-$REPO_ROOT/examples/stats.json}"
OUT="${OUT:-$REPO_ROOT/out/smoke}"

# card/render.py:MODEL_STEPS, which decides the "Made on Akamai Cloud" line on
# the back of the card. tests/unit/test_deploy_scripts.py holds them equal.
MODEL_STEPS="cutout art text"

[ -f "$PHOTO" ] || die "no photo at $PHOTO"
[ -f "$STATS" ] || die "no stats at $STATS"

curl -sf --max-time 5 -o /dev/null "$GATEWAY/livez" \
	|| die "nothing answered $GATEWAY/livez. Run make tunnel in another terminal, or set DOMAIN in .env for domain mode."

mkdir -p "$OUT"
log "making a card from $(basename "$PHOTO") at $GATEWAY"

started="$SECONDS"
# cardctl prints the ledger itself and reads the key from the environment.
(cd "$REPO_ROOT" && uv run cardctl make --photo "$PHOTO" --stats "$STATS" --out "$OUT" --host "$GATEWAY") >&2
elapsed=$((SECONDS - started))

failures=0
pass() { printf '  \033[32mok\033[0m    %s\n' "$*" >&2; }
fail() {
	printf '  \033[31mFAIL\033[0m  %s\n' "$*" >&2
	failures=$((failures + 1))
}

# ---- 1. the two PNGs --------------------------------------------------------

log "the pictures"
for side in front back; do
	png="$OUT/card-$side.png"
	if [ ! -f "$png" ]; then
		fail "no $png"
		continue
	fi
	# The width and height straight out of the PNG's IHDR. Nothing to install.
	size="$(od -An -tu1 -j16 -N8 "$png" | awk '{ printf "%dx%d", ($1*16777216)+($2*65536)+($3*256)+$4, ($5*16777216)+($6*65536)+($7*256)+$8 }')"
	if [ "$size" = "1500x2100" ]; then
		pass "card-$side.png is $size"
	else
		fail "card-$side.png is $size, wanted 1500x2100"
	fi
done

# ---- 2. it ran on the GPUs --------------------------------------------------

log "where each model step ran"
# cardctl wrote the whole job to card.json beside the PNGs, so this reads the
# ledger the api built rather than the table meant for a person.
ledger="$OUT/card.json"
if [ ! -f "$ledger" ]; then
	fail "no $ledger, so where each step ran cannot be checked"
else
	# One field of a step's row, or the default when the row or the field is
	# missing.
	row() { jq -r --arg s "$1" --arg d "$3" "[.steps[] | select(.step == \$s)][0].$2 // \$d" "$ledger"; }
	for step in $MODEL_STEPS; do
		status="$(row "$step" status missing)"
		ran_on="$(row "$step" ran_on "")"
		if [ "$status" != "done" ]; then
			# A text step that fails its checks twice ran on the GPU, and its
			# words were not used. The reason says which.
			fail "the $step step is $status, not done, so a stand-in made that part of the card: $(row "$step" error "no reason given")"
		elif [ "${ran_on#akamai-gpu}" != "$ran_on" ]; then
			pass "$step ran on $ran_on"
		else
			fail "$step says it ran on ${ran_on:-nothing}. It has to start with akamai-gpu, or that model did not run on a card."
		fi
	done
	note "card $(jq -r '.card_id' "$ledger")"
fi

echo >&2
log "$elapsed seconds, both sides in $OUT"
[ "$failures" = 0 ] || die "$failures check(s) failed"
log "smoke test passed"
