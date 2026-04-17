# AlphaEvolve-XLS

> **AI-Driven Hardware Scheduling Algorithm Research**
> An AlphaEvolve-style evolutionary loop that evolves a standalone Google XLS pipeline scheduler for better PPA.

---

## What This Is

AlphaEvolve-XLS applies an AlphaEvolve-inspired evolutionary loop to automatically discover better pipeline scheduling algorithms inside [Google XLS](https://github.com/google/xls), an open-source High-Level Synthesis (HLS) toolchain.

In Google XLS, the scheduler sits between optimized IR and hardware generation. XLS first lowers DSLX into intermediate result (IR), optimizes that IR, and then assigns each IR node to a pipeline cycle. That schedule decides where stage boundaries are inserted, how long values remain live across stages, and how many pipeline registers are needed. `codegen_main` then uses the schedule to emit pipelined Verilog and block metrics, while `benchmark_main` can estimate timing and area from the scheduled design. So the scheduler does not change what computation the IR performs, but it can strongly change latency, register pressure, timing balance, and sometimes final area.

The project evolves exactly one C++ function: `AgentGeneratedScheduler()` in `xls/scheduling/agent_generated_scheduler.cc`. This file is a standalone scheduler that XLS dispatches when it is invoked with `--scheduling_strategy=agent`. Every other XLS built-in scheduler (SDC, min-cut, ASAP, random) is left untouched — we do not mutate them.

Each iteration, the AI (Codex CLI or the OpenAI API) reads the current scheduler source, a bundle of reference XLS sources (SDC, min-cut, dispatch code) and provided knowledge, and emits a new C++ body that must respect the scheduler contract. The candidate is incrementally recompiled with Bazel, run against a set of DSLX benchmark designs, and scored on PPA (pipeline stages, register bits, area, critical-path delay). The best candidates become parents for the next generation. Details of architecture are in [Overall Architecture](docs/architecture.md).

### Scheduler contract

The AI must preserve this signature exactly. This implementation lives in `xls/scheduling/agent_generated_scheduler.cc` inside the Google XLS source tree:

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
├── run.py                      Main CLI entry point
├── configs/
│   ├── ppa_constraints.yaml    Delay model, generator, per-design stage overrides
│   └── evolve_config.yaml      Islands, AI backend, score weights, ppa_mode
├── designs/
│   ├── mac/mac.x               32-bit multiply-accumulate
│   ├── fir_filter/fir.x        8-tap FIR filter
│   ├── dot_product/dot.x       8-element dot product
│   └── matmul4x4/matmul_4x4.x  4×4 systolic array (float32 FMA)
├── alphaevolve/
│   ├── sampler.py              Codex CLI / OpenAI SDK interface
│   ├── evaluator.py            Splice C++ → Bazel build → XLS pipeline → PPA
│   ├── ppa_metrics.py          block_metrics (fast) + benchmark_main (slow) parsing
│   ├── database.py             SQLite candidate store
│   ├── islands.py              Island population manager, 4 mutation variants
│   └── prompts/                system.txt + implement_agent_scheduler.txt (Jinja2)
├── xls_tools/
│   ├── build.py                Incremental Bazel wrapper (agent + codegen + opt + irc)
│   └── pipeline.py             DSLX → IR → opt → schedule → Verilog (+ optional benchmark)
├── knowledge/
│   ├── papers/                 Scheduling theory summaries injected into prompts
│   └── heuristics/             ASAP/ALAP, list scheduling references
└── results/                    Per-run outputs (gitignored)
```

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

**Minimum build (works with `--ppa_mode fast`):**

```bash
cd /mnt/d/final/xls
bazel build -c opt \
  //xls/scheduling:agent_generated_scheduler \
  //xls/tools:codegen_main \
  //xls/tools:opt_main \
  //xls/dslx/ir_convert:ir_converter_main
```

**Add `benchmark_main` if you want to use `--ppa_mode slow`:**

```bash
bazel build -c opt //xls/dev_tools:benchmark_main
```

> `benchmark_main` pulls in LLVM/JIT and is the largest target. With `--ppa_mode fast` it is not built or invoked at all — PPA comes from `codegen_main`'s `block_metrics` textproto, which is fast enough to iterate on.

---

## Quick Start — Dry Run

Run this first to confirm the pipeline is wired up end-to-end. No AI calls, no mutations — it just runs XLS with the baseline scheduler.

```bash
source .venv/bin/activate

python run.py \
  --input_file designs/mac/mac.x \
  --clock_period 1000 \
  --dry_run \
  --xls_src /mnt/d/final/xls
```

Expected output has a line for the design with stage count, critical-path picosecond estimate, area (if `benchmark_main` is available), and score. The last line reads `Dry run complete.`.

---

## Run the Evolution

The default mode is `--ppa_mode fast` (codegen block-metrics only, ~30–90 s per iteration). Start small and expand once you are satisfied the loop is behaving.

**Linear, single-island (recommended while getting started):**

```bash
python run.py \
  --input_file designs/mac/mac.x \
  --clock_period 1000 \
  --iterations 20 \
  --num_islands 1 \
  --ppa_mode fast \
  --output_dir results/exp_001 \
  --xls_src /mnt/d/final/xls \
  --backend codex
```

**Multi-island, more diversity (default `num_islands=4`, round-robin):**

```bash
python run.py \
  --input_file designs/mac/mac.x \
  --clock_period 1000 \
  --iterations 40 \
  --ppa_mode fast \
  --output_dir results/islands_001 \
  --xls_src /mnt/d/final/xls \
  --backend codex
```

> `--extra_designs` lets you evaluate multiple DSLX designs in a single run (all sharing the same `--clock_period`). Useful when you want the scorer to aggregate PPA across a suite of benchmarks. Omit it when designs have different timing requirements — run them separately with the appropriate `--clock_period` for each.

**With ASAP7 area metrics (slow — rebuilds `benchmark_main` each iter):**

```bash
python run.py \
  --input_file designs/mac/mac.x \
  --clock_period 1000 \
  --iterations 10 \
  --ppa_mode slow \
  --output_dir results/asap7_001 \
  --xls_src /mnt/d/final/xls \
  --backend codex
```

**Direct OpenAI API instead of Codex CLI:**

```bash
python run.py \
  --input_file designs/mac/mac.x \
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
  --input_file PATH         Primary DSLX design file (.x).
  --clock_period N          Target clock period in picoseconds. Fixed for the entire
                            run — never modified by the evolution process or AI.
                            Pick based on your design's critical path:
                              ~1000 ps  simple arithmetic (mac, dot_product)
                              ~1500 ps  moderate data paths
                              ~2000 ps  wide multipliers, float32 FMA (matmul_4x4)

Evolution control:
  --iterations N            Evolution iterations (default: 10).
  --mutation_target NAME    The only supported value is 'agent_scheduler' (default).
                            Kept as a CLI flag for forward compatibility.
  --num_islands N           Island populations (default: from evolve_config.yaml).
                            Use --num_islands 1 for a single linear population.
  --island_id N             Pin ALL iterations to island N (0-indexed). Useful
                            for debugging one mutation variant in isolation.

PPA evaluation depth:
  --ppa_mode MODE           fast    (default) codegen_main + block_metrics only.
                                    No benchmark_main needed. Fastest iteration.
                            medium  placeholder for Yosys synth+stat (not yet wired).
                            slow    rebuild benchmark_main each iter for asap7
                                    area + critical-path-ps.
                            slowest placeholder for Yosys+OpenROAD (not yet wired).
                            Overrides evolve_config.yaml :: ppa_mode.

AI backend:
  --backend NAME            openai | codex (default: from evolve_config.yaml).
  --model NAME              AI model name (default: from evolve_config.yaml).

Paths:
  --extra_designs PATHS     Additional DSLX designs for PPA evaluation. All designs
                            share the same --clock_period value, so only combine
                            designs whose critical paths fit the same timing budget.
                            Run designs with different timing requirements separately.
  --ppa_constraints PATH    PPA constraints YAML (default: configs/ppa_constraints.yaml).
  --evolve_config PATH      Evolve config YAML  (default: configs/evolve_config.yaml).
  --output_dir PATH         Results directory   (default: results/<timestamp>).
  --xls_src PATH            XLS source clone    (default: $XLS_SRC_PATH or /mnt/d/final/xls).
  --xls_prebuilt PATH       Optional pre-built binary directory fallback.

Other:
  --dry_run                 Validate pipeline only, no AI calls.
  --log_level LEVEL         DEBUG | INFO | WARNING (default: INFO).
```

---

## `--ppa_mode` — How it changes the loop

| Mode      | PPA source                              | Needs `benchmark_main`? | Per-iter cost   | Status                   |
| --------- | --------------------------------------- | ----------------------- | --------------- | ------------------------ |
| `fast`    | `codegen_main --block_metrics_path`     | No                      | ~30–90 s        | Implemented (default)    |
| `medium`  | `yosys -p "synth; stat"` on the Verilog | No                      | ~15–60 s extra  | Placeholder (falls back) |
| `slow`    | `benchmark_main` with `asap7` area      | Yes — rebuilt each iter | ~5–10 min extra | Implemented              |
| `slowest` | Yosys `synth_asap7` + OpenROAD PnR      | No (Yosys/OpenROAD)     | ~5–15 min extra | Placeholder (falls back) |

Placeholders emit a warning and evaluate as if mode were `fast`.

Score weights (`stage_weight`, `power_weight`/`reg_weight`, `area_weight`, `delay_weight`) live in `configs/evolve_config.yaml`. Lower scores are better. In `fast` mode the area term is zero because codegen does not emit absolute silicon area; the scoring is dominated by stages and register bits.

---

## How an Iteration Works

```
Startup:
  0a. Ensure agent_generated_scheduler / codegen_main / opt_main / ir_converter_main are built.
  0b. If --ppa_mode slow/slowest:  build benchmark_main too.
  0c. Seed each island with the baseline PPA of the unmodified scheduler.

Per iteration:
  1. Select island (round-robin; pinned via --island_id / --num_islands 1).
  2. Select parent  (tournament within the island, or global best if island is cold).
  3. Sample — sampler.py renders implement_agent_scheduler.txt and calls the AI.
              The prompt always contains: current scheduler source, reference XLS sources,
              parent score, best score, and a mutation-variant instruction.
  --- Compile-retry loop (default max_build_retries=3) ---
  4. Splice the generated C++ into xls/scheduling/agent_generated_scheduler.cc.
  5. Incremental Bazel build of the agent_scheduler target + its reverse deps.
     Compile failure: send the exact clang errors back to the AI and retry.
  6. Run the XLS pipeline on every benchmark design:
       DSLX → IR → opt → codegen  (+ optional benchmark_main if ppa_mode=slow).
  7. Extract PPA (block_metrics in fast, benchmark_main stdout in slow).
  8. Score = stages*stage_weight + flop_bits*power_weight + area*area_weight + delay*delay_weight.
  9. Insert candidate in SQLite; update island.
 10. If the best score improved, write results/<run>/best_algorithm.patch.
 11. Restore the original agent_generated_scheduler.cc.
 12. Every migration_interval iterations: copy global best into all islands.
```

The AI always sees four rotating instruction "variants" per island: register-pressure-aware, ASAP-with-tie-break, mobility-driven, and lookahead. All four reference only real APIs (`TopoSort`, `IsUntimed`, `bounds->lb/ub`, `TightenNodeLb/Ub`, `PropagateBounds`, `delay_estimator.GetOperationDelayInPs`, `node->operands()`, `node->users()`, `GetFlatBitCount`).

---

## Results layout

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

## Adding New Designs

Drop a `designs/<name>/<name>.x` file containing a DSLX `fn` or `proc` and pass it via `--input_file` or `--extra_designs`. If the design imports XLS standard-library modules (e.g. `float32`), the stdlib path is resolved automatically from `--xls_src`.

Designs with deeper data paths (wider multipliers, FMAs, convolutions) give the scheduler more room to make interesting decisions.

### Per-design clock constraints

The clock period is set once via `--clock_period` and applies to every design in the run. If your designs have different timing requirements, run them in separate invocations:

```bash
# Fast arithmetic designs — 1 ns clock
python run.py --input_file designs/mac/mac.x --clock_period 1000 ...

# Float32 FMA designs — umul alone takes ~1146 ps with asap7
python run.py --input_file designs/matmul4x4/matmul_4x4.x --clock_period 2000 ...
```

Other per-design constraints (fixed `pipeline_stages`, alternate `generator`) can still be set in `configs/ppa_constraints.yaml` under the `per_design` block using the DSLX file stem as the key.

---

## Reproducibility

All CLI arguments, both YAML configs, and the resolved `ppa_mode` are written to the `meta` table of `candidates_db.sqlite`. The XLS scheduler source is always restored to the baseline after each iteration, so a run never leaves the XLS tree in a mutated state. Every candidate's `source_diff` is stored verbatim, so any historical candidate can be reproduced by applying its diff to the baseline and rebuilding.
