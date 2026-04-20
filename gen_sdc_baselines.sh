#!/usr/bin/env bash
# gen_sdc_baselines.sh
# Generates SDC baseline benchmark files for each design at the SAME
# (pipeline_stages, clock_period_ps) constraints used in the agent experiments.
# Run from the alphaevolve-xls directory:
#   bash gen_sdc_baselines.sh
set -uo pipefail

XLS=/mnt/d/final/xls
IR_CONV=$(find "$XLS/bazel-out" -name "ir_converter_main" -type f 2>/dev/null | head -1)
IR_CONV=${IR_CONV:-$XLS/bazel-bin/xls/dslx/ir_convert/ir_converter_main}
OPT=$(find "$XLS/bazel-out" -name "opt_main" -type f 2>/dev/null | head -1)
OPT=${OPT:-$XLS/bazel-bin/xls/tools/opt_main}
BM=$(find "$XLS/bazel-out" -name "benchmark_main" -type f 2>/dev/null | grep "dev_tools/benchmark_main$" | head -1)
BM=${BM:-$XLS/bazel-bin/xls/dev_tools/benchmark_main}
STDLIB=$XLS/xls/dslx/stdlib
DESIGNS=$(pwd)/designs
TMP=$(mktemp -d)

trap 'rm -rf "$TMP"' EXIT

echo "Resolved tool paths:"
echo "  ir_converter_main : $IR_CONV"
echo "  opt_main          : $OPT"
echo "  benchmark_main    : $BM"
echo ""

# ── Verify all binaries are present ──────────────────────────────────────────
for tool_var in "ir_converter_main:$IR_CONV" "opt_main:$OPT" "benchmark_main:$BM"; do
    name=${tool_var%%:*}
    path=${tool_var#*:}
    if [ ! -f "$path" ]; then
        echo "ERROR: $name not found at: $path"
        echo "Run a slow-mode experiment first (--ppa_mode slow) to build benchmark_main,"
        echo "or build manually: cd $XLS && bazel build -c opt //xls/dev_tools:benchmark_main"
        exit 1
    fi
    echo "  ✓ $name built"
done

run_design() {
    local name=$1       # short name for messages
    local dslx=$2       # path to .x file
    local dslx_dir=$3   # --dslx_path (directory containing the .x)
    local stages=$4
    local clock=$5
    local out=$6        # output benchmark .txt path

    echo "── $name  (${stages} stages, ${clock}ps) ──"

    echo "  [1/3] ir_convert..."
    "$IR_CONV" "$dslx" \
        --dslx_stdlib_path="$STDLIB" \
        --dslx_path="$dslx_dir" \
        --top=main \
        > "$TMP/${name}.ir" 2>&1 \
        || { echo "  FAILED: ir_convert — see output:"; cat "$TMP/${name}.ir"; return 1; }

    echo "  [2/3] opt_main..."
    "$OPT" "$TMP/${name}.ir" \
        > "$TMP/${name}.opt.ir" 2>&1 \
        || { echo "  FAILED: opt_main"; return 1; }

    echo "  [3/3] benchmark_main (SDC, asap7)..."
    "$BM" "$TMP/${name}.opt.ir" \
        --delay_model=asap7 \
        --area_model=asap7 \
        --scheduling_strategy=sdc \
        --pipeline_stages="$stages" \
        --clock_period_ps="$clock" \
        --run_evaluators=false \
        --generator=pipeline \
        > "$out" 2>&1 \
        && echo "  ✓ written → $out" \
        || echo "  ✗ benchmark_main failed — check $out for details"
}

# ── Experiments ──────────────────────────────────────────────────────────────
#  exp006  sha256          8 stages  25000 ps
run_design sha256 \
    "$DESIGNS/sha256/sha256.x" \
    "$DESIGNS/sha256" \
    8 25000 \
    "$DESIGNS/sha256/sha_sdc_8stage_25kps.txt"

#  exp007  idct_chen       8 stages  50000 ps
run_design idct \
    "$DESIGNS/idct/idct_chen.x" \
    "$DESIGNS/idct" \
    8 50000 \
    "$DESIGNS/idct/idct_sdc_8stage_50kps.txt"

#  exp008  gemm4x4_int     4 stages   1000 ps
run_design gemm4x4 \
    "$DESIGNS/gemm4x4_int/gemm4x4_int.x" \
    "$DESIGNS/gemm4x4_int" \
    4 1000 \
    "$DESIGNS/gemm4x4_int/gemm_sdc_4stage_1kps.txt"

#  exp009  fusion_tile     4 stages   8000 ps
run_design fusion \
    "$DESIGNS/irregular_fusion/fusion_tile.x" \
    "$DESIGNS/irregular_fusion" \
    4 8000 \
    "$DESIGNS/irregular_fusion/fusion_sdc_4stage_8kps.txt"

#  exp010  bitonic_sort   64 stages  70000 ps
#  bitonic_sort_wrapper.x imports bitonic_sort.x from the same directory
run_design bitonic \
    "$DESIGNS/bitonic_sort/bitonic_sort_wrapper.x" \
    "$DESIGNS/bitonic_sort" \
    64 70000 \
    "$DESIGNS/bitonic_sort/bitonic_sdc_64stage_70kps.txt"

echo ""
echo "Done. Files written to designs/<design>/sdc_*.txt"
