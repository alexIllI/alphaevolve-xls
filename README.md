# AlphaEvolve-XLS

> **AI-Driven Hardware Scheduling Algorithm Research**
> An AlphaEvolve-style evolutionary loop that evolves a standalone Google XLS pipeline scheduler for better PPA.

---

## What This Is

AlphaEvolve-XLS applies an AlphaEvolve-inspired evolutionary loop to automatically discover better pipeline scheduling algorithms inside [Google XLS](https://github.com/google/xls), an open-source High-Level Synthesis (HLS) toolchain.

In Google XLS, the scheduler sits between optimized IR and hardware generation. XLS first lowers DSLX into intermediate representation (IR), optimizes that IR, and then assigns each IR node to a pipeline cycle. That schedule decides where stage boundaries are inserted, how long values remain live across stages, and how many pipeline registers are needed. `codegen_main` then uses the schedule to emit pipelined Verilog and block metrics, while `benchmark_main` can estimate timing and area from the scheduled design. So the scheduler does not change what computation the IR performs, but it can strongly change latency, register pressure, timing balance, and sometimes final area.

The project evolves exactly one C++ function: `AgentGeneratedScheduler()` in `xls/scheduling/agent_generated_scheduler.cc`. This file is a standalone scheduler that XLS dispatches when it is invoked with `--scheduling_strategy=agent`. Every other XLS built-in scheduler (SDC, min-cut, ASAP, random) is left untouched — we do not mutate them.

Each iteration, the AI (Codex CLI or the OpenAI API) reads the current scheduler source, a bundle of reference XLS sources (SDC, min-cut, dispatch code) and provided knowledge, and emits a new C++ body that must respect the scheduler contract. The candidate is incrementally recompiled with Bazel, run against a set of DSLX benchmark designs, and scored on PPA (pipeline stages, register bits, area, critical-path delay). The best candidates become parents for the next generation. Details of the architecture are in [Overall Architecture](docs/architecture.md).

### Scheduler contract

The AI must preserve this function signature exactly. This implementation lives in `xls/scheduling/agent_generated_scheduler.cc` inside the Google XLS source tree:

```cpp
absl::StatusOr<ScheduleCycleMap> AgentGeneratedScheduler(
    FunctionBase* f,
    int64_t pipeline_stages,
    int64_t clock_period_ps,
    const DelayEstimator& delay_estimator,
    sched::ScheduleBounds* bounds,
    absl::Span<const SchedulingConstraint> constraints);
```

And it must:

1. Walk nodes via `TopoSort(f)` and skip anything `IsUntimed(node)`.
2. Read the feasibility window `[bounds->lb(node), bounds->ub(node)]`.
3. Pick a cycle inside that window using a principled heuristic.
4. Record the choice in a `ScheduleCycleMap` (`absl::flat_hash_map<Node*, int64_t>`).
5. Pin it via `bounds->TightenNodeLb/Ub(node, cycle)` and then `bounds->PropagateBounds()` after every assignment.

The helper functions already defined in the file (`NodeBitCount`, `NodeFanout`, `EstimateBoundaryRegisterCost`, `ScoreCandidateCycle`) are available to reuse.

---

## Directory Structure

```
alphaevolve-xls/
├── run.py                          Main CLI entry point
├── configs/
│   ├── ppa_constraints.yaml        Delay model, generator, per-design stage overrides
│   └── evolve_config.yaml          Islands, AI backend, score weights, ppa_mode
├── designs/
│   ├── mac/mac.x                   32-bit multiply-accumulate (proc design)
│   ├── fir_filter/fir.x            Simple FIR filter
│   ├── fir32/fir32.x               32-tap FIR filter
│   ├── dot_product/dot.x           8-element dot product
│   ├── gemm4x4_int/gemm4x4_int.x  4×4 integer matrix multiply
│   ├── idct/idct.x                 2D Inverse Discrete Cosine Transform
│   ├── sha256/sha256.x             SHA-256 hash (complex control + data)
│   ├── bitonic_sort/bitonic_sort.x Bitonic sort network
│   ├── crc32/crc32.x               CRC-32 checksum
│   └── matmul4x4/matmul_4x4.x     4×4 float32 FMA (proc design)
├── alphaevolve/
│   ├── sampler.py                  Codex CLI / OpenAI SDK interface
│   ├── evaluator.py                Splice C++ → Bazel build → XLS pipeline → PPA
│   ├── ppa_metrics.py              block_metrics (fast) + benchmark_main (slow) parsing
│   ├── database.py                 SQLite candidate store
│   ├── islands.py                  Island population manager, 4 mutation variants
│   └── prompts/                    system.txt + implement_agent_scheduler.txt (Jinja2)
├── xls_tools/
│   ├── build.py                    Incremental Bazel wrapper
│   └── pipeline.py                 DSLX → IR → opt → schedule → Verilog (+ optional benchmark)
├── knowledge/
│   ├── papers/                     Scheduling theory summaries injected into prompts
│   └── heuristics/                 ASAP/ALAP, list scheduling references
└── results/                        Per-run outputs (gitignored)
```

> **Proc vs function designs:** `mac.x` and `matmul_4x4.x` are XLS *proc* (stateful) designs. In `--ppa_mode fast`, `codegen_main --generator=pipeline` does not support proc networks. Run proc designs with `--ppa_mode slow` to get PPA via `benchmark_main`, which handles proc networks correctly.

---

## Prerequisites

Google XLS cloned and buildable from source. The project assumes `/mnt/d/final/xls` but any path works as long as `--xls_src` or `$XLS_SRC_PATH` points at it.

A Linux environment. WSL2 Ubuntu 22.04 is the tested setup.

Python 3.11 or newer, in a virtualenv.

Either an OpenAI API key (for `--backend openai`) or Codex CLI v0.120 installed (for `--backend codex`).

---

## Setup

```bash
# 1. Clone alongside XLS
cd /mnt/d/final
git clone https://github.com/alexIllI/alphaevolve-xls.git
cd alphaevolve-xls

# 2. Python env
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Env file (optional; needed for --backend openai)
cp .env.example .env
# edit .env and set OPENAI_API_KEY=...
```

---

## Build XLS from Source (one-time)

The evolution loop needs to be able to recompile the scheduler on every iteration, which means XLS must be built from source first. Plan on a 2–6 hour first build; after that every iteration is a small incremental rebuild.

**Minimum build (works with `--ppa_mode fast` on function-type designs):**

```bash
cd /mnt/d/final/xls
bazel build -c opt \
  //xls/scheduling:agent_generated_scheduler \
  //xls/tools:codegen_main \
  //xls/tools:opt_main \
  //xls/dslx/ir_convert:ir_converter_main
```

**Add `benchmark_main` for `--ppa_mode slow` or proc designs (mac, matmul):**

```bash
bazel build -c opt //xls/dev_tools:benchmark_main
```

> `benchmark_main` pulls in LLVM/JIT and is the largest target. With `--ppa_mode fast` on function-type designs (fir32, dot_product, gemm, idct, sha256, bitonic_sort, crc32), it is not needed. For proc-type designs or when you need ASAP7 area estimates, build it once and use `--ppa_mode slow`.

---

## Quick Start — Dry Run

Run this first to confirm the pipeline is wired up end-to-end. No AI calls, no mutations — it runs XLS with a minimal ASAP stub scheduler and reports the resulting PPA.

```bash
source .venv/bin/activate

python run.py \
  --input_file designs/dot_product/dot.x \
  --clock_period 1000 \
  --dry_run \
  --xls_src /mnt/d/final/xls
```

Expected output: a table row with stage count, register bits, and score for the design, followed by `Dry run complete.`

If the pre-built binaries do not yet support `--scheduling_strategy=agent` (they were built before this was added), the dry run will trigger an incremental Bazel rebuild automatically.

---

## Run the Evolution

The default mode is `--ppa_mode fast` (codegen block-metrics only, ~30–90 s per iteration). Start small and expand once you are satisfied the loop is behaving.

**Single-island, function-type design (recommended starting point):**

```bash
python run.py \
  --input_file designs/gemm4x4_int/gemm4x4_int.x \
  --clock_period 1000 \
  --iterations 20 \
  --num_islands 1 \
  --ppa_mode fast \
  --output_dir results/exp_001 \
  --xls_src /mnt/d/final/xls \
  --backend codex
```

**Multi-island, more diversity:**

```bash
python run.py \
  --input_file designs/sha256/sha256.x \
  --clock_period 3000 \
  --iterations 40 \
  --ppa_mode fast \
  --output_dir results/sha256_islands_001 \
  --xls_src /mnt/d/final/xls \
  --backend codex
```

**Proc-type design (requires `--ppa_mode slow`):**

```bash
python run.py \
  --input_file designs/mac/mac.x \
  --clock_period 1000 \
  --iterations 20 \
  --ppa_mode slow \
  --output_dir results/mac_slow_001 \
  --xls_src /mnt/d/final/xls \
  --backend codex
```

> `--extra_designs` lets you evaluate multiple DSLX designs in a single run (all sharing the same `--clock_period`). Only combine designs whose critical paths are compatible with the same timing budget. Run designs with different timing requirements in separate invocations.

**With ASAP7 area metrics (`--ppa_mode slow`):**

```bash
python run.py \
  --input_file designs/idct/idct.x \
  --clock_period 1500 \
  --iterations 10 \
  --ppa_mode slow \
  --output_dir results/idct_asap7_001 \
  --xls_src /mnt/d/final/xls \
  --backend codex
```

> `--ppa_mode slow` builds `benchmark_main` once at startup, then runs it on every iteration for ASAP7 timing and area estimates. Per-iteration cost is higher (~5–10 min) but you get true area numbers.

**Direct OpenAI API instead of Codex CLI:**

```bash
python run.py \
  --input_file designs/gemm4x4_int/gemm4x4_int.x \
  --clock_period 1000 \
  --iterations 20 \
  --backend openai \
  --model gpt-4o \
  --xls_src /mnt/d/final/xls
```

---

## CLI Reference

```
python run.py [OPTIONS]

Required:
  --input_file PATH         Primary DSLX design file (.x). Additional designs can be
                            added via --extra_designs (same clock period only).
  --clock_period N          Target clock period in picoseconds. Fixed for the entire
                            run — never modified by the evolution or AI.
                            Typical values by design type:
                              ~1000 ps  simple arithmetic (dot_product, crc32, fir32)
                              ~1500 ps  moderate datapaths (idct)
                              ~2000 ps  wider multipliers, float32 FMA (matmul_4x4)
                              ~3000 ps  very complex designs at looser timing (sha256)

Evolution control:
  --iterations N            Number of evolution iterations (default: 10).
  --mutation_target NAME    Only 'agent_scheduler' is supported (default).
                            Kept as a flag for forward compatibility.
  --num_islands N           Island populations (default: from evolve_config.yaml = 4).
                            Use --num_islands 1 for single linear evolution (simplest).
  --island_id N             Pin ALL iterations to island N (0-indexed). Useful for
                            debugging one mutation variant in isolation.

PPA evaluation depth:
  --ppa_mode MODE           fast    (default) codegen_main + block_metrics only.
                                    Does NOT invoke benchmark_main. Fast iteration.
                                    Note: proc-type designs (mac.x, matmul_4x4.x)
                                    are not supported in fast mode — use slow.
                            medium  Placeholder for Yosys synth+stat (not yet wired).
                            slow    Runs benchmark_main with asap7 area model.
                                    Required for proc-type designs. benchmark_main
                                    is built once at startup, then run each iteration.
                            slowest Placeholder for Yosys+OpenROAD (not yet wired).
                            Overrides evolve_config.yaml :: ppa_mode.

AI backend:
  --backend NAME            openai | codex (default: from evolve_config.yaml).
  --model NAME              AI model name (default: from evolve_config.yaml).

Paths:
  --extra_designs PATHS     Additional DSLX designs evaluated together with --input_file.
                            All designs share the same --clock_period. Only use this
                            when designs have similar timing requirements. Run designs
                            with different clock budgets in separate invocations.
  --ppa_constraints PATH    PPA constraints YAML (default: configs/ppa_constraints.yaml).
  --evolve_config PATH      Evolve config YAML  (default: configs/evolve_config.yaml).
  --output_dir PATH         Results directory   (default: results/<timestamp>).
  --xls_src PATH            XLS source clone    (default: $XLS_SRC_PATH or /mnt/d/final/xls).
  --xls_prebuilt PATH       Optional pre-built binary directory fallback.

Other:
  --dry_run                 Validate the full pipeline without calling the AI.
                            Uses the ASAP stub scheduler inside agent_generated_scheduler.cc.
                            Triggers an incremental Bazel rebuild if the binaries
                            do not yet support --scheduling_strategy=agent.
  --log_level LEVEL         DEBUG | INFO | WARNING (default: INFO).
```

---

## `--ppa_mode` — How it changes the loop

| Mode      | PPA source                              | Needs `benchmark_main`? | Per-iter cost   | Works with proc designs? | Status                   |
| --------- | --------------------------------------- | ----------------------- | --------------- | ------------------------ | ------------------------ |
| `fast`    | `codegen_main --block_metrics_path`     | No                      | ~30–90 s        | **No**                   | Implemented (default)    |
| `medium`  | `yosys -p "synth; stat"` on the Verilog | No                      | ~15–60 s extra  | No                       | Placeholder (falls back) |
| `slow`    | `benchmark_main` with `asap7` area      | Yes — built at startup  | ~5–10 min extra | **Yes**                  | Implemented              |
| `slowest` | Yosys `synth_asap7` + OpenROAD PnR      | No (Yosys/OpenROAD)     | ~5–15 min extra | No                       | Placeholder (falls back) |

Placeholder modes emit a warning and evaluate as if the mode were `fast`.

Score weights (`stage_weight`, `reg_weight`, `area_weight`, `delay_weight`) live in `configs/evolve_config.yaml`. Lower scores are better. In `fast` mode the area term is zero (codegen does not emit absolute silicon area); scoring is dominated by stages and register bits.

---

## How an Iteration Works

```
Startup:
  0a. Confirm agent_generated_scheduler / codegen_main / opt_main / ir_converter_main
      are built (probe: run binary with --scheduling_strategy=agent).
      If not supported → incremental Bazel rebuild of those targets only.
  0b. If --ppa_mode slow/slowest: build benchmark_main once at startup.
  0c. If a design folder contains <stem>_benchmark.txt (from a prior benchmark_main run),
      load it as optional baseline context for the AI prompt. This is purely informational
      — the AI uses it to understand what PPA to aim for. Missing files are silently skipped.

Per iteration:
  1. Select island (round-robin; pinned via --island_id / --num_islands 1).
  2. Select parent  (tournament within the island, or global best if island is empty,
                     or None on cold start — first iteration).
  3. Sample — sampler.py renders implement_agent_scheduler.txt and calls the AI.
              The prompt always contains: current scheduler source, reference XLS sources,
              parent score, best score so far, mutation-variant instruction, and optional
              baseline benchmark context.
  --- Compile-retry loop (default max_build_retries=3) ---
  4. Splice the generated C++ into xls/scheduling/agent_generated_scheduler.cc.
  5. Incremental Bazel build of the agent_scheduler target + its reverse deps.
     Compile failure: send the exact clang errors back to the AI and retry.
  6. Run the XLS pipeline on every benchmark design:
       DSLX → IR → opt → codegen  (+ benchmark_main if ppa_mode=slow).
  7. Extract PPA (block_metrics in fast, benchmark_main stdout in slow).
  8. Score = stages*stage_weight + flop_bits*reg_weight + area*area_weight + delay*delay_weight.
  9. Insert candidate in SQLite; update island.
 10. If score improved, write results/<run>/best_algorithm.patch.
 11. Restore the original agent_generated_scheduler.cc.
 12. Every migration_interval iterations: copy global best into all islands.
```

The AI always sees four rotating instruction "variants" per island: register-pressure-aware, ASAP-with-tie-break, mobility-driven, and lookahead. All four reference only real APIs (`TopoSort`, `IsUntimed`, `bounds->lb/ub`, `TightenNodeLb/Ub`, `PropagateBounds`, `delay_estimator.GetOperationDelayInPs`, `node->operands()`, `node->users()`, `GetFlatBitCount`).

---

## Providing Baseline Benchmark Context

For each design you run, you can place a file named `<stem>_benchmark.txt` in the design folder. If present, its contents are injected into the AI prompt as a reference target. This is useful to tell the AI what PPA the standard XLS scheduler achieves so it knows what to beat.

To generate the baseline file for a design, run `benchmark_main` manually once:

```bash
# Example: generate baseline for dot_product at 1000ps
<xls-bazel-bin>/xls/dev_tools/benchmark_main \
    /tmp/dot_product_opt.ir \
    --delay_model=unit \
    --area_model=asap7 \
    --scheduling_strategy=sdc \
    --clock_period_ps=1000 \
    --run_evaluators=false \
    > designs/dot_product/dot_product_benchmark.txt
```

Then on the next evolution run the file is picked up automatically.

---

## Choosing Designs for PPA Experiments

Different designs give the scheduler more or less room to make different decisions. For an area vs. delay plot with meaningful spread across iterations:

| Design | Recommended clock | Expected variation | Notes |
|--------|------------------|--------------------|-------|
| `fir32.x` | 1000 ps | Low | Very regular balanced tree; ASAP ≈ ALAP for all nodes. Use as a sanity baseline. |
| `gemm4x4_int.x` | 1000 ps | Moderate | 16 independent reduction chains — scheduler can spread or pack 64 multiplies differently. |
| `idct.x` | 1500 ps | Good | Mixed mul/shift/add + two-pass row/col structure; rich PPA trade-off space. |
| `sha256.x` | 3000 ps | Best | 64-round feedback structure, irregular dependency graph, many heuristic choices visible in area and delay simultaneously. |
| `bitonic_sort.x` | 1000 ps | Good | Fixed comparison network — register pressure vs stage count trade-offs are clear. |
| `mac.x` | 1000 ps | Moderate | Proc design — requires `--ppa_mode slow`. |

---

## Adding New Designs

Drop a `designs/<name>/<name>.x` file containing a DSLX `fn` or `proc` and pass it via `--input_file` or `--extra_designs`. If the design imports XLS standard-library modules (e.g. `float32`), the stdlib path is resolved automatically from `--xls_src`.

Designs with deeper data paths (wider multipliers, FMAs, convolutions) give the scheduler more room to make interesting decisions. Designs with **irregular dependency graphs** (SHA-256, IDCT, sorting networks) show more variation across schedulers than fully regular designs (balanced adder trees, FIR).

### Per-design clock constraints

The clock period is set once via `--clock_period` and applies to every design in the run. If your designs have different timing requirements, run them in separate invocations:

```bash
# Moderate arithmetic designs — 1 ns clock
python run.py --input_file designs/gemm4x4_int/gemm4x4_int.x --clock_period 1000 ...

# Designs with tighter paths at a looser clock
python run.py --input_file designs/sha256/sha256.x --clock_period 3000 ...

# Float32 FMA proc design (slow mode required)
python run.py --input_file designs/matmul4x4/matmul_4x4.x --clock_period 2000 --ppa_mode slow ...
```

Other per-design constraints (fixed `pipeline_stages`, alternate `generator`) can still be set in `configs/ppa_constraints.yaml` under the `per_design` block using the DSLX file stem as the key.

---

## Results Layout

Every run creates a directory under `--output_dir` (or `results/<timestamp>/` by default) containing:

| File                   | Contents                                                          |
| ---------------------- | ----------------------------------------------------------------- |
| `best_algorithm.patch` | Unified diff of `agent_generated_scheduler.cc` for the best score |
| `ppa_report.json`      | Final PPA summary + top 3 candidates                              |
| `evolution_log.csv`    | One row per candidate (score, status, timings, diff sizes)        |
| `candidates_db.sqlite` | Full candidate database (code, diffs, all metrics)                |
| `eval_runs/iterXXXX_*` | Per-design IR, schedule, block_metrics, Verilog, benchmark output |

Apply the best result permanently:

```bash
cd /mnt/d/final/xls
patch -p1 < ../alphaevolve-xls/results/exp_001/best_algorithm.patch
```

---

## Reproducibility

All CLI arguments, both YAML configs, and the resolved `ppa_mode` are written to the `meta` table of `candidates_db.sqlite`. The XLS scheduler source is always restored to the baseline after each iteration, so a run never leaves the XLS tree in a mutated state. Every candidate's `source_diff` is stored verbatim, so any historical candidate can be reproduced by applying its diff to the baseline and rebuilding.
