# AlphaEvolve-XLS — System Architecture

> Last updated: 2026-04-17 — reflects the agent-scheduler-only refactor and the `--ppa_mode` flag.

This doc is the reference for how AlphaEvolve-XLS is wired together. `README.md` is the user-facing guide; this file is the internals guide.

---

## Part 1 — What gets evolved

Exactly one C++ function is evolved:

```
file:     xls/scheduling/agent_generated_scheduler.cc
function: absl::StatusOr<ScheduleCycleMap> AgentGeneratedScheduler(
              FunctionBase* f,
              int64_t pipeline_stages,
              int64_t clock_period_ps,
              const DelayEstimator& delay_estimator,
              sched::ScheduleBounds* bounds,
              absl::Span<const SchedulingConstraint> constraints);
```

XLS already dispatches this function when `--scheduling_strategy=agent` is passed to any XLS tool (e.g. `codegen_main`, `benchmark_main`). The dispatch lives in `xls/scheduling/run_pipeline_schedule.cc :: RunPipelineSchedule()`, around the block:

```
if      strategy == MIN_CUT   → MinCutScheduler::Schedule()
else if strategy == AGENT     → AgentGeneratedScheduler()   ← we mutate this
else if strategy == RANDOM    → random walk
else                          → SDCSchedulingModel / ASAP
```

We do **not** modify any of the other schedulers, the dispatch code, the `scheduling_options.h` enum, or the XLS BUILD files. They are already correct; we just stop calling them.

The XLS BUILD dependency `//xls/scheduling:agent_generated_scheduler` is already a dep of `run_pipeline_schedule`, so whenever `agent_generated_scheduler.cc` changes, everything downstream (codegen_main, benchmark_main) gets a partial relink.

### The scheduler contract (what the AI must implement)

1. Walk nodes via `TopoSort(f)`.
2. Skip any node where `IsUntimed(node)` is true.
3. Read the feasible window `[bounds->lb(node), bounds->ub(node)]`.
4. Choose a cycle inside that window using a principled heuristic.
5. Record the choice in a `ScheduleCycleMap` (`absl::flat_hash_map<Node*, int64_t>`).
6. Pin it with `bounds->TightenNodeLb(node, cycle)` and `bounds->TightenNodeUb(node, cycle)`.
7. Call `bounds->PropagateBounds()` after every assignment.
8. Return the completed map.

Helper functions already defined in the file (`NodeBitCount`, `NodeFanout`, `EstimateBoundaryRegisterCost`, `ScoreCandidateCycle`) are available to reuse. The AI may add its own helpers inside a fresh anonymous namespace block above the target function.

---

## Part 2 — File map

