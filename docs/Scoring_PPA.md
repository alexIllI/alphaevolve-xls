# AlphaEvolve-XLS — Scoring & PPA Modes

> Part of the system architecture docs.
> See also: [Architecture.md](Architecture.md) · [Evolution_Loop.md](Evolution_Loop.md) · [Output_Decisions.md](Output_Decisions.md)

---

## Part 4 — XLS pipeline, command level

### Stage 1: DSLX → IR

```bash
ir_converter_main \
    --dslx_stdlib_path=<xls_src>/xls/dslx/stdlib \
    --top=<auto-detected> \
    design.x
```

`_detect_dslx_top()` scans the DSLX source for the last non-test `proc`/`fn` and sets it as the package top. `--dslx_path=<design_dir>` is also passed so local imports (e.g. `bitonic_sort_wrapper.x` importing `bitonic_sort`) resolve correctly.

### Stage 2: IR → optimized IR

```bash
opt_main design.ir
```

Dead-code elimination, constant folding, bit-width narrowing, CSE.

### Stage 3 (slow modes only): benchmark_main

```bash
benchmark_main design.opt.ir \
    --delay_model=unit \
    --area_model=asap7 \
    --scheduling_strategy=agent \
    --run_evaluators=false \
    --generator=pipeline \
    --clock_period_ps=<N>
```

Prints `Critical path delay`, `Total area`, `Total pipeline flops`, and a per-stage pipeline breakdown. A non-zero exit is tolerated when the `Pipeline:` section is present — proc-network designs may fail the register-reset lowering step after scheduling metrics have been printed.

In slow mode: if codegen fails but benchmark_main produced valid metrics, the result is still treated as `success` (with `verilog_path=None`).

### Stage 4: Codegen + block metrics

```bash
codegen_main design.opt.ir \
    --generator=pipeline \
    --delay_model=unit \
    --clock_period_ps=<N> \
    --scheduling_strategy=agent \
    --output_verilog_path=design.v \
    --output_schedule_path=design_schedule.textproto \
    --block_metrics_path=design_block_metrics.textproto
```

`design_block_metrics.textproto` contains flop count, delay breakdown, and stage count — the primary PPA source in `--ppa_mode fast`.

> **Proc designs and codegen:** `--generator=pipeline` does not support proc networks (`mac.x`, `matmul_4x4.x`). In `--ppa_mode fast`, proc designs always produce `run_failed`. Use `--ppa_mode slow` for proc designs.

---

## Part 5 — `--ppa_mode` matrix

| Mode | Per-iter build targets | Runs benchmark_main? | Runs codegen_main per iter? | Proc support? | Score terms | Status |
|------|----------------------|---------------------|----------------------------|--------------|-------------|--------|
| `fast` | agent_scheduler + codegen_main + opt_main + ir_converter | No | Yes (required) | **No** | stages, pipeline_reg_bits | Implemented |
| `medium` | same as fast (Yosys planned) | No | Yes | No | stages, reg_bits, gate_count (planned) | Placeholder |
| `slow` | agent_scheduler + benchmark_main only | Yes, each iter | **No** (built once at startup) | **Yes** | stages, reg_bits, area, delay, runtime_s | Implemented |
| `slowest` | same as slow (Yosys+OpenROAD planned) | Yes | **No** | No | stages, reg_bits, silicon area, WNS (planned) | Placeholder |

**Key difference in slow mode:** `codegen_main` is **not** in the per-iteration build targets. It was built once during the startup probe. `benchmark_main` alone provides all PPA metrics in slow mode, avoiding the codegen relink cost.

Placeholder modes (`medium`, `slowest`) emit a warning and evaluate as `fast`.

Implementation references:
- `xls_tools/build.py :: XLSBuilder.iteration_targets_for_mode(mode)`
- `xls_tools/pipeline.py :: XLSPipeline.run(..., ppa_mode=..., benchmark_timeout=...)`
- `alphaevolve/evaluator.py :: Evaluator.__init__(..., ppa_mode="fast")`
- `run.py :: parse_args()` — declares `--ppa_mode {fast,medium,slow,slowest}` and `--benchmark_timeout N` (default 1800 s)

---

## Part 6 — PPA extraction (ppa_metrics.py)

Source priority — first available wins per metric:

| Priority | Source | Metrics extracted |
|----------|--------|------------------|
| 1 | `benchmark_main` stdout | `critical_path_ps`, `total_area_um2`, `total_pipeline_flops`, `num_stages` |
| 2 | `block_metrics.textproto` | `flop_count`, `max_reg_to_reg_delay_ps`, `pipeline_stages`, `pipeline_reg_bits` |
| 3 | `schedule.textproto` | `length`, `min_clock_period_ps` |
| 4 | Verilog regex | approximate reg count (last resort) |

---

## Part 7 — Scoring

All metrics are converted to **unit-free ratios** before weighting. **Lower score is better.**

```
score = (num_stages          / REF_STAGES)    × stage_weight
      + (effective_flop_bits / REF_FLOP_BITS) × power_weight
      + (total_area_um²      / REF_AREA_UM2)  × area_weight
      + (max_stage_delay_ps  / REF_CLOCK_PS)  × delay_weight
      + balance_penalty                        × balance_weight
      + (scheduler_runtime_s / REF_TIMEOUT_S) × runtime_weight
```

Defaults (from `ppa_metrics.py`; overridable in `evolve_config.yaml`):

| Parameter | Default | Notes |
|-----------|---------|-------|
| `stage_weight` | 0.0 | Set to 0 when pipeline_stages is fixed — it's a constant offset |
| `power_weight` | 0.5 | Pipeline register bit proxy for register power |
| `area_weight` | 0.0 | Area rarely changes with scheduling; raise only in slow mode |
| `delay_weight` | 1.0 | Primary timing signal — max combinational delay across stages |
| `balance_weight` | 1.5 | Stage-load distribution penalty |
| `runtime_weight` | 0.5 | Scheduler wall-clock penalty; timeout → `runtime_s=3600` |
| `ref_stages` | 16 | Auto-set from `pipeline_stages` in YAML if fixed |
| `ref_flop_bits` | 10000 | Max expected pipeline register bits |
| `ref_area_um2` | 50000 | Max expected combinational area (um²) |
| `ref_clock_ps` | — | **Set from `--clock_period` each run. Must not be left at default.** |
| `ref_timeout_s` | 1800 | Set from `--benchmark_timeout` |

### Key metric: `max_stage_delay_ps`

The maximum combinational delay across all pipeline stages, extracted from `benchmark_main` per-stage output. This replaced `critical_path_ps` (total design critical path) because `critical_path_ps` was constant regardless of scheduling decisions; `max_stage_delay_ps` directly reflects how well the scheduler balanced stages.

### Key metric: `balance_penalty`

```
u_i             = stage_delay_i / REF_CLOCK_PS
spread          = population_std(u_i)
overload        = sqrt(mean(max(0, u_i − 1)²))
balance_penalty = spread + 2 × overload
```

- **0** = all stages evenly balanced and at or below the target clock
- Higher = skewed schedule and/or multiple stages over the clock period

### Multi-design aggregation

When multiple designs are evaluated per iteration:
- `total_stages`, `total_flops`, `total_area` — **summed** across designs
- `max_stage_delay`, `max_min_clock`, `max_runtime_s` — **maximum** across designs

`max_runtime_s` uses the worst scheduler runtime since the slowest design determines whether the iteration is practical.
