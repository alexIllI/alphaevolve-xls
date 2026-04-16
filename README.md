# AlphaEvolve-XLS

> **AI-Driven Hardware Scheduling Algorithm Research**  
> Using AlphaEvolve methodology to evolve Google XLS scheduling algorithms for better PPA.

---

## What This Is

This project applies an **AlphaEvolve-inspired evolutionary loop** to automatically discover better pipeline scheduling algorithms inside [Google XLS](https://github.com/google/xls) — an open-source High-Level Synthesis (HLS) toolchain.

**The core idea**: An AI agent (via Codex CLI / OpenAI) reads the XLS scheduling source code, understands the current algorithm (SDC LP formulation), and proposes improved C++ implementations informed by research papers. Each candidate is compiled incrementally via Bazel (~2–3 min), evaluated on benchmark hardware designs, and scored on PPA (pipeline stages, area in um², critical path delay). The best algorithms are retained and used as parents for the next generation.

**What makes it real**: PPA metrics come directly from `benchmark_main` (XLS's own benchmark tool), using the `asap7` area model and unit delay model — not from heuristics or regex.

---

## Directory Structure

```
alphaevolve-xls/
├── run.py                    ← Main CLI entry point
├── configs/
│   ├── ppa_constraints.yaml  ← Clock period, delay model, area model
│   └── evolve_config.yaml    ← Islands, AI backend, iteration settings
├── designs/
│   ├── mac/mac.x             ← 32-bit multiply-accumulate
│   ├── fir_filter/fir.x      ← 8-tap FIR filter
│   ├── dot_product/dot.x     ← 8-element dot product
│   └── matmul4x4/matmul_4x4.x ← 4×4 systolic array (float32 FMA)
├── alphaevolve/
│   ├── sampler.py            ← Codex CLI / OpenAI SDK AI interface
│   ├── evaluator.py          ← patch → build → run → extract PPA
│   ├── ppa_metrics.py        ← Parse benchmark_main output for PPA
│   ├── database.py           ← SQLite candidate store
│   ├── islands.py            ← Island-based population management
│   └── prompts/              ← System + Jinja2 templates for AI
├── xls_tools/
│   ├── build.py              ← Incremental Bazel build wrapper
│   └── pipeline.py           ← DSLX → IR → opt → benchmark → Verilog
├── knowledge/
│   ├── papers/               ← SDC, force-directed, scheduling theory
│   └── heuristics/           ← ASAP/ALAP, list scheduling references
└── results/                  ← Experiment outputs (gitignored)
```

---

## Prerequisites

- **WSL2** on Windows (Ubuntu 22.04 recommended) — or any Linux machine
- **Google XLS** cloned and built from source at a known path (e.g. `/mnt/d/final/xls`)
- **OpenAI API key** or **Codex CLI** configured
- **Python 3.11+** with a virtual environment

---

## Setup

```bash
# 1. Clone this repo alongside XLS source
cd /mnt/d/final
git clone https://github.com/alexIllI/alphaevolve-xls.git
cd alphaevolve-xls

# 2. Create Python virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. (Optional) Copy and edit environment file
cp .env.example .env
# set OPENAI_API_KEY if using openai backend
```

---

## Build XLS from Source (one-time, ~2–6 hours)

This is required to mutate and rebuild the scheduler. Do this once; subsequent builds are incremental (~2–3 min).

```bash
cd /mnt/d/final/xls

# Build all required tools including benchmark_main
bazel build -c opt \
  //xls/tools:codegen_main \
  //xls/tools:opt_main \
  //xls/dslx/ir_convert:ir_converter_main \
  //xls/dev_tools:benchmark_main
```

> **Note:** `benchmark_main` is the largest target (~122 MB, pulls in LLVM/JIT). It provides real area (asap7) and critical path delay metrics. The build may take 1–2 additional hours beyond the other tools.

---

## Quick Start — Dry Run (Validate Pipeline)

Run this first to confirm everything is wired up correctly. No AI calls, no mutations.

```bash
cd /mnt/d/final/alphaevolve-xls
source .venv/bin/activate

# Dry run with all benchmark designs
python run.py \
  --input_file designs/matmul4x4/matmul_4x4.x \
  --extra_designs designs/mac/mac.x designs/dot_product/dot.x \
  --dry_run \
  --xls_src /mnt/d/final/xls
```

**Expected output:**
```
DRY RUN — testing pipeline only (no AI calls)
  ✓ matmul_4x4: 1 stages, 0 flops, crit_path=72ps, area=0.0um², score=1000   (benchmark_main)
  ✓ mac:        1 stages, 128 flops, crit_path=2ps, area=426.0um², score=43728 (benchmark_main)
  ✓ dot:        1 stages, 288 flops, crit_path=15ps, area=3386.0um², score=339888 (benchmark_main)
Dry run complete.
```

---

## Run the Evolution

```bash
# Evolve using mac as primary benchmark, with codex backend
python run.py \
  --input_file designs/mac/mac.x \
  --extra_designs designs/dot_product/dot.x \
  --iterations 20 \
  --output_dir results/mac_exp_001 \
  --xls_src /mnt/d/final/xls \
  --backend codex \
  --mutation_target sdc_objective

# Or use OpenAI API directly
python run.py \
  --input_file designs/mac/mac.x \
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
  --input_file PATH         Primary DSLX design file (.x)

Optional:
  --extra_designs PATHS     Additional benchmark designs (space-separated)
  --ppa_constraints PATH    PPA constraints YAML (default: configs/ppa_constraints.yaml)
  --evolve_config PATH      Evolution config YAML (default: configs/evolve_config.yaml)
  --iterations N            Evolution iterations (default: 10)
  --output_dir PATH         Results directory (default: results/<timestamp>)
  --xls_src PATH            XLS source clone (default: $XLS_SRC_PATH or /mnt/d/final/xls)
  --mutation_target NAME    Function to evolve: sdc_objective | delay_constraints | min_cut
  --backend NAME            AI backend: openai | codex (default: from evolve_config.yaml)
  --model NAME              AI model name (default: from evolve_config.yaml)
  --dry_run                 Validate pipeline only, no AI calls
  --log_level LEVEL         DEBUG | INFO | WARNING (default: INFO)
```

---

## How the Evolution Works

```
For each iteration:
  1. Select island (round-robin) + parent candidate (tournament selection)
  2. Build prompt: current algorithm source + PPA score + paper excerpts + instruction
  3. AI generates new C++ implementation of target function
  4. Patch xls/scheduling/sdc_scheduler.cc with generated code
  5. Bazel incremental build (~2-3 min, only changed files recompiled)
  6. Run XLS pipeline on each benchmark design:
       DSLX → IR → opt → benchmark_main (PPA) → codegen (Verilog)
  7. Extract PPA from benchmark_main: critical_path_ps, area_um2, flops
  8. Score = stages×1000 + flops×1 + area×100  (lower is better)
  9. If improved: save diff to database, propagate to islands
  10. Restore original sdc_scheduler.cc
```

**What `--scheduling_strategy=sdc` does:** Forces `benchmark_main` and `codegen_main` to use the SDC scheduler (`sdc_scheduler.cc`) — the file we mutate. Every PPA measurement reflects the AI's proposed algorithm.

**Mutation targets:**
| Target | C++ Function | What Changes |
|--------|-------------|--------------|
| `sdc_objective` | `SDCSchedulingModel::SetObjective()` | What the LP minimizes (stages, registers, delay, weighted) |
| `delay_constraints` | `ComputeCombinationalDelayConstraints()` | How timing constraints are added between operations |
| `min_cut` | `MinCutScheduler::Schedule()` | Alternative: skip LP, use graph partitioning instead |

---

## Baseline PPA (original XLS SDC scheduler)

| Design | Stages | Crit path | Area | Score |
|--------|--------|-----------|------|-------|
| `mac` (32-bit MAC) | 1 | 2 ps | 426 um² | 43,728 |
| `dot` (8-elem dot product) | 1 | 15 ps | 3,386 um² | 339,888 |
| `matmul_4x4` (4×4 float32 systolic) | 1 | 72 ps | — | 1,000 |

When the scheduler improves, these scores go down.

---

## Results

Each run produces in `--output_dir`:

| File | Contents |
|------|----------|
| `best_algorithm.patch` | Unified diff of the best C++ found — apply to XLS |
| `ppa_report.json` | Final PPA metrics and top 3 candidates |
| `evolution_log.csv` | All iterations: scores, build status, durations |
| `candidates_db.sqlite` | Full SQLite database of every candidate |
| `dry_run/<design>/` | Per-design: `_benchmark.txt`, `_block_metrics.textproto`, `.v`, `.ir` |

Apply the best result permanently:
```bash
patch /mnt/d/final/xls/xls/scheduling/sdc_scheduler.cc < results/mac_exp_001/best_algorithm.patch
```

---

## Adding New Designs

1. Create `designs/<name>/<name>.x` with a DSLX function or proc
2. Pass it via `--input_file` or `--extra_designs`

Designs with deeper data paths (larger multiplications, convolutions) provide richer scheduling decisions. For designs importing XLS standard library modules (e.g. `float32`), the stdlib path is auto-configured from `--xls_src`.

---

## Reproducibility

- All experiment parameters are saved to `candidates_db.sqlite` (metadata table)
- XLS source is always restored after each iteration — no permanent mutations during a run
- Results include the full source diff to reproduce the best algorithm found
