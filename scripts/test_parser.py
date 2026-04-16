#!/usr/bin/env python3
import subprocess, sys
sys.path.insert(0, '/mnt/d/final/alphaevolve-xls')
from xls_tools.pipeline import parse_benchmark_stdout

result = subprocess.run([
    '/mnt/d/final/xls/bazel-bin/xls/dev_tools/benchmark_main',
    '/tmp/matmul_test/matmul_opt.ir',
    '--delay_model=unit', '--area_model=asap7',
    '--scheduling_strategy=sdc', '--run_evaluators=false',
    '--generator=pipeline', '--clock_period_ps=1000'
], capture_output=True, text=True, timeout=60)

bm = parse_benchmark_stdout(result.stdout + result.stderr)
print("=== matmul_4x4 benchmark_main PPA ===")
print(f"critical_path_ps    : {bm.critical_path_ps}")
print(f"total_pipeline_flops: {bm.total_pipeline_flops}")
print(f"total_area_um2      : {bm.total_area_um2}")
print(f"total_delay_ps      : {bm.total_delay_ps}")
print(f"num_stages          : {bm.num_stages}")
print(f"min_stage_slack_ps  : {bm.min_stage_slack_ps}")