```
alphaevolve-xls/
│
├── run.py                          MAIN ENTRY POINT. Parses CLI, resolves ppa_mode,
│                                   builds the components, runs the evolution loop.
│
├── configs/
│   ├── evolve_config.yaml          Hyperparameters: num islands, AI backend,
│   │                               score weights, ppa_mode (default "fast"),
│   │                               mutation_types=[agent_scheduler].
│   └── ppa_constraints.yaml        Hardware target: clock_period_ps, delay_model,
│                                   generator (pipeline), pipeline_stages.
│
├── designs/
│   ├── mac/mac.x                   32-bit MAC
│   ├── fir_filter/fir.x            8-tap FIR filter
│   ├── dot_product/dot.x           8-element dot product
│   └── matmul4x4/matmul_4x4.x      4×4 systolic array (float32 FMA)
│
├── alphaevolve/
│   ├── sampler.py                  AI interface. Builds the Jinja2 prompt, calls
│   │                               `codex exec` via subprocess OR the OpenAI SDK,
│   │                               strips markdown fences, returns C++ string.
│   ├── evaluator.py                Splice → build → run → extract PPA → restore.
│   │                               MUTATION_TARGETS dict has ONE entry:
│   │                                 "agent_scheduler": (
│   │                                   "xls/scheduling/agent_generated_scheduler.cc",
│   │                                   "absl::StatusOr<ScheduleCycleMap>"
│   │                                   " AgentGeneratedScheduler(")
│   │                               `_splice_function` uses signature + brace-counting
│   │                               to replace the whole function body atomically.
│   │                               Accepts ppa_mode via constructor.
│   ├── ppa_metrics.py              Parses metrics. Source priority:
│   │                                 1. benchmark_main stdout (ppa_mode=slow only)
│   │                                 2. block_metrics textproto (always, codegen)
│   │                                 3. schedule textproto (tertiary)
│   │                                 4. Verilog regex (last resort)
│   │                               Score = stages * stage_weight
│   │                                     + pipeline_flops * power_weight
│   │                                     + area_um2 * area_weight
│   │                                     + critical_path_ps * delay_weight.
│   ├── database.py                 SQLite. Stores candidate, metrics, diff, status.
│   ├── islands.py                  Island population manager. Only mutation_type
│   │                               in use is "agent_scheduler"; each island picks
│   │                               one of 4 instruction variants per iteration
│   │                               (register-pressure / ASAP / mobility / lookahead).
│   └── prompts/
│       ├── system.txt              AI persona: "expert in HLS scheduling".
│       ├── implement.txt           Fallback Jinja2 template (agent-scheduler-shaped).
│       └── implement_agent_scheduler.txt  Active template used by sampler.py.
│
├── xls_tools/
│   ├── build.py                    Bazel wrapper. Targets:
│   │                                 //xls/scheduling:agent_generated_scheduler
│   │                                 //xls/tools:codegen_main
│   │                                 //xls/tools:opt_main
│   │                                 //xls/dslx/ir_convert:ir_converter_main
│   │                                 //xls/dev_tools:benchmark_main  (slow only)
│   │                               Classmethod `iteration_targets_for_mode(mode)`
│   │                               returns the per-iteration rebuild list.
│   └── pipeline.py                 DSLX → IR → opt → schedule → Verilog.
│                                   Always uses --scheduling_strategy=agent.
│                                   `run(..., ppa_mode=...)`:
│                                     fast|medium: block_metrics only.
│                                     slow|slowest: also runs benchmark_main.
│
├── knowledge/
│   ├── papers/*.md                 Scheduling-theory summaries. Loaded into prompts.
│   └── heuristics/*.md             ASAP/ALAP/mobility references.
│
├── plan/plan.txt                   Architecture plan (source of truth for refactors).
│
├── scripts/                        Ad-hoc validation helpers.
│
└── results/<timestamp>/            Per-run artefacts (see Part 7).
```

---

## Part 3 — The evolution loop

