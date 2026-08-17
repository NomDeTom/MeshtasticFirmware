#!/usr/bin/env bash
# Run sweep blocks so they survive the shell that started them.
#
# Backgrounding with `nohup ... &` from inside a tool call did not survive: several runs today were
# reported as in flight and produced nothing, because the process died with its parent. setsid
# detaches properly, a lock stops a second launch racing the first, and a manifest records what was
# asked for so a half-finished batch is visible rather than silent.
#
#   ./run-blocks.sh <out-dir> <seed-base> <block> [block...]
#   ./run-blocks.sh --status <out-dir>
set -uo pipefail
cd "$(dirname "$0")"

OUT_ROOT=${1:?usage: run-blocks.sh <out-dir> <seed-base> <block>...}

if [ "$OUT_ROOT" = "--status" ]; then
	DIR=${2:?need a directory}
	echo "== $DIR =="
	if [ -f "$DIR/.manifest" ]; then
		while read -r blk; do
			if ls "$DIR/$blk"*.json >/dev/null 2>&1; then
				echo "  done      $blk"
			else echo "  PENDING   $blk"; fi
		done <"$DIR/.manifest"
	else
		echo "  (no manifest)"
	fi
	if [ -f "$DIR/.lock" ] && kill -0 "$(cat "$DIR/.lock")" 2>/dev/null; then
		echo "  runner alive, pid $(cat "$DIR/.lock")"
	else
		echo "  no runner alive"
	fi
	exit 0
fi

SEED_BASE=${2:?need a seed base}
shift 2
BLOCKS=("$@")
[ ${#BLOCKS[@]} -gt 0 ] || {
	echo "no blocks given"
	exit 2
}

mkdir -p "$OUT_ROOT"
LOCK="$OUT_ROOT/.lock"
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
	echo "a runner is already alive here (pid $(cat "$LOCK")); refusing to race it"
	exit 3
fi

printf '%s\n' "${BLOCKS[@]}" >"$OUT_ROOT/.manifest"
PIN=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)

# The gate: a transport that fails its own tests does not get to produce results.
if ! python3 -m pytest sfpp/test_mesh.py -q >"$OUT_ROOT/.tests.log" 2>&1; then
	echo "transport tests FAILED - see $OUT_ROOT/.tests.log; not running anything"
	exit 4
fi
echo "transport $PIN, tests pass ($(tail -1 "$OUT_ROOT/.tests.log"))"

runner() {
	echo $$ >"$LOCK"
	{
		echo "started $(date -Is) · transport $PIN · seed base $SEED_BASE"
		for blk in "${BLOCKS[@]}"; do
			if ls "$OUT_ROOT/$blk"*.json >/dev/null 2>&1; then
				echo "skip $blk (already present)"
				continue
			fi
			echo "--- $blk $(date -Is) ---"
			python3 -m sfpp.sweep --block "$blk" --seeds 3 --seed-base "$SEED_BASE" \
				--out "$OUT_ROOT" 2>&1
			echo "--- $blk finished rc=$? $(date -Is) ---"
		done
		echo "all blocks attempted $(date -Is)"
		python3 -m sfpp.tuning --runs "$OUT_ROOT" --markdown \
			--out "$OUT_ROOT/tuning-metrics.md" 2>&1 | tail -5
	} >>"$OUT_ROOT/runner.log" 2>&1
	rm -f "$LOCK"
}

export -f runner
setsid bash -c "$(declare -f runner); OUT_ROOT='$OUT_ROOT' LOCK='$LOCK' PIN='$PIN' \
  SEED_BASE='$SEED_BASE' BLOCKS=(${BLOCKS[*]@Q}) runner" </dev/null >/dev/null 2>&1 &
disown
sleep 1
echo "detached runner started; ${#BLOCKS[@]} blocks queued"
echo "  log:    $OUT_ROOT/runner.log"
echo "  status: ./run-blocks.sh --status $OUT_ROOT"
