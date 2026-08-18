#!/usr/bin/env bash
# Run sweep blocks so they survive the shell that started them.
#
# Backgrounding with `nohup ... &` from inside a tool call did not survive: several runs today were
# reported as in flight and produced nothing, because the process died with its parent. setsid
# detaches properly, a lock stops a second launch racing the first, and a manifest records what was
# asked for so a half-finished batch is visible rather than silent.
#
#   ./run-blocks.sh <out-dir> <seed-base> <block|@batch> [block|@batch...]
#   ./run-blocks.sh --status <out-dir>
#
# A @name argument expands to the blocks of that batch, so a themed group is launched by what it
# asks rather than by listing its members. `python3 -m sfpp.sweep --list` prints both.
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

# Expand any @batch argument into its blocks, leaving plain block names alone.
BLOCKS=()
for arg in "$@"; do
	case "$arg" in
	@*)
		members=$(python3 -c "
import sys
from sfpp.sweep import BATCHES
name = sys.argv[1]
if name not in BATCHES:
    sys.exit(f'unknown batch {name!r}; known: ' + ', '.join(sorted(BATCHES)))
print(' '.join(BATCHES[name]))
" "${arg#@}") || {
			echo "$members"
			exit 2
		}
		# shellcheck disable=SC2206
		BLOCKS+=($members)
		;;
	*) BLOCKS+=("$arg") ;;
	esac
done
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
#
# Both test modules, and pytest only when it is installed. The tests are unittest.TestCase either
# way, so unittest runs the same suite; pytest is preferred when present only because its output is
# shorter. A checkout without pytest must still be able to launch a block.
if python3 -c "import pytest" >/dev/null 2>&1; then
	GATE=(python3 -m pytest sfpp/test_mesh.py sfpp/test_knowledge.py -q)
else
	GATE=(python3 -m unittest sfpp.test_mesh sfpp.test_knowledge)
fi
if ! "${GATE[@]}" >"$OUT_ROOT/.tests.log" 2>&1; then
	echo "transport tests FAILED - see $OUT_ROOT/.tests.log; not running anything"
	exit 4
fi
echo "transport $PIN, tests pass ($(tail -1 "$OUT_ROOT/.tests.log"))"

# Blocks arrive as positional arguments, NOT as an exported array: bash silently accepts
# `BLOCKS=(a b) runner` as a command prefix but assigns the literal string "(a b)", so every
# launch queued one block named after the whole list and died on a KeyError in one second.
# Land each block as it finishes, rather than at the end.
#
# The runner is detached but the machine it runs on need not outlive it: a previous attempt left
# three runner.logs holding a "started" line and nothing else, twenty-two blocks lost, because the
# host went away mid-run. A block that is committed and pushed costs nothing if that happens again;
# the most that can be lost is the block in flight. Concurrent runners share a branch, so pull
# --rebase before pushing and retry rather than treating a race as failure.
persist() {
	local blk=$1 attempt
	[ -n "${RESULTS_REPO:-}" ] || return 0
	git -C "$RESULTS_REPO" add -A "$OUT_ROOT" >/dev/null 2>&1 || return 0
	git -C "$RESULTS_REPO" diff --cached --quiet && return 0
	git -C "$RESULTS_REPO" commit -q -m "runs: $blk on transport $PIN, seed base $SEED_BASE" || return 0
	for attempt in 1 2 3 4 5; do
		git -C "$RESULTS_REPO" pull --rebase -q origin "$RESULTS_BRANCH" >/dev/null 2>&1
		if git -C "$RESULTS_REPO" push -q origin "HEAD:$RESULTS_BRANCH" >/dev/null 2>&1; then
			echo "pushed $blk"
			return 0
		fi
		sleep $((attempt * 4))
	done
	echo "WARNING: $blk committed but not pushed after 5 attempts"
}

runner() {
	echo $$ >"$LOCK"
	{
		echo "started $(date -Is) · transport $PIN · seed base $SEED_BASE"
		echo "blocks: $*"
		for blk in "$@"; do
			if ls "$OUT_ROOT/$blk"*.json >/dev/null 2>&1; then
				echo "skip $blk (already present)"
				continue
			fi
			printf -- "--- %s %s ---\n" "$blk" "$(date -Is)"
			python3 -u -m sfpp.sweep --block "$blk" --seeds 3 --seed-base "$SEED_BASE" \
				--out "$OUT_ROOT" 2>&1
			printf -- "--- %s finished rc=%s %s ---\n" "$blk" "$?" "$(date -Is)"
			persist "$blk"
		done
		echo "all blocks attempted $(date -Is)"
		python3 -m sfpp.tuning --runs "$OUT_ROOT" --markdown \
			--out "$OUT_ROOT/tuning-metrics.md" 2>&1 | tail -5
	} >>"$OUT_ROOT/runner.log" 2>&1
	rm -f "$LOCK"
}

# RESULTS_REPO, if set, is a git checkout containing OUT_ROOT; each finished block is committed and
# pushed to RESULTS_BRANCH there. Unset means results stay on disk only.
RESULTS_REPO=${RESULTS_REPO:-}
RESULTS_BRANCH=${RESULTS_BRANCH:-$(git -C "${RESULTS_REPO:-.}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)}
if [ -n "$RESULTS_REPO" ]; then
	echo "results land in $RESULTS_REPO on $RESULTS_BRANCH, one commit per block"
fi

setsid bash -c "$(declare -f runner persist); export OUT_ROOT=${OUT_ROOT@Q} LOCK=${LOCK@Q} \
  PIN=${PIN@Q} SEED_BASE=${SEED_BASE@Q} RESULTS_REPO=${RESULTS_REPO@Q} \
  RESULTS_BRANCH=${RESULTS_BRANCH@Q}; runner ${BLOCKS[*]@Q}" </dev/null >/dev/null 2>&1 &
disown
sleep 1
echo "detached runner started; ${#BLOCKS[@]} blocks queued"
echo "  log:    $OUT_ROOT/runner.log"
echo "  status: ./run-blocks.sh --status $OUT_ROOT"
