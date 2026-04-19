# AlphaEvolve-XLS — System Architecture

> Last updated: 2026-04-18 — reflects agent-scheduler-only design, `--clock_period` CLI flag, optional baseline benchmark context, and `--ppa_mode` behavior.

This document is the reference for how AlphaEvolve-XLS is wired together internally. `README.md` is the user-facing guide; this file covers the internals.

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

XLS already dispatches this function when `--scheduling_strategy=agent` is passed to any XLS tool (`codegen_main`, `benchmark_main`). The dispatch lives in `xls/scheduling/run_pipeline_schedule.cc :: RunPipelineSchedule()`:

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

### Dry-run stub

`agent_generated_scheduler.cc` contains a fast ASAP stub at the top of the function, gated by the `XLS_AGENT_DRY_RUN` environment variable. When `--dry_run` is passed to `run.py`, this variable is set so the pipeline can be validated end-to-end without invoking the AI. The stub is surrounded by prominent banners instructing the AI to ignore it; the splicer replaces everything from the function signature onward, so the stub never appears in AI-generated candidates.

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
│   └── ppa_constraints.yaml        Hardware target: delay_model, generator (pipeline),
│                                   optional per-design pipeline_stages overrides.
│                                   clock_period_ps is NOT stored here — it comes
│                                   exclusively from the --clock_period CLI flag.
│
├── designs/
│   ├── mac/mac.x                   32-bit multiply-accumulate (proc)
│   ├── fir_filter/fir.x            Simple FIR filter
│   ├── fir32/fir32.x               32-tap FIR filter
│   ├── dot_product/dot.x           8-element dot product
│   ├── gemm4x4_int/gemm4x4_int.x  4×4 integer matrix multiply
│   ├── idct/idct.x                 2D IDCT (Chen algorithm)
│   ├── sha256/sha256.x             SHA-256 hash (64-round feedback structure)
│   ├── bitonic_sort/bitonic_sort.x Bitonic sort network
│   ├── crc32/crc32.x               CRC-32 checksum
│   └── matmul4x4/matmul_4x4.x     4×4 float32 FMA systolic array (proc)
│   (each folder may also hold <stem>_benchmark.txt — optional baseline AI context)
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
│   │                                 1. benchmark_main stdout (ppa_mode=slow)
│   │                                 2. block_metrics textproto (codegen, always)
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
│       ├── implement.txt           Fallback Jinja2 template.
│       └── implement_agent_scheduler.txt  Active template used by sampler.py.
│
├── xls_tools/
│   ├── build.py                    Bazel wrapper. Iteration targets:
│   │                                 //xls/scheduling:agent_generated_scheduler
│   │                                 //xls/tools:codegen_main
│   │                                 //xls/tools:opt_main
│   │                                 //xls/dslx/ir_convert:ir_converter_main
│   │                                 //xls/dev_tools:benchmark_main  (slow only)
│   │                               `supports_agent_strategy(tool)` probes whether
│   │                               the existing binary already supports agent mode,
│   │                               skipping the rebuild when possible.
│   │                               `iteration_targets_for_mode(mode)` returns the
│   │                               correct target list per ppa_mode.
│   └── pipeline.py                 DSLX → IR → opt → schedule → Verilog.
│                                   Always uses --scheduling_strategy=agent.
│                                   `run(..., ppa_mode=...)`:
│                                     fast|medium: block_metrics only; codegen_main
│                                       failure is a hard fail (no PPA) for function
│                                       designs. Proc designs fail codegen; use slow.
│                                     slow|slowest: also runs benchmark_main;
│                                       codegen failure is non-fatal when benchmark_main
│                                       produced metrics (handles proc networks).
│
├── knowledge/
│   ├── papers/*.md                 Scheduling-theory summaries. Loaded into prompts.
│   └── heuristics/*.md             ASAP/ALAP/mobility references.
│
├── scripts/                        Ad-hoc validation helpers.
│
└── results/<timestamp>/            Per-run artefacts (see Part 7).
```

---

## Part 3 — The evolution loop

```
python run.py \
    --input_file designs/gemm4x4_int/gemm4x4_int.x \
    --clock_period 1000 \
    --iterations 20 \
    --ppa_mode fast \
    --backend codex
       │
       ▼
┌────────────────────────────────────────────────────────────────────────┐
│ run.py — startup                                                       │
│   1. Load ppa_constraints.yaml + evolve_config.yaml.                   │
│   2. Apply CLI overrides (--mutation_target, --backend, --model,       │
│      --ppa_mode, --num_islands, --island_id).                          │
│   3. Inject clock_period_ps from --clock_period into ppa_cfg.          │
│      (The YAML does not store clock_period; CLI is the single source.) │
│   4. Validate ppa_mode ∈ {fast, medium, slow, slowest}.                │
│      medium/slowest emit a placeholder warning.                        │
│   5. Construct XLSBuilder + XLSPipeline + Sampler + Evaluator          │
│      (ppa_mode is passed into Evaluator) + IslandManager + CandidateDB.│
│   6. Probe existing binaries with supports_agent_strategy():           │
│      if agent mode is missing → incremental Bazel rebuild.             │
│   7. If ppa_mode in (slow, slowest): build benchmark_main at startup.  │
│   8. For each design folder, check for <stem>_benchmark.txt.           │
│      If found, load its contents as optional baseline context for the  │
│      AI prompt. Islands start empty (cold); no seeding is performed.  │
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
│   else return None (cold start — first iteration of a fresh run).      │
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
│   All variants name real XLS APIs only.                                │
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
│        baseline_benchmark_context (optional, from <stem>_benchmark.txt)│
│        compile_error (None on first attempt).                          │
│   e) Backend:                                                          │
│        codex  → codex exec -m <model> --sandbox read-only              │
│                 --skip-git-repo-check --ephemeral -o /tmp/out.cpp -    │
│                 (prompt on stdin, response in file)                    │
│        openai → chat.completions.create(model=evo.ai_model,            │
│                 messages=[system, user], max_tokens=evo.max_tokens)    │
│   f) Strip markdown fences → return raw C++ string.                   │
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
│       builder.apply(target_file, new_source)                           │
│                                                                        │
│   5c. builder.build(                                                   │
│           XLSBuilder.iteration_targets_for_mode(self.ppa_mode))        │
│       fast|medium  → agent_generated_scheduler + codegen_main          │
│                      + opt_main + ir_converter_main                    │
│       slow|slowest → above + benchmark_main                            │
│                                                                        │
│   5d. If build fails (C++ compile error):                              │
│       - restore the .bak                                               │
│       - run.py trims compiler output to the first ~30 error/note lines │
│         and feeds it back as `compile_error` for a retry               │
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
│     [optional, slow modes]                                             │
│     IR  → benchmark_main (benchmark_main --scheduling_strategy=agent)  │
│              Non-zero exit is tolerated when the Pipeline: section     │
│              is present — proc-network designs may fail at the         │
│              internal block-codegen lowering step after scheduling      │
│              metrics have already been printed.                         │
│     IR  → codegen        (codegen_main --scheduling_strategy=agent     │
│                            --block_metrics_path=<...>.textproto)       │
│              In fast mode, codegen failure is a hard fail.             │
│              In slow mode, codegen failure is non-fatal when           │
│              benchmark_main already produced valid metrics.            │
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

## Part 3.5 — Compile failure and retry loop

This section explains exactly what happens when the AI generates C++ that does not compile, what gets fed back, and how the loop decides to retry or give up.

### The retry wrapper (run.py)

Each iteration is wrapped in a retry loop controlled by `max_build_retries` (default: 3, set in `evolve_config.yaml`):

```
for attempt in 1 .. max_build_retries:

    generated_code = sampler.sample(..., compile_error=last_compile_error)
    result         = evaluator.evaluate(..., generated_code=generated_code)

    if result.candidate.build_status == "success":
        break                     # compiled → exit retry loop

    last_compile_error = trim(result.candidate.notes)

# record whichever result came last (even if build_failed)
```

On attempt 1, `compile_error=None` — the prompt does not mention a previous failure.
On attempts 2 and 3, `compile_error` is the trimmed Clang output from the previous attempt.

### What gets trimmed and passed back

`build_result.stderr` contains the full Bazel + Clang output (often thousands of lines).
`run.py` filters it before handing it to the AI:

```python
error_lines = [l for l in stderr.splitlines() if "error:" in l or "note:" in l]
last_compile_error = "\n".join(error_lines[:30])
```

Only lines containing `error:` or `note:` are kept, capped at 30 lines. This strips
Bazel progress noise, INFO lines, and long include stacks, leaving the AI with the
smallest message that still identifies every error location and cause.

The trimmed text is injected into the prompt template under:

```
== PREVIOUS ATTEMPT FAILED TO COMPILE ==
Compiler output:
<trimmed error here>

Fix every compiler issue before writing the next version.
```

### How the AI uses the error

The prompt instructs the AI to fix the specific C++ issue first and only then consider changing algorithmic details. A typical error message looks like:

```
xls/scheduling/agent_generated_scheduler.cc:47:5: error: use of undeclared identifier 'NodeCost'
xls/scheduling/agent_generated_scheduler.cc:47:5: note: did you mean 'NodeBitCount'?
xls/scheduling/agent_generated_scheduler.cc:83:12: error: expected ';' after return statement
```

The AI sees the file, line, column, and a human-readable description — enough to pinpoint and fix each issue without seeing the surrounding XLS source again.

### Build status values and what they mean

| `build_status` | Meaning | Retried? |
|----------------|---------|----------|
| `success` | Bazel returned 0; C++ compiled and linked. | — (done) |
| `build_failed` | Bazel returned non-zero; Clang rejected the AI's C++. | **Yes** — error fed back to AI, up to `max_build_retries` attempts. |
| `run_failed` | Compiled fine, but no design produced valid PPA in the pipeline (clock period exceeded, proc design in fast mode, scheduler timeout, etc.). | **No** — this is a runtime outcome, not a code defect. The candidate is recorded and the loop moves to the next iteration. |

### What counts as a build failure vs a run failure

**`build_failed`** — the generated C++ itself is broken. Common causes:
- `#include` directives added by the AI that conflict with existing includes
- `namespace xls { }` wrapper re-nesting the code (already inside `namespace xls`)
- Calls to functions that don't exist in the scheduler API (`NodeCost`, `GetDelay`, etc.)
- Syntax errors, missing semicolons, unmatched braces
- Type mismatches (e.g. returning `int` where `absl::StatusOr<ScheduleCycleMap>` is required)

**`run_failed`** — the C++ compiled, but the schedule was not accepted or produced no PPA. Common causes:
- AI-generated scheduler returns a map where some node cycles violate `[lb, ub]` bounds
- Clock period constraint not satisfiable with the generated schedule
- `benchmark_main` or `codegen_main` timed out (AI scheduler too slow — e.g. O(n²) inner loop on a large design)
- Design is a proc network and `--ppa_mode fast` was used (proc designs need `--ppa_mode slow`)

`run_failed` is **not** fed back to the AI as an error. The candidate is stored in the database with `ppa_score=inf` so it ranks last in island selection, and the next iteration starts fresh with a new AI call.

### What happens if all retries are exhausted

After `max_build_retries` failed compile attempts, the loop records the last `build_failed` candidate (with the most recent error in `notes`) and moves on to the next iteration. The island population is updated (the failing candidate is added with `ppa_score=inf`), so the next iteration's parent selection will avoid it.

No exception is raised; the run continues normally.

### Restore safety

`evaluator.evaluate()` wraps the backup → apply → build → run sequence in a `try/finally` block. The `finally` always calls `builder.restore(target_file)` regardless of whether a `build_failed`, `run_failed`, `TimeoutExpired`, or any other exception occurred. This ensures `agent_generated_scheduler.cc` is always returned to its baseline state before the next iteration begins.

On startup, `run.py` also checks for a leftover `.bak` file (left by a previous run that was killed before the `finally` could execute) and auto-restores it with a warning.

---

## Part 4 — XLS pipeline, command level

### Stage 1: DSLX → IR

```bash
ir_converter_main \
    --dslx_stdlib_path=<xls_src>/xls/dslx/stdlib \
    --top=<auto-detected> \
    design.x
```

`_detect_dslx_top()` scans the DSLX source for the last non-test `proc`/`fn` and sets it as the package top so downstream tools do not need the flag again.

### Stage 2: IR → optimized IR

```bash
opt_main design.ir
```

Dead-code elimination, constant folding, bit-width narrowing, CSE. For proc-network designs this is often the slowest optimization step.

### Stage 3 (optional, slow modes): benchmark_main

```bash
benchmark_main design.opt.ir \
    --delay_model=unit \
    --area_model=asap7 \
    --scheduling_strategy=agent \
    --run_evaluators=false \
    --generator=pipeline \
    --clock_period_ps=<N>
```

Prints a human-readable report including `Critical path delay`, `Total area`, `Total pipeline flops`, and a per-stage pipeline breakdown. A nonzero exit code is tolerated when the `Pipeline:` section is present — proc-network designs may fail the register-reset lowering pass inside benchmark_main after they have already printed their scheduling stats.

In slow mode, if codegen fails but benchmark_main produced valid metrics, the result is still treated as `success` (with `verilog_path=None`). `extract_ppa()` handles this via its source-priority chain.

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

With `--scheduling_strategy=agent`, codegen_main calls `AgentGeneratedScheduler()` internally. Every PPA measurement reflects the mutated C++.

`design_block_metrics.textproto` contains an `XlsMetricsProto` with flop count, delay breakdown, and stage count. This is the primary PPA source in `--ppa_mode fast`.

> **Proc designs and codegen:** `--generator=pipeline` does not support proc networks. Proc designs (e.g. `mac.x`, `matmul_4x4.x`) will fail at this stage. In `--ppa_mode slow`, this failure is non-fatal when benchmark_main already ran successfully. In `--ppa_mode fast`, proc designs cannot be evaluated and will always produce `run_failed`.

---

## Part 5 — `--ppa_mode` matrix

| Mode      | Runs benchmark_main?               | Runs codegen_main? | Proc design support? | Score terms non-zero         | Status      |
| --------- | ---------------------------------- | ------------------ | -------------------- | ----------------------------- | ----------- |
| `fast`    | No                                 | Yes (required)     | **No**               | stages, pipeline_reg_bits     | Implemented |
| `medium`  | No (planned: Yosys synth+stat)     | Yes                | No                   | stages, reg_bits, gate_count  | Placeholder |
| `slow`    | Yes (built at startup, runs each iter) | Yes (non-fatal fail OK) | **Yes**     | stages, reg_bits, area, delay | Implemented |
| `slowest` | No (planned: Yosys+OpenROAD)       | Yes                | No                   | stages, reg_bits, silicon area, WNS | Placeholder |

Implementation references:

- `xls_tools/build.py :: XLSBuilder.iteration_targets_for_mode(mode)`
  Adds `//xls/dev_tools:benchmark_main` when `mode in ("slow", "slowest")`.
- `xls_tools/pipeline.py :: XLSPipeline.run(..., ppa_mode=...)`
  Gates the `benchmark_main` subprocess on the same condition, and treats codegen failure as non-fatal in slow mode when benchmark_main produced metrics.
- `alphaevolve/evaluator.py :: Evaluator.__init__(..., ppa_mode="fast")`
  Stores the mode and threads it into both `builder.build(...)` and `pipeline.run(...)`.
- `run.py :: parse_args()`
  Declares `--ppa_mode {fast,medium,slow,slowest}` and validates the resolved value.

Placeholder modes emit a warning to stderr and then evaluate as if `fast`, so the loop keeps making progress.

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
| `stage_weight` | 200     | Pipeline depth is the primary signal — always meaningful across all modes.            |
| `power_weight` | 0       | Pipeline flop bits as a proxy for switching-power cost. Off by default.               |
| `reg_weight`   | 1       | Deprecated alias for `power_weight`. Still read for backward compatibility.           |
| `area_weight`  | 1       | Only meaningful with `ppa_mode=slow` (area comes from asap7). Otherwise area=0.       |
| `delay_weight` | 1       | Critical-path picoseconds.                                                            |

Aggregation across multiple designs (`evaluator._run_pipeline_on_designs`):

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
        ├── <design>_block_metrics.textproto   codegen_main (when it succeeds)
        ├── <design>_schedule.textproto        codegen_main (when it succeeds)
        ├── <design>.v                         codegen_main (when it succeeds)
        └── <design>_benchmark.txt             benchmark_main (slow modes); or
                                               "benchmark_main skipped" in fast mode
```

---

## Part 8 — Data-flow summary

```
INPUTS                         EVOLUTION ENGINE                   OUTPUTS
──────                         ────────────────                   ───────

designs/*.x           ────►┐
designs/*_benchmark.txt ──►│   (optional baseline AI context)
                            │
configs/                    │   For each iteration:
  evolve_config.yaml ─────►│
  ppa_constraints.yaml ───►│   1. sampler.py  → AI → new C++ body
                            │        (reference sources + variants +
knowledge/**/*.md  ───────►│         baseline context + compile_error on retry)
                            │                    │
