#!/usr/bin/env bash
# Whole remaining night in one function, so bash has parsed all of it before running any of it.
main() {
	set -uo pipefail
	cd "$(dirname "$0")"
	echo $$ >run_rest.pid
	export AB_DEADLINE="03:58"
	step() { echo "=== $* ($(date +%H:%M))"; }
	sweep() { # sweep KEY FILE: resumable; on a stuck radio, heal and carry on (3 tries)
		export AB_DONE="$2.done"
		for t in 1 2 3; do
			python abrun.py sweep "$1" "$2" && return 0
			echo "sweep $2 exit $?; healing"
			python abrun.py health
			python abrun.py provision "$1" --all-tx
		done
	}
	step "provision SF all-tx"
	python abrun.py provision SF --all-tx
	step "knobs8: flash"
	for n in dut peer witness lr w2; do
		python abrun.py flash ~/bench-firmware/ab/knobs8.uf2 $n || {
			sleep 20
			python abrun.py flash ~/bench-firmware/ab/knobs8.uf2 $n || echo "flash $n FAILED"
		}
	done
	step "health"
	python abrun.py health
	export AB_GLOBAL="nf=100"
	step "blast SF: contend blast_iq"
	python abrun.py blast SF blast_iq.json
	export AB_GLOBAL="nf=200"
	step "late SF: contend sweep_late16"
	python abrun.py late SF sweep_late16.json
	step "health"
	python abrun.py health
	step "provision SF all-tx"
	python abrun.py provision SF --all-tx
	step "night_r2 SF: contend night_r2"
	sweep SF night_r2.json
	for k in SS MF MS LM; do
		lk=$(echo $k | tr A-Z a-z)
		step "provision $k"
		python abrun.py provision $k --all-tx || {
			echo "provision $k failed"
			continue
		}
		step "night_$lk $k: contend night_$lk"
		sweep $k night_$lk.json
	done
	step "provision SF all-tx"
	python abrun.py provision SF --all-tx
	for sw in sweep_li sweep3_sp20 sweep_fiq sweep_burst night_r3 night_lp; do
		step "$sw SF: contend $sw"
		sweep SF $sw.json
	done
	step "ALL DONE"
}
main "$@"
