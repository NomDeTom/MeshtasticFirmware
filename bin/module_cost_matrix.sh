#!/usr/bin/env bash
# Sequentially build one PlatformIO env under a series of build-flag variants (module
# excludes, linker experiments, ...) and report each one's flash/RAM delta against a
# common baseline build. These are real linked binaries, not estimates.
#
# Usage:
#   bin/module_cost_matrix.sh <env> [config.tsv]
#
# config.tsv format: one variant per line, tab-separated:
#   <label>\t<extra PLATFORMIO_BUILD_FLAGS to append>
# Blank lines and lines starting with '#' are ignored. Defaults to
# bin/module_cost_matrix.default.tsv next to this script.
#
# Each variant is built in its own build dir (.pio/build_matrix/<label>/) via
# PLATFORMIO_BUILD_DIR, so variants never clobber each other or your normal
# .pio/build/<env> output, and can be re-run/resumed independently. PLATFORMIO_BUILD_FLAGS
# is appended (not substituted) after the env's own ini build_flags, confirmed via
# `pio project config --json-output`. A '-' extra-flags field builds with no changes at all
# (identical to baseline; useful as a repeatability/noise-floor check).
#
# CAVEAT (verified via `pio run -v`): PlatformIO/SCons buckets all -D flags before all -U
# flags in the FINAL compile command, regardless of the order you write them in a config
# line. "-UFOO -DFOO=0" therefore compiles as "define then undefine" - FOO ends up
# undefined, not 0, silently falling through to whatever a header's #ifndef default sets it
# to. Only add a -U here if you actually need to cancel a -D baked into the ini itself (those
# come before PLATFORMIO_BUILD_FLAGS and so are safely canceled by a later -U); to force a
# macro that a header only #ifndef-defaults, a bare -D with no paired -U is unambiguous.
#
# Requires the env to build cleanly already (pio pkg install done, toolchain present).
# Each variant is a full from-scratch compile (~8 min on nRF52 with LTO) - changing global
# build_flags invalidates every translation unit's SCons signature, so there is no
# meaningful object-file reuse to be had between variants regardless of build dir choice.
set -euo pipefail

ENV_NAME=${1:?"Usage: $0 <env> [config.tsv]"}
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CONFIG=${2:-"$SCRIPT_DIR/module_cost_matrix.default.tsv"}
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

PIO="$(command -v pio || command -v platformio || echo "$HOME/.platformio/penv/bin/pio")"
if ! command -v "$PIO" >/dev/null 2>&1 && [ ! -x "$PIO" ]; then
	echo "Error: 'pio'/'platformio' CLI not found on PATH or at $PIO." >&2
	exit 1
fi
# Some setups have no bare `python3` on PATH (this repo's WSL dev box among them) -
# PlatformIO always ships its own, so fall back to that.
PYTHON="$(command -v python3 || command -v python || echo "$HOME/.platformio/penv/bin/python")"
if ! command -v "$PYTHON" >/dev/null 2>&1 && [ ! -x "$PYTHON" ]; then
	echo "Error: no Python interpreter found (tried python3, python, $HOME/.platformio/penv/bin/python)." >&2
	exit 1
fi

if [ ! -f "$CONFIG" ]; then
	echo "Error: config file not found: $CONFIG" >&2
	exit 1
fi

MATRIX_ROOT=".pio/build_matrix"
mkdir -p "$MATRIX_ROOT"
RESULTS="$MATRIX_ROOT/${ENV_NAME}.results.tsv"
LOGDIR="$MATRIX_ROOT/${ENV_NAME}.logs"
mkdir -p "$LOGDIR"
echo -e "label\tflash_bytes\tram_bytes\tflash_delta\tram_delta\textra_flags" >"$RESULTS"

BASELINE_FLASH=""
BASELINE_RAM=""

extract() {
	# extract <manifest-dir> <key>
	"$PYTHON" - "$1" "$2" "$ENV_NAME" <<'PYEOF'
import json, os, sys
manifest_dir, key, env_name = sys.argv[1], sys.argv[2], sys.argv[3]
for fname in sorted(os.listdir(manifest_dir)):
    if not fname.endswith(".mt.json"):
        continue
    with open(os.path.join(manifest_dir, fname)) as f:
        data = json.load(f)
    if data.get("platformioTarget") == env_name and key in data:
        print(data[key])
        break
PYEOF
}

build_one() {
	label=$1
	extra_flags=$2
	builddir="$MATRIX_ROOT/$label"
	logfile="$LOGDIR/$label.log"

	echo "=== [$label] extra flags: '${extra_flags:--}' ===" >&2
	if [ "$extra_flags" = "-" ]; then
		extra_flags=""
	fi

	if PLATFORMIO_BUILD_DIR="$builddir" PLATFORMIO_BUILD_FLAGS="$extra_flags" \
		"$PIO" run -e "$ENV_NAME" -t mtjson >"$logfile" 2>&1; then
		: # fall through
	else
		echo "  BUILD FAILED - see $logfile" >&2
		tail -30 "$logfile" >&2
		echo -e "$label\tFAILED\tFAILED\t-\t-\t$extra_flags" >>"$RESULTS"
		return 0
	fi

	manifest_dir="$builddir/$ENV_NAME"
	flash=$(extract "$manifest_dir" flash_bytes)
	ram=$(extract "$manifest_dir" ram_bytes)
	if [ -z "$flash" ]; then
		echo "  WARNING: no flash_bytes in manifest for $label" >&2
		flash="n/a"
	fi
	if [ -z "$ram" ]; then
		ram="n/a"
	fi

	if [ "$label" = "baseline" ]; then
		BASELINE_FLASH=$flash
		BASELINE_RAM=$ram
	fi

	fd="-"
	rd="-"
	if [ "$flash" != "n/a" ] && [ -n "$BASELINE_FLASH" ] && [ "$BASELINE_FLASH" != "n/a" ]; then
		fd=$((flash - BASELINE_FLASH))
	fi
	if [ "$ram" != "n/a" ] && [ -n "$BASELINE_RAM" ] && [ "$BASELINE_RAM" != "n/a" ]; then
		rd=$((ram - BASELINE_RAM))
	fi

	echo -e "$label\t$flash\t$ram\t$fd\t$rd\t${extra_flags:--}" >>"$RESULTS"
	echo "  flash=$flash ram=$ram flash_delta=$fd ram_delta=$rd" >&2
}

build_one baseline "-"

while IFS=$'\t' read -r label flags || [ -n "$label" ]; do
	[ -z "$label" ] && continue
	case "$label" in \#*) continue ;; esac
	build_one "$label" "$flags"
done <"$CONFIG"

echo >&2
echo "=== Results ($RESULTS) ===" >&2
column -t -s$'\t' "$RESULTS"