```
python run.py \
    --input_file designs/mac/mac.x \
    --extra_designs designs/dot_product/dot.x \
    --iterations 20 \
    --ppa_mode fast \
    --backend codex
       │
       ▼
┌────────────────────────────────────────────────────────────────────────┐
│ run.py                                                                 │
│   1. Load ppa_constraints.yaml + evolve_config.yaml.                   │
│   2. Apply CLI overrides (--mutation_target, --backend, --model,       │
│      --ppa_mode, --num_islands, --island_id).                          │
│   3. Validate ppa_mode ∈ {fast, medium, slow, slowest}.                │
│      medium/slowest emit a placeholder warning.                        │
│   4. Construct XLSBuilder + XLSPipeline + Sampler + Evaluator          │
│      (ppa_mode is passed into Evaluator) + IslandManager + CandidateDB.│
│   5. Only build benchmark_main when ppa_mode in (slow, slowest)        │
│      OR --dry_run.                                                     │
│   6. Seed every island with the UNMODIFIED agent_generated_scheduler.  │
└────────────────────────────────────────────────────────────────────────┘
       │
       │  for iteration in range(args.iterations):
       ▼
┌────────────────────────────────────────────────────────────────────────┐
│ STEP 1: Island selection                                               │
│   islands.py → IslandManager.select_island(iteration)                  │
│   - --num_islands 1  → always island 0                                 │
│   - --island_id N    → always island N                                 │
│   - otherwise        → round-robin by iteration                        │
└────────────────────────────────────────────────────────────────────────┘
       │
       ▼
┌────────────────────────────────────────────────────────────────────────┐
│ STEP 2: Parent selection                                               │
│   IslandManager.select_parent(island)                                  │
│   Tournament (size 2) over island's successful candidates,             │
│   else fall back to the global best in the DB,                         │
│   else return None (cold start).                                       │
└────────────────────────────────────────────────────────────────────────┘
       │
       ▼
┌────────────────────────────────────────────────────────────────────────┐
│ STEP 3: Mutation instruction                                           │
│   IslandManager.mutation_instruction_for(island, iteration)            │
│   Returns one of 4 variants for "agent_scheduler":                     │
│     0: register-pressure-aware list scheduler                          │
│     1: ASAP-first heuristic with delay-model tie-breaking              │
│     2: mobility-driven greedy                                          │
│     3: deterministic multistage heuristic with lookahead               │
│   All variants name real APIs only.                                    │
└────────────────────────────────────────────────────────────────────────┘
       │
       ▼
┌────────────────────────────────────────────────────────────────────────┐
│ STEP 4: AI sampling (sampler.py)                                       │
│                                                                        │
│   a) Load knowledge base (knowledge/**/*.md).                          │
│   b) Read current agent_generated_scheduler.cc.                        │
│   c) Build a reference-source bundle:                                  │
│        - current standalone scheduler                                  │
│        - SDC scheduler (reference)                                     │
│        - Min-cut scheduler (reference)                                 │
│        - run_pipeline_schedule.cc (dispatch reference)                 │
│        - scheduling_options.h (strategy enum)                          │
│   d) Render implement_agent_scheduler.txt with:                        │
│        mutation_target, target_file_path, required_signature,          │
│        best_score / best_num_stages / best_reg_bits / best_delay_ps,   │
│        parent_score / parent_num_stages / parent_reg_bits / ...,       │
│        mutation_instruction, knowledge_context,                        │
│        current_function_source, reference_source_bundle,               │
│        compile_error (None on first attempt).                          │
│   e) Backend:                                                          │
│        codex  → codex exec -m <model> --sandbox read-only              │
│                 --skip-git-repo-check --ephemeral -o /tmp/out.cpp -    │
│                 (prompt on stdin, response in file)                    │
│        openai → chat.completions.create(model=evo.ai_model,            │
│                 messages=[system, user], max_tokens=evo.max_tokens)    │
│   f) Strip markdown fences → return raw C++ string.                    │
└────────────────────────────────────────────────────────────────────────┘
       │
       ▼
┌────────────────────────────────────────────────────────────────────────┐
│ STEP 5: Evaluate (evaluator.py) — the critical path                    │
│                                                                        │
│   5a. _splice_function(source, signature, new_body)                    │
│       - Regex-locate the signature                                     │
│       - Brace-count forward to find the matching `}`                   │
│       - Replace the whole function (signature + body)                  │
│                                                                        │
│   5b. builder.backup(target_file)  (writes .bak)                       │
│       builder.apply (target_file, new_source)                          │
│                                                                        │
│   5c. builder.build(                                                   │
│           XLSBuilder.iteration_targets_for_mode(self.ppa_mode))        │
│       fast|medium  → agent_generated_scheduler + codegen_main          │
│                      + opt_main + ir_converter_main                    │
│       slow|slowest → above + benchmark_main                            │
│                                                                        │
│   5d. If build fails:                                                  │
│       - restore the .bak                                               │
│       - run.py trims the compiler output to the first ~30 error/note   │
│         lines and feeds it back as `compile_error` for another attempt │
│         (default max_build_retries=3).                                 │
│                                                                        │
│   5e. On success: run XLS pipeline on each benchmark design.           │
│                                                                        │
│   5f. Restore the backup and cleanup.                                  │
└────────────────────────────────────────────────────────────────────────┘
       │
       ▼
┌────────────────────────────────────────────────────────────────────────┐
│ STEP 6: XLS pipeline (pipeline.py)                                     │
│   For each design:                                                     │
│     DSLX → IR           (ir_converter_main, --dslx_stdlib_path set)    │
│     IR  → optimized IR  (opt_main)                                     │
│     IR  → codegen        (codegen_main --scheduling_strategy=agent     │
│                            --block_metrics_path=<...>.textproto)       │
│     [optional, slow]                                                   │
│     IR  → benchmark_main (benchmark_main --scheduling_strategy=agent)  │
└────────────────────────────────────────────────────────────────────────┘
       │
       ▼
┌────────────────────────────────────────────────────────────────────────┐
│ STEP 7: PPA extraction (ppa_metrics.py)                                │
│   Priority (first available wins per metric):                          │
│     benchmark_main stdout  → critical_path_ps, total_area_um2,         │
│                               total_pipeline_flops, num_stages         │
│     block_metrics textproto → flop_count, max_reg_to_reg_delay_ps,     │
│                               pipeline_stages, pipeline_reg_bits       │
│     schedule textproto     → length, min_clock_period_ps               │
│     Verilog regex          → approximate reg count (last resort)       │
│   Score = stages * stage_weight                                        │
│         + pipeline_flops * power_weight                                │
│         + area_um2 * area_weight                                       │
│         + critical_path_ps * delay_weight.                             │
└────────────────────────────────────────────────────────────────────────┘
       │
       ▼
┌────────────────────────────────────────────────────────────────────────┐
│ STEP 8: Record + migrate                                               │
│   - db.insert(candidate)                                               │
│   - island.add(candidate); island is truncated to 50 by score.         │
│   - If candidate score < best_score:                                   │
│       write results/<run>/best_algorithm.patch (unified diff).         │
│   - Every migration_interval iterations: copy global best into         │
│     every island that doesn't already have it (cross-pollination).     │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Part 4 — XLS pipeline, command level

### Stage 1: DSLX → IR

```bash
ir_converter_main \
    --dslx_stdlib_path=<xls_src>/xls/dslx/stdlib \
    --top=<auto-detected> \
    design.x
