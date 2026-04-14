# AlphaEvolve-XLS

> **AI-Driven Hardware Scheduling Algorithm Research**
> Using AlphaEvolve methodology to evolve Google XLS scheduling algorithms for better PPA.

---

## What This Is

This project applies an **AlphaEvolve-inspired evolutionary loop** to automatically discover better pipeline scheduling algorithms inside [Google XLS](https://github.com/google/xls) — an open-source High-Level Synthesis (HLS) toolchain.

**The core idea**: An AI agent (via Codex CLI / OpenAI) reads the XLS scheduling source code, understands the current algorithm (based on SDC LP formulation), and proposes improved C++ implementations informed by research papers (force-directed scheduling, list scheduling, register lifetime minimization). Each candidate is compiled incrementally via Bazel (~2–3 min), evaluated on benchmark DSLX hardware designs, and scored on PPA (pipeline stages, register bits, critical path). The best algorithms are retained and used as parents for the next generation.

**Key distinction**: The AI implements *real scheduling algorithms*, not parameter tweaks.

---

## Directory Structure

```
alphaevolve-xls/
├── run.py                    ← Main CLI entry point
├── Dockerfile                ← Reproducible environment (Ubuntu 22.04 + Bazel)
├── docker-compose.yml        ← Volume mounts + persistent Bazel cache
├── configs/
│   ├── ppa_constraints.yaml  ← Clock period, delay model, pipeline mode
│   └── evolve_config.yaml    ← Islands, AI backend, iteration settings
├── designs/
│   ├── mac/mac.x             ← 32-bit MAC (verified baseline)
│   ├── fir_filter/fir.x      ← 8-tap FIR filter benchmark
│   └── dot_product/dot.x     ← 8-element dot product benchmark
├── alphaevolve/
│   ├── sampler.py            ← Codex CLI / OpenAI SDK AI interface
│   ├── evaluator.py          ← patch → build → run → extract PPA
│   ├── database.py           ← SQLite candidate store
│   ├── islands.py            ← Island-based population management
│   ├── ppa_metrics.py        ← Parse XLS schedule output for PPA
│   └── prompts/              ← System + Jinja2 templates for AI
├── xls_tools/
│   ├── build.py              ← Incremental Bazel build wrapper
│   └── pipeline.py           ← DSLX → IR → opt → Verilog pipeline
├── knowledge/
│   ├── papers/               ← SDC, force-directed, scheduling theory
│   └── heuristics/           ← ASAP/ALAP, list scheduling references
└── results/                  ← Experiment outputs (gitignored)
```

**Separation**: This repo contains only research orchestration code. The Google XLS source (`d:\final\xls`) is mounted separately and mutated during experiments, then restored after each iteration.

---

## Prerequisites

- **WSL2** on Windows (Ubuntu 22.04) — or any Linux machine
- **Google XLS** cloned at a known path (e.g. `/mnt/d/final/xls`)
- **OpenAI API key** (for Codex CLI or direct API)
- **Docker** (recommended) or manual Bazel install

---

## Quick Start — WSL

```bash
# 1. Clone this repo (separate from XLS source)
cd /mnt/d/final
git clone <this-repo-url> alphaevolve-xls
cd alphaevolve-xls

# 2. Run setup (installs Bazel, Python venv, Codex CLI)
bash scripts/setup_wsl.sh

# 3. Activate environment and configure
source .venv/bin/activate
cp .env.example .env
# Edit .env: set OPENAI_API_KEY and verify XLS_SRC_PATH

# 4. Verify the pipeline (dry run, uses pre-built XLS binary)
python run.py --input_file designs/mac/mac.x --dry_run

# 5. Trigger the one-time XLS source build (2-6 hours, then incremental)
cd /mnt/d/final/xls
bazel build -c opt //xls/tools:codegen_main //xls/tools:opt_main //xls/tools:ir_converter_main

# 6. Run the evolution!
cd /mnt/d/final/alphaevolve-xls
python run.py \
  --input_file designs/mac/mac.x \
  --iterations 20 \
  --output_dir results/mac_exp_001
```

---

## Quick Start — Docker (Recommended for Reproducibility)

```bash
# 1. Copy and configure environment
cp .env.example .env
# Edit .env: set OPENAI_API_KEY

# 2. Build the Docker image
docker-compose build

# 3. Dry run (validates everything, triggers Bazel build on first run)
./scripts/run_docker.sh python run.py --input_file designs/mac/mac.x --dry_run

# 4. Run experiment
./scripts/run_docker.sh python run.py \
  --input_file designs/mac/mac.x \
  --iterations 20 \
  --output_dir results/mac_exp_001
```

> **Note**: The first `docker-compose run` will trigger the XLS Bazel build (~2–6 hours). The Bazel cache is stored in a named Docker volume (`alphaevolve_bazel_cache`) and persists across container restarts. Subsequent builds only recompile changed files (~2–3 minutes).

---

## CLI Reference

```
python run.py [OPTIONS]

Required:
  --input_file PATH         Primary DSLX design file (.x)

Optional:
  --extra_designs PATHS     Additional benchmark designs
  --ppa_constraints PATH    PPA constraints YAML (default: configs/ppa_constraints.yaml)
  --evolve_config PATH      Evolution config YAML (default: configs/evolve_config.yaml)
  --iterations N            Evolution iterations (default: 10)
  --output_dir PATH         Results directory (default: results/<timestamp>)
  --xls_src PATH            XLS source clone (default: $XLS_SRC_PATH)
  --mutation_target NAME    Function to evolve: sdc_objective | delay_constraints | min_cut
  --backend NAME            AI backend: openai | codex
  --model NAME              AI model (default: o3)
  --dry_run                 Validate pipeline only, no AI calls
  --log_level LEVEL         DEBUG | INFO | WARNING
```

---

## How the Evolution Works

```
For each iteration:
  1. Select island (round-robin) + parent candidate (tournament selection)
  2. Build prompt: current algorithm source + PPA score + paper excerpts + instruction
  3. AI generates new C++ implementation of target function
  4. Patch XLS source file with generated code
  5. Bazel incremental build (~2-3 min)
  6. Run XLS pipeline on benchmark designs → collect schedule metrics
  7. Extract PPA: num_stages, pipeline_reg_bits, max_stage_delay_ps
  8. If improved: save diff to database, propagate to islands
  9. Restore original source file
```

**Mutation targets** (which C++ functions the AI rewrites):
- `sdc_objective`: `SDCSchedulingModel::SetObjective()` — the LP objective function
- `delay_constraints`: `ComputeCombinationalDelayConstraints()` — timing constraint generation
- `min_cut`: `MinCutScheduler::Schedule()` — alternative min-cut partitioning

**Knowledge base** (what the AI reads before generating code):
- SDC scheduling theory (Cong & Zhang 2006)
- Force-directed scheduling (Paulin & Knight 1989)
- List scheduling / ASAP/ALAP / mobility heuristics

---

## Results

Each run produces in `--output_dir`:

| File | Contents |
|------|----------|
| `best_algorithm.patch` | Unified diff of the best C++ algorithm to apply to XLS |
| `ppa_report.json` | Final PPA metrics and top candidates |
| `evolution_log.csv` | All iterations: scores, build status, durations |
| `candidates_db.sqlite` | Full SQLite database of all candidates |
| `eval_runs/` | Per-iteration Verilog and schedule outputs |

---

## Adding New Designs

1. Create `designs/<name>/<name>.x` with a DSLX function
2. Add `--input_file designs/<name>/<name>.x` to your run command

Designs with deeper pipeline structure (more multiplications, larger data paths) provide richer scheduling decisions and more interesting PPA trade-offs.

---

## Adding Research Knowledge

Add `.md` files to `knowledge/papers/` or `knowledge/heuristics/`. The sampler automatically loads all knowledge files and includes them in the AI prompt. Good additions:
- Summaries of HLS scheduling papers
- Known good heuristics or constraints
- Analysis of why certain schedules are suboptimal

---

## Reproducibility

- All experiment parameters are logged in `candidates_db.sqlite` (`run_meta` table)
- The Dockerfile pins Ubuntu 22.04 + Bazel 7.7.1 (from `.bazelversion`)
- Results include the full source diff to reproduce the best algorithm
- Apply the best result: `patch xls/scheduling/sdc_scheduler.cc < best_algorithm.patch`
