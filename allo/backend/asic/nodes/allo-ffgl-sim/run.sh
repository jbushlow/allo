#!/usr/bin/env bash
set -euo pipefail

: "${testbench_name:=allo_generated_testbench}"
: "${waveform:=True}"
: "${ffgl_powerup_mode:=random}"
: "${ffgl_powerup_seed:=2}"

case "$ffgl_powerup_mode" in
  random|x|zero|one) ;;
  *)
    echo "unsupported FFGL power-up mode: $ffgl_powerup_mode" >&2
    echo "supported modes: random, x, zero, one" >&2
    exit 2
    ;;
esac
if ! [[ "$ffgl_powerup_seed" =~ ^[0-9]+$ ]]; then
  echo "FFGL power-up seed must be a nonnegative integer" >&2
  exit 2
fi
if [[ "$ffgl_powerup_mode" == "random" ]] \
  && (( 10#$ffgl_powerup_seed < 2 )); then
  echo "random FFGL power-up seed must be at least 2; VCS reserves 0 and 1" >&2
  exit 2
fi

mkdir -p outputs
rm -f outputs/run.vcd outputs/compile.log outputs/simulation.log \
      outputs/simulation-report.json

sources=()
macro_model_count=0
for file in inputs/adk/*.v inputs/adk/*.sv; do
  [[ -e "$file" ]] && sources+=("$file")
done
if [[ -d inputs/srams ]]; then
  while IFS= read -r -d '' file; do sources+=("$file"); done \
    < <(find -L inputs/srams -type f \( -name '*.v' -o -name '*.sv' \) -print0 | sort -z)
fi
if [[ -d inputs/macro-registry ]]; then
  while IFS= read -r -d '' file; do
    sources+=("$file")
    macro_model_count=$((macro_model_count + 1))
  done \
    < <(find -L inputs/macro-registry -type f \
      \( -name '*.v' -o -name '*.sv' \) \
      ! -name '*.lvs.v' ! -name '*.pg.v' -print0 | sort -z)
fi
sources+=(inputs/design.v)
# Some vendor Python launchers print environment warnings to stdout first.
backend=$(python3 -c \
  'import json, sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], sys.argv[3]))' \
  inputs/testbench-contract.json backend vitis | tail -n 1)
case "$backend" in
  vitis)
    sources+=(
      inputs/vitis-axi-memory-bfm.sv
      inputs/vitis-axilite-master-bfm.sv
    )
    ;;
  catapult|systemc) ;;
  *) echo "unsupported testbench backend: $backend" >&2; exit 2 ;;
esac
sources+=(inputs/testbench.sv)

compile_args=()
run_args=()
case "$ffgl_powerup_mode" in
  x)
    # VCS X-propagation is mutually exclusive with initreg.
    compile_args+=("-xprop=tmerge")
    ;;
  random|zero|one)
    initreg_value="$ffgl_powerup_mode"
    [[ "$initreg_value" == "zero" ]] && initreg_value=0
    [[ "$initreg_value" == "one" ]] && initreg_value=1
    # Scope initialization to the DUT so time-zero testbench state is untouched.
    printf 'tree %s.dut 0 %s\n' \
      "$testbench_name" "$initreg_value" > ffgl-initreg.config
    compile_args+=("+vcs+initreg+config+ffgl-initreg.config")
    run_args+=("+vcs+initreg+config+ffgl-initreg.config")
    if [[ "$ffgl_powerup_mode" == "random" ]]; then
      run_args+=("+vcs+initreg+$ffgl_powerup_seed")
    fi
    ;;
esac

vcs -full64 -sverilog "${compile_args[@]}" -override_timescale=1ns/1ps \
  -top "$testbench_name" +delay_mode_zero +define+TETRAMAX \
  -o simv "${sources[@]}" \
  2>&1 | tee outputs/compile.log

if [[ "$waveform" == "True" ]]; then
  run_args+=("+ALLO_DUMP_VCD")
fi
./simv "${run_args[@]}" 2>&1 | tee outputs/simulation.log

python3 report_sim.py --mode ffgl --compile-log outputs/compile.log \
  --simulation-log outputs/simulation.log --vcd outputs/run.vcd \
  --macro-model-count "$macro_model_count" \
  --powerup-mode "$ffgl_powerup_mode" \
  --powerup-seed "$ffgl_powerup_seed" \
  --output outputs/simulation-report.json
