#!/usr/bin/env bash
set -e

STDLIB=/mnt/d/final/xls/xls/dslx/stdlib
IR_CONV=/mnt/d/final/xls/bazel-bin/xls/dslx/ir_convert/ir_converter_main
OPT=/mnt/d/final/xls/bazel-bin/xls/tools/opt_main
BM=/mnt/d/final/xls/bazel-bin/xls/dev_tools/benchmark_main
DESIGN=/mnt/d/final/alphaevolve-xls/designs/matmul4x4/matmul_4x4.x
OUT=/tmp/matmul_test
mkdir -p "$OUT"

echo '=== Stage 1: DSLX -> IR (top=matmul_4x4) ==='
"$IR_CONV" --dslx_stdlib_path="$STDLIB" \
  --top=matmul_4x4 \
  "$DESIGN" > "$OUT/matmul.ir"
echo "IR lines: $(wc -l < "$OUT/matmul.ir")"

# Show what ir_converter sets as the package top
echo "Package top: $(grep '^top ' "$OUT/matmul.ir" || grep 'top_[a-z]' "$OUT/matmul.ir" | head -1 || echo 'not found')"

echo '=== Stage 2: Optimize (auto-detects top from package) ==='
"$OPT" "$OUT/matmul.ir" > "$OUT/matmul_opt.ir"
echo "Opt IR lines: $(wc -l < "$OUT/matmul_opt.ir")"

echo ''
echo '=== Stage 3: benchmark_main PPA (auto-detects top) ==='
"$BM" "$OUT/matmul_opt.ir" \
  --delay_model=unit \
  --area_model=asap7 \
  --scheduling_strategy=sdc \
  --run_evaluators=false \
  --generator=pipeline \
  --clock_period_ps=1000 \
  2>&1 | grep -E '(Critical path delay:|Total delay:|Total area:|Total pipeline flops:|Min clock period|Min stage slack|Function:|Pipeline:|^\s+nodes:|Stage|Scheduling time|Lines of Verilog)'
