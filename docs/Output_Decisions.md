# AlphaEvolve-XLS — Output Layout & Design Decisions

> Part of the system architecture docs.
> See also: [Architecture.md](Architecture.md) · [Evolution_Loop.md](Evolution_Loop.md) · [Scoring_PPA.md](Scoring_PPA.md)

---

## Part 8 — Per-run output layout

```
results/<timestamp>/
├── candidates_db.sqlite          Every candidate: code, score, diff, metrics, status.
├── evolution_log.csv             Flat CSV of every candidate (for plotting/analysis).
├── ppa_report.json               Final summary with the top 3 candidates.
├── best_algorithm.patch          Unified diff against the baseline scheduler.
└── eval_runs/
    ├── iter<NNNN>_island<K>_stages.log    Real-time stage log (on_stage_start/on_stage).
    │                                      Use `tail -f` during a slow run to monitor progress.
    ├── iter<NNNN>_island<K>_attempts.log  Per-iteration retry record. One entry per
    │                                      attempt (number, build_status, notes).
    │                                      Written after every attempt — failed retries
    │                                      are preserved even if the loop continues.
    └── iter<NNNN>_island<K>/
        └── <design>/
            ├── <design>.ir                   after ir_converter_main
            ├── <design>_opt.ir               after opt_main
            ├── <design>_block_metrics.textproto  codegen_main (when it succeeds)
            ├── <design>_schedule.textproto       codegen_main (when it succeeds)
            ├── <design>.v                        codegen_main (when it succeeds)
            └── <design>_benchmark.txt            benchmark_main (slow modes);
                                                  "benchmark_main skipped" in fast mode
```

---

## Part 9 — Data-flow summary

```
INPUTS                         EVOLUTION ENGINE                   OUTPUTS
──────                         ────────────────                   ───────

designs/*.x            ──────►│
designs/*_benchmark.txt ─────►│   (optional baseline AI context)
                              │
configs/                      │   For each iteration:
  evolve_config.yaml  ───────►│
  ppa_constraints.yaml ──────►│   1. sampler.py → AI → new C++ body
                              │        (reference sources + variants +
knowledge/**/*.md  ──────────►│         baseline context + compile_error on retry)
                              │                   │
xls/ (source)      ──────────►│                   ▼
  xls/scheduling/             │   2. evaluator.py → splice → Bazel rebuild
    agent_generated_          │            (agent + codegen + opt + ir_converter
    scheduler.cc ────────────►│             in fast mode; + benchmark_main in slow)
    (ONLY file mutated)       │                   │
                              │                   ▼
                              │   3. pipeline.py  DSLX → IR → opt →
                              │           [benchmark_main if slow] →
                              │           codegen (non-fatal fail in slow
                              │           when benchmark succeeded)
                              │                   │
                              │                   ▼
                              │   4. ppa_metrics.py → score
                              │                   │
                              │                   ▼
                              │   5. db.insert, island.add, migrate
                              │                   │
                              │                   ▼
                              │   6. restore baseline; next iteration
                              └───────────────────┘
                                      │
                                      ▼
                             results/<timestamp>/
                             best_algorithm.patch   ← apply to XLS for permanent adoption
                             ppa_report.json
                             evolution_log.csv
                             candidates_db.sqlite
                             eval_runs/iter*_island*/
```

---

## Part 10 — What is intentionally untouched

By design, these XLS files are **never modified** by the evolution loop:

| File | Reason |
|------|--------|
| `xls/scheduling/sdc_scheduler.cc` | XLS still uses it for `--scheduling_strategy=sdc`; we just stop invoking that strategy |
| `xls/scheduling/min_cut_scheduler.cc` | Referenced as prompt context only, so the AI can learn XLS scheduling APIs |
| `xls/scheduling/run_pipeline_schedule.cc` | Already dispatches `AGENT` correctly |
| `xls/scheduling/scheduling_options.h` | Defines `SchedulingStrategy::AGENT` enum |
| `xls/scheduling/BUILD` | `agent_generated_scheduler` is already a dep of `run_pipeline_schedule` |
| `alphaevolve/database.py` | Schema is stable |
| `alphaevolve/sampler.py` | Only the prompt template body changes when tuning the AI |

The XLS source tree is **always restored** after each iteration. No iteration leaves the tree in a mutated state.

---

## Part 11 — Architectural decisions

### Why a standalone scheduler file?

Earlier approaches mutated `SDCSchedulingModel::SetObjective()`, `ComputeCombinationalDelayConstraints()`, and `MinCutScheduler::Schedule()` inside XLS proper. Every iteration was expensive (SDC LP solver links against ortools) and caused frequent compile failures because the AI had to respect XLS-internal invariants across multiple files.

The current design collapses all mutation targets into one standalone file that XLS opts into via `--scheduling_strategy=agent`. Three consequences:

1. **Cheaper incremental builds.** The agent file is small with few reverse-deps and does not link against ortools. Per-iteration relink is limited to the scheduler + the thin set of tools that include it.
2. **Tractable AI contract.** Fixed signature, five named helpers, small set of `ScheduleBounds` APIs. Compile failures are easier to self-correct.
3. **`benchmark_main` only needed in slow mode.** `codegen_main` already emits `block_metrics` with stages and register bits — enough signal for fast-mode evolution.

### Why `--clock_period` is a CLI flag, not a YAML value

The clock period is a fixed hardware constraint for the entire run. Keeping it exclusively in the CLI prevents any YAML edit, config override, or AI-generated code from silently changing the timing target mid-run. The YAML files hold only tuning knobs.

### Why islands start cold (no pre-seeding)

Seeding islands with the baseline scheduler's PPA would require running the full XLS pipeline before evolution begins — adding startup latency and coupling to the current binary state. Instead, islands start empty and the first iteration's parent is `None`. This avoids startup errors when the baseline binary does not yet support `--scheduling_strategy=agent`.

### Why proc designs need `--ppa_mode slow`

`codegen_main --generator=pipeline` does not support proc networks (they require `--generator=block` plus reset/state handling). In `--ppa_mode fast`, proc designs have no fallback PPA source when `codegen_main` fails. In `--ppa_mode slow`, `benchmark_main` runs the scheduler standalone and prints timing metrics before any codegen step, allowing proc designs to be scored correctly.

### AI output sanitization

The AI is instructed not to emit `#include` directives or wrap output in `namespace xls {}`. These rules are stated prominently in the prompt because violations cause compile failures: the output is spliced mid-file inside an existing `namespace xls` block. Compile errors are captured and fed back for up to `max_build_retries=3` retries per iteration.