xls/ (source)    ────────►│                    ▼
  xls/scheduling/           │   2. evaluator.py  → splice → Bazel
    agent_generated_         │           rebuild agent + codegen +
    scheduler.cc ───────────►│           opt + ir_converter
    (the ONLY file mutated)  │           (+ benchmark_main if slow)
                            │                    │
                            │                    ▼
                            │   3. pipeline.py  DSLX → IR → opt →
                            │           [benchmark_main if slow] →
                            │           codegen (non-fatal fail in slow
                            │           when benchmark succeeded)
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

By design, the following XLS files are never modified by the evolution loop:

- `xls/scheduling/sdc_scheduler.cc` — XLS still uses it for `--scheduling_strategy=sdc`; we just stop invoking that strategy.
- `xls/scheduling/min_cut_scheduler.cc` — same; referenced only as prompt context so the AI can learn XLS scheduling APIs.
- `xls/scheduling/run_pipeline_schedule.cc` — already dispatches `AGENT` correctly.
- `xls/scheduling/scheduling_options.h` — defines the `SchedulingStrategy::AGENT` enum.
- `xls/scheduling/BUILD` — `agent_generated_scheduler` is already a dep of `run_pipeline_schedule`.
- `alphaevolve/database.py` and `alphaevolve/ppa_metrics.py` — schema and parsing are stable.
- `alphaevolve/sampler.py` — only the prompt template body is changed when tuning the AI.