```

`_detect_dslx_top()` scans the DSLX source for the last non-test `proc`/`fn` and sets it as the package top so downstream tools don't need the flag again.

### Stage 2: IR → optimized IR

```bash
opt_main design.ir
```

Dead-code elimination, constant folding, bit-width narrowing, CSE. For proc-network designs like `matmul_4x4` this is the slowest optimization step.

### Stage 3: Codegen + block metrics (always run)

```bash
codegen_main design.opt.ir \
    --generator=pipeline \
    --delay_model=unit \
    --clock_period_ps=1000 \
    --scheduling_strategy=agent \
    --output_verilog_path=design.v \
    --output_schedule_path=design_schedule.textproto \
    --block_metrics_path=design_block_metrics.textproto
```

With `--scheduling_strategy=agent`, codegen_main calls `AgentGeneratedScheduler()` internally. Every PPA measurement we extract reflects the mutated C++.

`design_block_metrics.textproto` contains an `XlsMetricsProto`:

```
block_metrics {
  flop_count: 128
  max_reg_to_reg_delay_ps: 2
  max_input_to_reg_delay_ps: 2
  max_reg_to_output_delay_ps: 0
  pipeline_stages: 1
  pipeline_reg_bits: 128
}
```

This is the PPA source in `--ppa_mode fast`.

### Stage 4 (optional, slow modes): benchmark_main

```bash
benchmark_main design.opt.ir \
    --delay_model=unit \
    --area_model=asap7 \
    --scheduling_strategy=agent \
    --run_evaluators=false \
    --generator=pipeline \
    --clock_period_ps=1000