The XLS source tree is always restored to its original state after each iteration. No iteration leaves the tree in a mutated state.

---

## Part 10 — Architectural decisions

### Why a standalone scheduler file?

Earlier approaches mutated `SDCSchedulingModel::SetObjective()`, `ComputeCombinationalDelayConstraints()`, and `MinCutScheduler::Schedule()` inside XLS proper. That made every iteration expensive (the SDC LP solver is entangled with ortools) and caused frequent compile failures because the AI had to respect many XLS-internal invariants across multiple files.

The current design collapses all mutation targets into one standalone file that XLS explicitly opts into via `--scheduling_strategy=agent`. Three consequences:

1. **Incremental builds are much cheaper.** The agent file is small, has few reverse-deps, and does not link against ortools. Per-iteration relink is limited to the scheduler + the thin set of tools that include it.
2. **The AI contract is tractable.** The function has a fixed signature, five named helper functions, and a small set of `ScheduleBounds` APIs. Compile failures are easier for the AI to fix because the scope is narrow.
3. **We can skip `benchmark_main` in the common case.** `codegen_main` already emits a `block_metrics` textproto with stages and register bits — enough signal for fast-mode evolution. `benchmark_main` (LLVM/JIT, slow build) is only invoked when the user asks for `--ppa_mode slow`.

### Why `--clock_period` is a CLI flag, not a YAML value

The clock period is a fixed hardware constraint for the entire run. Keeping it exclusively in the CLI argument prevents any YAML edit, config override, or AI-generated code from silently changing the timing target mid-run. The YAML files (`ppa_constraints.yaml`, `evolve_config.yaml`) hold only tuning knobs — not the constraints themselves.

### Why islands start cold (no pre-seeding)

Seeding islands with the baseline scheduler's PPA would require running the full XLS pipeline before the evolution begins — adding latency and coupling the startup to the current binary state. Instead, islands start empty and the first iteration's parent is `None`. The cold-start iteration generates a scheduler from scratch (guided by the AI prompt, reference sources, and any baseline context files). This is cheaper and avoids a class of startup errors where the baseline binary does not yet support `--scheduling_strategy=agent`.

### Why proc designs need `--ppa_mode slow`

XLS's `codegen_main --generator=pipeline` does not support proc networks (proc designs require `--generator=block` plus reset/state handling, which the pipeline generator does not provide). In `--ppa_mode fast`, `benchmark_main` is not run, so proc designs have no fallback PPA source when `codegen_main` fails. In `--ppa_mode slow`, `benchmark_main` runs the scheduler standalone and prints timing metrics before any codegen step, so its output is available even when `codegen_main` fails — allowing proc designs to be scored correctly.

### AI output sanitization

The AI is instructed not to emit `#include` directives or wrap its output in `namespace xls {}`. These rules are stated prominently in the prompt because violating them causes compile failures: the output is spliced mid-file inside an existing `namespace xls` block, so any re-wrapping or extra includes result in redefinition errors. Compile errors are captured and fed back to the AI for a retry (up to `max_build_retries=3` attempts per iteration).