```

Prints a human-readable report including `Critical path delay`, `Total area`, `Total pipeline flops`, and per-stage pipeline breakdown. The parser tolerates a nonzero exit code as long as the `Pipeline:` section is present — this matters for proc-network designs where the register-reset lowering pass inside benchmark_main fails non-fatally after it has already printed the scheduling stats.

---

## Part 5 — `--ppa_mode` matrix

| Mode      | PPA source                                    | Rebuilds `benchmark_main`? | Score terms that can be non-zero         | Status      |
| --------- | --------------------------------------------- | -------------------------- | ---------------------------------------- | ----------- |
| `fast`    | `block_metrics` textproto                     | No                         | stages, pipeline_reg_bits                | Implemented |
| `medium`  | (planned) `yosys -p "synth; stat"` on Verilog | No                         | stages, reg_bits, gate_count             | Placeholder |
| `slow`    | `benchmark_main` stdout (asap7 area model)    | Yes, per iteration         | stages, reg_bits, area_um2, delay_ps     | Implemented |
| `slowest` | (planned) Yosys `synth_asap7` + OpenROAD      | No (needs Yosys/OpenROAD)  | stages, reg_bits, true-silicon area, WNS | Placeholder |

Implementation references:

- `xls_tools/build.py :: XLSBuilder.iteration_targets_for_mode(mode)`
  Adds `//xls/dev_tools:benchmark_main` when `mode in ("slow", "slowest")`.
- `xls_tools/pipeline.py :: XLSPipeline.run(..., ppa_mode=...)`
  Gates the `benchmark_main` subprocess on the same condition.
- `alphaevolve/evaluator.py :: Evaluator.__init__(..., ppa_mode="fast")`
  Stores the mode and threads it into both `builder.build(...)` and `pipeline.run(...)`.
- `run.py :: parse_args()`
  Declares `--ppa_mode {fast,medium,slow,slowest}` and validates the resolved value.

Placeholder modes emit a warning to stderr and then evaluate as if `fast`, so the loop keeps making progress until Yosys/OpenROAD integration lands.

---

## Part 6 — Scoring

```
Score = num_stages        * stage_weight
      + pipeline_flops    * power_weight
      + area_um2          * area_weight
      + critical_path_ps  * delay_weight
```

Lower is better. Defaults from `configs/evolve_config.yaml`:

| Weight         | Default | Notes                                                                                 |
| -------------- | ------- | ------------------------------------------------------------------------------------- |
| `stage_weight` | 200     | Pipeline depth is the primary signal — stages are cheap in `fast`, always meaningful. |
| `power_weight` | 0       | Pipeline flop bits as a proxy for switching-power cost. Off by default.               |
| `reg_weight`   | 1       | Deprecated alias for `power_weight`. Still read for backward compatibility.           |
| `area_weight`  | 1       | Only meaningful with `ppa_mode=slow` (area comes from asap7). Otherwise area=0.       |
| `delay_weight` | 1       | Critical-path picoseconds.                                                            |

Aggregation across multiple designs (see `evaluator._run_pipeline_on_designs`):

- `total_stages`, `total_flops`, `total_area` are **summed** across designs.
- `max_delay` and `max_min_clock` are the **maximum** across designs.

---

## Part 7 — Per-run output layout

```
results/<timestamp>/
├── candidates_db.sqlite        Every candidate: code, score, diff, metrics, status.
├── evolution_log.csv           Flat CSV of every candidate (for plotting).
├── ppa_report.json             Final summary with the top 3 candidates.
├── best_algorithm.patch        Unified diff against the baseline scheduler.
└── eval_runs/iter<NNNN>_island<K>/
    └── <design>/
        ├── <design>.ir                    after ir_converter_main
        ├── <design>_opt.ir                after opt_main
        ├── <design>_block_metrics.textproto   codegen_main (always)
        ├── <design>_schedule.textproto        codegen_main (always)
        ├── <design>.v                         codegen_main (when codegen succeeds)
        └── <design>_benchmark.txt             benchmark_main (slow modes only)
```

---

## Part 8 — Data-flow summary

```
INPUTS                         EVOLUTION ENGINE                   OUTPUTS
──────                         ────────────────                   ───────

designs/*.x           ────►┐
                            │
configs/                    │   For each iteration:
  evolve_config.yaml ─────►│
  ppa_constraints.yaml ───►│   1. sampler.py  → AI → new C++ body
                            │        (reference sources + variants +
knowledge/**/*.md  ───────►│         previous compile_error on retry)
                            │                    │
xls/ (source)    ────────►│                    ▼
  xls/scheduling/           │   2. evaluator.py  → splice → Bazel
    agent_generated_         │           rebuild agent + codegen +
    scheduler.cc ───────────►│           opt + ir_converter
    (the ONLY file mutated)  │           (+ benchmark_main if slow)
                            │                    │
                            │                    ▼
                            │   3. pipeline.py  DSLX → IR → opt →
                            │           codegen (+ benchmark_main)
                            │                    │
                            │                    ▼
                            │   4. ppa_metrics.py  → score
                            │                    │
                            │                    ▼
                            │   5. db.insert, island.add, migrate
                            │                    │
                            │                    ▼
                            │   6. restore baseline; next iteration
                            └────────────────────┘
                                    │
                                    ▼
                         results/<timestamp>/
                           best_algorithm.patch   ◄── apply to XLS
                           ppa_report.json              for permanent adoption
                           evolution_log.csv
                           candidates_db.sqlite
                           eval_runs/iter*_island*/
```

---

## Part 9 — What is intentionally untouched

Per `plan/plan.txt`:

- `xls/scheduling/sdc_scheduler.cc` — XLS still uses it for `--scheduling_strategy=sdc`; we just stop invoking that strategy.
- `xls/scheduling/min_cut_scheduler.cc` — same, referenced only as prompt context.
- `xls/scheduling/run_pipeline_schedule.cc` — already dispatches `AGENT` correctly.
- `xls/scheduling/scheduling_options.h` — defines the `SchedulingStrategy::AGENT` enum.
- `xls/scheduling/BUILD` — `agent_generated_scheduler` is already a dep of `run_pipeline_schedule`.
- `alphaevolve/database.py` and `alphaevolve/ppa_metrics.py` — schema and parsing unchanged.
- `alphaevolve/sampler.py` — only the prompt template body changed.

Everything in `alphaevolve-xls/` that references the old SDC-objective / delay-constraints / min-cut mutation targets has been removed. The only remaining mention of `sdc` or `min_cut` in the repo is as reference source material in the prompt bundle (the AI reads those files to learn the XLS scheduling APIs), and as comments noting "we no longer call this path".

---

## Part 10 — Recent architectural changes (2026-04 refactor)

The previous architecture mutated `SDCSchedulingModel::SetObjective()` / `ComputeCombinationalDelayConstraints()` / `MinCutScheduler::Schedule()` inside XLS proper. That made every iteration expensive (SDC LP is entangled with ortools) and made compile failures very frequent because the AI had to respect a lot of XLS-internal invariants.

The refactor, captured in `plan/plan.txt`, collapses all three mutation targets into one standalone file that XLS explicitly opts into via `--scheduling_strategy=agent`. This has three consequences:

1. **Incremental builds are much cheaper.** The agent file is small, has few reverse-deps, and does not link against ortools, so the per-iteration rebuild is limited to the scheduler plus the thin set of tools that include it.
2. **The AI contract is tractable.** The function has a fixed signature, five named helper functions, and a small set of `ScheduleBounds` APIs to work with. Compile failures are easier for the AI to fix because the scope is narrow.
3. **We can skip `benchmark_main` in the common case.** `codegen_main` already emits a `block_metrics` textproto with stages and register bits, which is enough signal for fast-mode evolution. `benchmark_main` (which pulls in LLVM/JIT and rebuilds slowly) is only rebuilt when the user asks for `--ppa_mode slow` to get asap7 area.

All of the Python code paths that used to branch on `mutation_type == "sdc_objective"` vs `"delay_constraints"` vs `"min_cut"` have been collapsed: `MUTATION_TARGETS` now has exactly one entry, `islands.py` has exactly one variant list, and the prompt templates describe exactly one scheduler contract.
