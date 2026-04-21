# AlphaEvolve-XLS ??System Architecture

> Last updated: 2026-04-19 ??reflects agent-scheduler-only design, `--clock_period` CLI flag, optional baseline benchmark context, `--ppa_mode` behavior, `--benchmark_timeout` flag, runtime scoring, per-stage console callbacks, slow-mode build-target optimisation, and `balance_cv_norm` stage-load distribution metric.

This document is the reference for how AlphaEvolve-XLS is wired together internally. `README.md` is the user-facing guide; this file covers the internals.

---

## Part 1 ??What gets evolved

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
if      strategy == MIN_CUT   ??MinCutScheduler::Schedule()
else if strategy == AGENT     ??AgentGeneratedScheduler()   ??we mutate this
else if strategy == RANDOM    ??random walk
else                          ??SDCSchedulingModel / ASAP
```

We do **not** modify any of the other schedulers, the dispatch code, the `scheduling_options.h` enum, or the XLS BUILD files. They are already correct; we just stop calling them.

The XLS BUILD dependency `//xls/scheduling:agent_generated_scheduler` is already a dep of `run_pipeline_schedule`, so whenever `agent_generated_scheduler.cc` changes, everything downstream (codegen_main, benchmark_main) gets a partial relink.

### The scheduler contract (what the AI must implement)

1. Call `bounds->PropagateBounds()` **once** before the main loop ??O(n), initialises lb/ub windows.
2. Walk nodes via `TopoSort(f)`.
3. Skip any node where `IsUntimed(node)` is true.
4. For each timed node, compute `lb = max(bounds->lb(node), max assigned_cycle of timed operands)`.
5. Read `ub = bounds->ub(node)`. If `pipeline_stages > 0`, cap `ub = min(ub, pipeline_stages ??1)`.
6. Choose a cycle in `[lb, ub]` using a principled heuristic.
7. Record the choice in a `ScheduleCycleMap` (`absl::flat_hash_map<Node*, int64_t>`).
8. Pin it with `bounds->TightenNodeLb(node, cycle)` and `bounds->TightenNodeUb(node, cycle)` ??O(1) each.
9. **Never** call `bounds->PropagateBounds()` inside the per-node loop ??O(n) per call ? ~4,919 nodes (SHA-256) = O(n簡), reliably hits the 30-minute timeout.
10. Return the completed map.

Helper functions available to reuse: `NodeBitCount`, `NodeFanout`. The AI may add its own helpers inside a fresh anonymous namespace block above the target function.

### Dry-run stub

`agent_generated_scheduler.cc` contains a fast ASAP stub at the top of the function, gated by the `XLS_AGENT_DRY_RUN` environment variable. When `--dry_run` is passed to `run.py`, this variable is set so the pipeline can be validated end-to-end without invoking the AI. The stub is surrounded by prominent banners instructing the AI to ignore it; the splicer replaces everything from the function signature onward, so the stub never appears in AI-generated candidates.

---

## Part 1A ??Inside `agent_generated_scheduler.cc`

This section answers the practical questions: *What does the file look like? What is the AI actually replacing? Does the agent rewrite the whole file or just one function? How do its helper functions get incorporated?*

---

### File anatomy

The file always has this five-zone structure:

```
????????????????????????????????????????????????????????????????????????? Zone 1  Apache 2.0 license header + 13 #include directives        ????         Static. Evolution never touches these lines.               ?????????????????????????????????????????????????????????????????????????       ??       ??????????????????????????????????????????????????????????????????????????? Zone 2  namespace xls {                                            ????         One line. Static.                                          ?????????????????????????????????????????????????????????????????????????       ??       ??????????????????????????????????????????????????????????????????????????? Zone 3  Helper accumulation zone (grows over iterations)           ????                                                                   ???? One or more  namespace { ... }  // namespace  blocks.             ???? Always includes at least:                                         ????   int64_t NodeBitCount(Node* node)  ??flat bit-width of a node    ????   int64_t NodeFanout(Node* node)    ??downstream user count       ???? The AI may add more helpers here in each iteration.               ???? Old helper blocks from prior iterations accumulate and remain.    ?????????????????????????????????????????????????????????????????????????       ??       ??????????????????????????????????????????????????????????????????????????? Zone 4  THE ONLY ZONE THE AI REPLACES                              ????                                                                   ???? absl::StatusOr<ScheduleCycleMap> AgentGeneratedScheduler(          ????     FunctionBase* f, int64_t pipeline_stages,                     ????     int64_t clock_period_ps,                                      ????     const DelayEstimator& delay_estimator,                        ????     sched::ScheduleBounds* bounds,                                ????     absl::Span<const SchedulingConstraint> constraints) {         ????   // ... current implementation ...                               ???? }                                                                  ????                                                                   ???? _splice_function() replaces from the first character of the       ???? function signature all the way to the matching closing `}`.       ???? Everything else in the file is left untouched.                    ?????????????????????????????????????????????????????????????????????????       ??       ??????????????????????????????????????????????????????????????????????????? Zone 5  }  // namespace xls                                        ????         One line. Static.                                          ?????????????????????????????????????????????????????????????????????????```

---

### The baseline implementation

The initial `AgentGeneratedScheduler()` body has two sections:

**Section A ??Dry-run stub (gated by `XLS_AGENT_DRY_RUN`)**

```cpp
if (std::getenv("XLS_AGENT_DRY_RUN") != nullptr) {
    XLS_RETURN_IF_ERROR(bounds->PropagateBounds());
    ScheduleCycleMap stub_map;
    for (Node* node : TopoSort(f)) {
        if (!IsUntimed(node)) stub_map[node] = bounds->lb(node);
    }
    return stub_map;
}
```

This is a pure ASAP scheduler ??every node is placed at its earliest legal cycle (`bounds->lb(node)`). It is only active when `--dry_run` is passed to `run.py`, which sets `XLS_AGENT_DRY_RUN` in the child process environment. The purpose is pipeline validation: you can run the entire ir_convert ??opt ??codegen ??Verilog chain without spending AI tokens or waiting for a build.

Because `_splice_function` replaces the *entire* function body (including this guard), the AI's output replaces the stub on every iteration. The stub is restored from backup after each evaluation (see `builder.restore()`), so it is always present in the on-disk file between iterations. It is absent only inside an active evaluation cycle.

**Section B ??The heuristic (the thing being evolved)**

Below the dry-run guard is the live scheduler. In the initial baseline this is a simple ASAP greedy pass. Evolution replaces it with progressively more sophisticated scored list schedulers. The current evolved body (in `xls_patch/files/xls/scheduling/agent_generated_scheduler.cc`) uses a multi-factor cost function that weighs:
- timing overflow (quadratic penalty for exceeding `clock_period_ps`)
- register pressure (weighted bit-width of values that must cross a pipeline boundary)
- stage load (node count per stage ??lower variance is better)
- criticality (fanout ? bit-width / scheduling mobility)

---

### How `_splice_function` works (step by step)

```python
# evaluator.py ??_splice_function(source, signature, new_body)
#   source    = the full .cc file read from disk
#   signature = "absl::StatusOr<ScheduleCycleMap> AgentGeneratedScheduler("
#   new_body  = sanitized AI output (new helpers + function, no #include, no namespace xls)

1.  Regex-find `signature` in `source`.
2.  From the match start, scan forward to find the opening `{` of the function body.
3.  Brace-count forward until depth returns to 0  ?? brace_end  (the closing `}`).
4.  Reconstruct:
        before = source[ : match.start() ]      # Zone 1 + Zone 2 + Zone 3 (all helpers so far)
        after  = source[ brace_end + 1 : ]      # "\n\n...\n}  // namespace xls"  (Zone 5)
        result = before + new_body + "\n" + after
```

`before` contains Zones 1-3 entirely intact ??license header, all 13 `#include` directives, `namespace xls {`, and every helper block accumulated from prior iterations.
`after` contains Zone 5 ??the trailing `} // namespace xls`.

The AI output (`new_body`) lands in the gap between them.

---

### What the AI must produce

The AI is given the current `.cc` file as context and must return a **drop-in replacement for Zone 4** ??optionally with new Zone-3 helpers prepended:

```cpp
// Optional: new anonymous namespace block with helper functions
namespace {
int64_t MyHelper(Node* node, ...) { ... }
// ...
}  // namespace

// Required: the target function, verbatim signature
absl::StatusOr<ScheduleCycleMap> AgentGeneratedScheduler(
    FunctionBase* f, int64_t pipeline_stages, int64_t clock_period_ps,
    const DelayEstimator& delay_estimator, sched::ScheduleBounds* bounds,
    absl::Span<const SchedulingConstraint> constraints) {
  // ... new implementation body ...
}
```

**`_sanitize_generated_code` enforces two rules before splicing:**

| Rule | What it strips | Why |
|------|----------------|-----|
| No `#include` lines | All `#include ...` lines are removed | Zone 1 already has all necessary headers. Extra includes inside `namespace xls {}` are a compile error. |
| No `namespace xls` wrapper | Leading `namespace xls { ... }` block is unwrapped | Zone 2 already opens `namespace xls`. Re-wrapping creates a nested redefinition. |

If either stripping operation fires, a warning is logged and the sanitization note is recorded in the candidate database.

---

### Helper accumulation across iterations

When the AI adds new helpers, they are prepended to `new_body`. After splicing:

```
... Zone 3 (all helpers from iterations 0?吉-1) ...   ??part of "before", unchanged
namespace {                                            ??  // New helpers from iteration N                     ??start of new_body
}  // namespace                                        ??absl::StatusOr<ScheduleCycleMap> AgentGeneratedScheduler(...) {  ??rest of new_body
  // New implementation
}
```

Old helpers accumulate silently. They live in anonymous namespaces so there are no ODR (One Definition Rule) violations. Unused symbols are discarded by the linker. The growing "graveyard" of prior helper functions is visible to the AI in the current-source context; the prompt treats it as a form of evolutionary memory ??the AI can see which helper patterns were tried before.

---

### Quick reference

| Zone | Content | Changed by AI? |
|------|---------|----------------|
| License + `#include` | 13 XLS headers | ??Never |
| `namespace xls {` | Opening brace | ??Never |
| Helper namespaces | Accumulated `namespace {}` blocks | Grows (never shrinks) |
| `AgentGeneratedScheduler()` | Full function: signature + body | **??Replaced every iteration** |
| `}  // namespace xls` | Closing brace | ??Never |

For the complete prompt the AI receives (system prompt, Jinja2 template, mutation instructions, injected variables), see **Part 3.2**.

---

## Part 2 ??File map

```
alphaevolve-xls/
????? run.py                          MAIN ENTRY POINT. Parses CLI, resolves ppa_mode,
??                                  builds the components, runs the evolution loop.
????? configs/
??  ??? evolve_config.yaml          Hyperparameters: num islands, AI backend,
??  ??                              score weights, ppa_mode (default "fast"),
??  ??                              mutation_types=[agent_scheduler].
??  ??? ppa_constraints.yaml        Hardware target: delay_model, generator (pipeline),
??                                  optional per-design pipeline_stages overrides.
??                                  clock_period_ps is NOT stored here ??it comes
??                                  exclusively from the --clock_period CLI flag.
????? designs/
??  ??? mac/mac.x                   32-bit multiply-accumulate (proc)
??  ??? fir_filter/fir.x            Simple FIR filter
??  ??? fir32/fir32.x               32-tap FIR filter
??  ??? dot_product/dot.x           8-element dot product
??  ??? gemm4x4_int/gemm4x4_int.x  4?4 integer matrix multiply
??  ??? idct/idct.x                 2D IDCT (Chen algorithm)
??  ??? sha256/sha256.x             SHA-256 hash (64-round feedback structure)
??  ??? bitonic_sort/bitonic_sort.x Bitonic sort network
??  ??? crc32/crc32.x               CRC-32 checksum
??  ??? matmul4x4/matmul_4x4.x     4?4 float32 FMA systolic array (proc)
??  (each folder may also hold <stem>_benchmark.txt ??optional baseline AI context)
????? alphaevolve/
??  ??? sampler.py                  AI interface. Builds the Jinja2 prompt, calls
??  ??                              `codex exec` via subprocess OR the OpenAI SDK,
??  ??                              strips markdown fences, returns C++ string.
??  ??? evaluator.py                Splice ??build ??run ??extract PPA ??restore.
??  ??                              MUTATION_TARGETS dict has ONE entry:
??  ??                                "agent_scheduler": (
??  ??                                  "xls/scheduling/agent_generated_scheduler.cc",
??  ??                                  "absl::StatusOr<ScheduleCycleMap>"
??  ??                                  " AgentGeneratedScheduler(")
??  ??                              `_splice_function` uses signature + brace-counting
??  ??                              to replace the whole function body atomically.
??  ??                              Accepts ppa_mode via constructor.
??  ??? ppa_metrics.py              Parses metrics. Source priority:
??  ??                                1. benchmark_main stdout (ppa_mode=slow)
??  ??                                2. block_metrics textproto (codegen, always)
??  ??                                3. schedule textproto (tertiary)
??  ??                                4. Verilog regex (last resort)
??  ??                              Score (all terms normalized to [0,1]):
??  ??                                (stages/ref_stages)          * stage_weight
??  ??                              + (flops/ref_flop_bits)        * flop_weight
??  ??                              + (area_um2/ref_area)          * area_weight
??  ??                              + (max_stage_delay/ref_clock)  * delay_weight
??  ??                              + balance_cv_norm              * balance_weight
??  ??                              + (runtime_s/ref_timeout)      * runtime_weight
??  ??? database.py                 SQLite. Stores candidate, metrics, diff, status.
??  ??? islands.py                  Island population manager. Only mutation_type
??  ??                              in use is "agent_scheduler"; each island picks
??  ??                              one of 4 instruction variants per iteration
??  ??                              (register-pressure / ASAP / mobility / lookahead).
??  ??? prompts/
??      ??? system.txt              AI persona: "expert in HLS scheduling".
??      ??? implement.txt           Fallback Jinja2 template.
??      ??? implement_agent_scheduler.txt  Active template used by sampler.py.
????? xls_tools/
??  ??? build.py                    Bazel wrapper. Iteration targets per mode:
??  ??                                fast|medium ??agent_generated_scheduler
??  ??                                              + codegen_main + opt_main
??  ??                                              + ir_converter_main
??  ??                                slow|slowest ??agent_generated_scheduler
??  ??                                              + benchmark_main ONLY
??  ??                                              (codegen_main is NOT rebuilt
??  ??                                              per iteration in slow mode ????  ??                                              it was built once at startup)
??  ??                              `supports_agent_strategy(tool)` probes whether
??  ??                              the existing binary already supports agent mode,
??  ??                              skipping the rebuild when possible.
??  ??                              `iteration_targets_for_mode(mode)` returns the
??  ??                              correct target list per ppa_mode.
??  ??                              Subprocess calls use encoding="utf-8",
??  ??                              errors="replace" to survive Bazel's non-UTF-8
??  ??                              terminal progress bars (0x80 bytes).
??  ??? pipeline.py                 DSLX ??IR ??opt ??schedule ??Verilog.
??                                  Always uses --scheduling_strategy=agent.
??                                  `__init__` accepts `benchmark_timeout` (int,
??                                  default 1800 s) ??kills benchmark_main after
??                                  this many seconds; timeout records runtime_s=3600.
??                                  `run(..., ppa_mode=..., on_stage_start=..., on_stage=...)`:
??                                    on_stage_start(name, extra) ??called before each
??                                      XLS stage; used by run.py to update the spinner.
??                                    on_stage(name, status, duration_s, extra) ??called
??                                      after each stage completes; logged to stages.log.
??                                    fast|medium: block_metrics only; codegen_main
??                                      failure is a hard fail (no PPA) for function
??                                      designs. Proc designs fail codegen; use slow.
??                                    slow|slowest: also runs benchmark_main;
??                                      codegen failure is non-fatal when benchmark_main
??                                      produced metrics (handles proc networks).
??                                  benchmark_main stage shown as "AI scheduler" in the
??                                  console ??it runs the AI's C++ code, not just linking.
????? knowledge/
??  ??? papers/*.md                 Scheduling-theory summaries. Loaded into prompts.
??  ??? heuristics/*.md             ASAP/ALAP/mobility references.
????? scripts/                        Ad-hoc validation helpers.
????? results/<timestamp>/            Per-run artefacts (see Part 7).
```

---

## Part 3.1 ??The evolution loop

```
python run.py \
    --input_file designs/gemm4x4_int/gemm4x4_int.x \
    --clock_period 1000 \
    --iterations 20 \
    --ppa_mode fast \
    --backend codex
       ??       ???????????????????????????????????????????????????????????????????????????????run.py ??startup                                                       ????  1. Load ppa_constraints.yaml + evolve_config.yaml.                   ????  2. Apply CLI overrides (--mutation_target, --backend, --model,       ????     --ppa_mode, --num_islands, --island_id).                          ????  3. Inject clock_period_ps from --clock_period into ppa_cfg.          ????     (The YAML does not store clock_period; CLI is the single source.) ????  4. Validate ppa_mode ??{fast, medium, slow, slowest}.                ????     medium/slowest emit a placeholder warning.                        ????  5. Construct XLSBuilder + XLSPipeline + Sampler + Evaluator          ????     (ppa_mode is passed into Evaluator) + IslandManager + CandidateDB.????  6. Probe existing binaries with supports_agent_strategy():           ????     if agent mode is missing ??incremental Bazel rebuild.             ????  7. If ppa_mode in (slow, slowest): build benchmark_main at startup.  ????  8. For each design folder, check for <stem>_benchmark.txt.           ????     If found, load its contents as optional baseline context for the  ????     AI prompt. Islands start empty (cold); no seeding is performed.  ?????????????????????????????????????????????????????????????????????????????       ??       ?? for iteration in range(args.iterations):
       ???????????????????????????????????????????????????????????????????????????????STEP 1: Island selection                                               ????  islands.py ??IslandManager.select_island(iteration)                  ????  - --num_islands 1  ??always island 0                                 ????  - --island_id N    ??always island N                                 ????  - otherwise        ??round-robin by iteration                        ?????????????????????????????????????????????????????????????????????????????       ??       ???????????????????????????????????????????????????????????????????????????????STEP 2: Parent selection                                               ????  IslandManager.select_parent(island)                                  ????  Tournament (size 2) over island's successful candidates,             ????  else fall back to the global best in the DB,                         ????  else return None (cold start ??first iteration of a fresh run).      ?????????????????????????????????????????????????????????????????????????????       ??       ???????????????????????????????????????????????????????????????????????????????STEP 3: Mutation instruction                                           ????  IslandManager.mutation_instruction_for(island, iteration)            ????                                                                       ????  Iterations 0, 1, 2 ??bootstrap instructions (3 diverse families):   ????    0: DAG-DP ??forward pass, cost(v,c) = 峉 max(0,c?ssigned[u])?bw  ????    1: stage-load balancer ??equalize node count per stage             ????    2: two-pass critical-path-first ??zero-mobility ASAP, rest ALAP   ????                                                                       ????  Iterations 3+ ??rotating variants for "agent_scheduler":            ????    0: register-pressure-aware list scheduler                          ????    1: ASAP-first heuristic with delay-model tie-breaking              ????    2: mobility-driven greedy                                          ????    3: deterministic multistage heuristic with lookahead               ????                                                                       ????  All instructions append _RUNTIME_WARNING: complexity constraint      ????  reminding the AI about sha256's ~800 IR nodes, 30-min timeout,      ????  3600s penalty, and O(n?W) algorithm requirement.                     ????  All variants name real XLS APIs only.                                ?????????????????????????????????????????????????????????????????????????????       ??       ???????????????????????????????????????????????????????????????????????????????STEP 4: AI sampling (sampler.py)                                       ????                                                                       ????  a) Load knowledge base (knowledge/**/*.md).                          ????  b) Read current agent_generated_scheduler.cc.                        ????  c) Build a reference-source bundle:                                  ????       - current standalone scheduler                                  ????       - SDC scheduler (reference)                                     ????       - Min-cut scheduler (reference)                                 ????       - run_pipeline_schedule.cc (dispatch reference)                 ????       - scheduling_options.h (strategy enum)                          ????  d) Render implement_agent_scheduler.txt with:                        ????       mutation_target, target_file_path, required_signature,          ????       best_score / best_num_stages / best_reg_bits / best_delay_ps,   ????       parent_score / parent_num_stages / parent_reg_bits / ...,       ????       mutation_instruction, knowledge_context,                        ????       current_function_source, reference_source_bundle,               ????       baseline_benchmark_context (optional, from <stem>_benchmark.txt)????       compile_error (None on first attempt).                          ????  e) Backend:                                                          ????       codex  ??codex exec -m <model> --sandbox read-only              ????                --skip-git-repo-check --ephemeral -o /tmp/out.cpp -    ????                (prompt on stdin, response in file)                    ????       openai ??chat.completions.create(model=evo.ai_model,            ????                messages=[system, user], max_tokens=evo.max_tokens)    ????  f) Strip markdown fences ??return raw C++ string.                   ?????????????????????????????????????????????????????????????????????????????       ??       ???????????????????????????????????????????????????????????????????????????????STEP 5: Evaluate (evaluator.py) ??the critical path                    ????                                                                       ????  5a. _splice_function(source, signature, new_body)                    ????      - Regex-locate the signature                                     ????      - Brace-count forward to find the matching `}`                   ????      - Replace the whole function (signature + body)                  ????                                                                       ????  5b. builder.backup(target_file)  (writes .bak)                       ????      builder.apply(target_file, new_source)                           ????                                                                       ????  5c. Build ??two stages in slow mode, one in fast:                    ????                                                                       ????      fast|medium ??one combined build step:                           ????        "compile:scheduler" target list =                              ????          agent_generated_scheduler + codegen_main                     ????          + opt_main + ir_converter_main                               ????                                                                       ????      slow|slowest ??two sequential build steps, each timed:          ????        "compile:scheduler" = agent_generated_scheduler only           ????        "compile:benchmark_main" = benchmark_main (relinks with        ????          the new scheduler object)                                    ????        on_stage_start/on_stage callbacks fired for each step so the   ????        console shows which compile is running and how long it took.   ????                                                                       ????  5d. If build fails (C++ compile error):                              ????      - restore the .bak                                               ????      - run.py trims compiler output to up to 60 lines, keeping only  ????        lines containing "error:", "warning:", "note:", or "^ "        ????        plus one line of context around each match                     ????      - trimmed error fed back as `compile_error` for a retry          ????        (default max_build_retries=3)                                  ????      - each attempt written to iter<N>_island<K>_attempts.log         ????                                                                       ????  5e. If build succeeds but pipeline produces no valid PPA             ????      (run_failed): break retry loop immediately. run_failed is a      ????      runtime outcome, not a code defect ??do NOT feed it to the AI.  ????                                                                       ????  5f. On success: run XLS pipeline on each benchmark design.           ????                                                                       ????  5g. Restore the backup and cleanup.                                  ?????????????????????????????????????????????????????????????????????????????       ??       ???????????????????????????????????????????????????????????????????????????????STEP 6: XLS pipeline (pipeline.py)                                     ????  For each design:                                                     ????    DSLX ??IR           (ir_converter_main, --dslx_stdlib_path set)    ????    IR  ??optimized IR  (opt_main)                                     ????    [optional, slow modes]                                             ????    IR  ??benchmark_main (benchmark_main --scheduling_strategy=agent)  ????             Non-zero exit is tolerated when the Pipeline: section     ????             is present ??proc-network designs may fail at the         ????             internal block-codegen lowering step after scheduling      ????             metrics have already been printed.                         ????    IR  ??codegen        (codegen_main --scheduling_strategy=agent     ????                           --block_metrics_path=<...>.textproto)       ????             In fast mode, codegen failure is a hard fail.             ????             In slow mode, codegen failure is non-fatal when           ????             benchmark_main already produced valid metrics.            ?????????????????????????????????????????????????????????????????????????????       ??       ???????????????????????????????????????????????????????????????????????????????STEP 7: PPA extraction (ppa_metrics.py)                                ????  Priority (first available wins per metric):                          ????    benchmark_main stdout  ??critical_path_ps, total_area_um2,         ????                              total_pipeline_flops, num_stages         ????    block_metrics textproto ??flop_count, max_reg_to_reg_delay_ps,     ????                              pipeline_stages, pipeline_reg_bits       ????    schedule textproto     ??length, min_clock_period_ps               ????    Verilog regex          ??approximate reg count (last resort)       ????  Score (all terms normalized to [0, 1] before weighting):            ????    (stages/ref_stages)         * stage_weight                        ????  + (flops/ref_flop_bits)       * flop_weight                         ????  + (area_um2/ref_area)         * area_weight                         ????  + (max_stage_delay/ref_clock) * delay_weight                        ????  + balance_cv_norm             * balance_weight  ??even load = lower ????  + (runtime_s/ref_timeout)     * runtime_weight                      ?????????????????????????????????????????????????????????????????????????????       ??       ???????????????????????????????????????????????????????????????????????????????STEP 8: Record + migrate                                               ????  - db.insert(candidate)                                               ????  - island.add(candidate); island is truncated to 50 by score.         ????  - If candidate score < best_score:                                   ????      write results/<run>/best_algorithm.patch (unified diff).         ????  - Every migration_interval iterations: copy global best into         ????    every island that doesn't already have it (cross-pollination).     ?????????????????????????????????????????????????????????????????????????????```

---

## Part 3.2 ??How the AI is prompted

Here's the complete call chain, file by file:

---

### High-level flow

```
run.py
  ?? sampler.sample(...)          # builds the prompt, picks backend
       ?? Sampler._call_codex()   # shells out to the `codex` CLI
            ?? codex exec -m gpt-5.4 --sandbox read-only --ephemeral -o /tmp/... -
                 (prompt piped via stdin)
                 (model response written to -o file)
       ?? Sampler._extract_cpp()  # strips markdown fences if any
  ?? evaluator.evaluate(generated_code)
```

---

### Step-by-step, with the exact code

#### 1. `run.py` ??calls `sampler.sample()` once per attempt

```python
# run.py  ~line 530
generated_code = sampler.sample(
    mutation_target=mutation_type,          # "agent_scheduler"
    mutation_instruction=mutation_instruction,  # the bootstrap or rotation instruction
    current_source=current_source,          # current .cc file text
    reference_source_bundle=...,           # other XLS scheduler sources for API context
    best_score=best_score,                 # best PPA seen so far (or None)
    best_num_stages=parent_stages,
    ...
    compile_error=last_compile_error,       # None on first attempt; clang error on retry
    target_file_path=target_file_rel,
)
```

`sampler.sample()` returns a raw C++ string. That string goes directly into `evaluator.evaluate()`.

---

#### 2. `alphaevolve/sampler.py` ??builds the prompt then shells out

**`sample()` renders the Jinja2 template:**
```python
# sampler.py  ~line 79
template = self._jinja.get_template("implement_agent_scheduler.txt")
user_prompt = template.render(
    mutation_instruction=mutation_instruction,
    current_source=current_source,
    compile_error=compile_error,        # injected into == PREVIOUS ATTEMPT FAILED ==
    ...
)
```

**`_call_codex()` shells out to the CLI:**
```python
# sampler.py  ~line 143
full_prompt = (
    f"{self._system_prompt}\n\n"
    f"{user_prompt}\n\n"
    "CRITICAL: Your ENTIRE response must be valid C++ source code only. ..."
)

result = subprocess.run(
    [
        "codex", "exec",
        "-m", self.model,           # e.g. "gpt-5.4"
        "--sandbox", "read-only",   # pure text generation, no shell allowed
        "--skip-git-repo-check",
        "--ephemeral",              # don't persist session files
        "-o", str(output_file),     # write model's last message to this file
        "-",                        # read prompt from stdin
    ],
    input=full_prompt,
    capture_output=True,
    text=True,
    timeout=300,
)

# Read the output file (the model's response)
if output_file.exists() and output_file.stat().st_size > 0:
    return output_file.read_text(encoding="utf-8")
```

The `-o` flag is key ??`codex exec` writes the model's final response text directly to a file, so there's no stdout scraping needed.

**`_extract_cpp()` strips fences if the model wrapped output anyway:**
```python
m = re.search(r"```(?:cpp|c\+\+)?\s*\n(.*?)```", raw, re.DOTALL)
return m.group(1).strip() if m else raw.strip()
```

---

#### 3. `alphaevolve/prompts/system.txt` + `implement_agent_scheduler.txt` ??what the model actually sees

The prompt has two parts concatenated:

| Part | Source | Content |
|------|--------|---------|
| System prompt | `prompts/system.txt` | Role description ??"you are an expert C++ compiler/HLS engineer" |
| User prompt | `prompts/implement_agent_scheduler.txt` (Jinja2) | Task, current PPA score, mutation instruction, current scheduler source, reference XLS API sources, output rules, and optionally the compiler error from the last attempt |

The compile-error injection at the bottom of the template:
```jinja2
{% if compile_error %}
== PREVIOUS ATTEMPT FAILED TO COMPILE ==
Compiler output:
{{ compile_error }}

Fix every compiler issue before writing the next version.
{% endif %}
```

---

#### 4. Back in `run.py` ??what happens to the returned string

```
sampler.sample() ??raw C++ string
  ??evaluator.evaluate(generated_code=...)
      ??evaluator._sanitize_generated_code()   # strips #include, namespace wrappers
      ??evaluator._splice_function()            # replaces the body in the .cc file
      ??builder.build()                         # bazel build ??compile
      ??pipeline.run()                          # ir ??opt ??benchmark ??codegen
```

---

### Key design decisions

- **No API key in the codex path** ??`codex exec` uses the session auth that was set up when the user ran `codex` interactively. No `OPENAI_API_KEY` environment variable is needed (though it's passed if present).
- **`--sandbox read-only`** ??prevents the model from executing shell commands; it only generates text.
- **`--ephemeral`** ??no conversation history is persisted across calls. Each `sampler.sample()` is a fresh, independent request.
- **`-o <file>` not stdout** ??the CLI's own progress/logging goes to stdout; the model output goes to a separate temp file, so there's no mixing to parse.
- **300s timeout on the codex CLI call** ??separate from the 1800s XLS build timeout.
- **OpenAI SDK fallback** ??`--backend openai` uses `Sampler._call_openai()` with the Python SDK directly, going through `OPENAI_API_KEY`. The prompt content is identical.

---

## Part 3.3 ??Compile failure and retry loop

This section explains exactly what happens when the AI generates C++ that does not compile, what gets fed back, and how the loop decides to retry or give up.

### The retry wrapper (run.py)

Each iteration is wrapped in a retry loop controlled by `max_build_retries` (default: 3, set in `evolve_config.yaml`):

```
for attempt in 1 .. max_build_retries:

    generated_code = sampler.sample(..., compile_error=last_compile_error)
    result         = evaluator.evaluate(..., generated_code=generated_code)

    # Write attempt record to disk immediately (preserves all retries)
    append iter<N>_island<K>_attempts.log with attempt/status/notes

    if result.candidate.build_status == "success":
        break                     # compiled and ran ??exit retry loop

    if result.candidate.build_status == "run_failed":
        break                     # compiled but no valid PPA ??do NOT retry

    last_compile_error = trim(result.candidate.notes)   # build_failed only

# record whichever result came last (even if build_failed)
```

On attempt 1, `compile_error=None` ??the prompt does not mention a previous failure.
On attempts 2 and 3, `compile_error` is the trimmed Clang output from the previous attempt.
`run_failed` exits the retry loop immediately ??it is a runtime outcome (infeasible schedule,
timeout, proc design in fast mode) and the AI cannot fix it by changing C++ syntax.

### What gets trimmed and passed back

`build_result.stderr` contains the full Bazel + Clang output (often thousands of lines).
`run.py` filters it before handing it to the AI:

```python
# Keep diagnostic lines and one line of context around each
kept = []
for i, line in enumerate(lines):
    if any(tag in line for tag in ("error:", "warning:", "note:", "^ ")):
        # one line before + the match + one line after
        if i > 0: kept.append(lines[i-1])
        kept.append(line)
        if i + 1 < len(lines): kept.append(lines[i+1])
last_compile_error = "\n".join(dict.fromkeys(kept))[:60 lines]
```

Lines containing `error:`, `warning:`, `note:`, or `^ ` (caret markers) are kept,
plus one surrounding line for context, deduplicated and capped at 60 lines. This
includes caret underlines that pin-point the exact character where the error occurred.

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

The AI sees the file, line, column, and a human-readable description ??enough to pinpoint and fix each issue without seeing the surrounding XLS source again.

### Build status values and what they mean

| `build_status` | Meaning | Retried? |
|----------------|---------|----------|
| `success` | Bazel returned 0; C++ compiled and linked. | ??(done) |
| `build_failed` | Bazel returned non-zero; Clang rejected the AI's C++. | **Yes** ??error fed back to AI, up to `max_build_retries` attempts. |
| `run_failed` | Compiled fine, but no design produced valid PPA in the pipeline (clock period exceeded, proc design in fast mode, scheduler timeout, etc.). | **No** ??this is a runtime outcome, not a code defect. The retry loop exits immediately and moves to the next iteration. |

### What counts as a build failure vs a run failure

**`build_failed`** ??the generated C++ itself is broken. Common causes:
- `#include` directives added by the AI that conflict with existing includes
- `namespace xls { }` wrapper re-nesting the code (already inside `namespace xls`)
- Calls to functions that don't exist in the scheduler API (`NodeCost`, `GetDelay`, etc.)
- Syntax errors, missing semicolons, unmatched braces
- Type mismatches (e.g. returning `int` where `absl::StatusOr<ScheduleCycleMap>` is required)

**`run_failed`** ??the C++ compiled, but the schedule was not accepted or produced no PPA. Common causes:
- AI-generated scheduler returns a map where some node cycles violate `[lb, ub]` bounds
- Clock period constraint not satisfiable with the generated schedule
- `benchmark_main` or `codegen_main` timed out (AI scheduler too slow ??e.g. O(n簡) inner loop on a large design)
- Design is a proc network and `--ppa_mode fast` was used (proc designs need `--ppa_mode slow`)

`run_failed` is **not** fed back to the AI as an error. The candidate is stored in the database with `ppa_score=inf` so it ranks last in island selection, and the next iteration starts fresh with a new AI call.

### What happens if all retries are exhausted

After `max_build_retries` failed compile attempts, the loop records the last `build_failed` candidate (with the most recent error in `notes`) and moves on to the next iteration. The island population is updated (the failing candidate is added with `ppa_score=inf`), so the next iteration's parent selection will avoid it.

No exception is raised; the run continues normally.

### Restore safety

`evaluator.evaluate()` wraps the backup ??apply ??build ??run sequence in a `try/finally` block. The `finally` always calls `builder.restore(target_file)` regardless of whether a `build_failed`, `run_failed`, `TimeoutExpired`, or any other exception occurred. This ensures `agent_generated_scheduler.cc` is always returned to its baseline state before the next iteration begins.

On startup, `run.py` also checks for a leftover `.bak` file (left by a previous run that was killed before the `finally` could execute) and auto-restores it with a warning.

---

## Part 3.4 ??Console output and stage callbacks

The spinner and log lines during an iteration are driven by two callbacks threaded from `run.py` ??`evaluator.py` ??`pipeline.py`:

```
on_stage_start(name: str, extra: str = "")
    Called immediately before a stage begins.
    run.py: updates the Rich spinner label to "??<name>  (<extra>)".
    Also appends "START  <name>  (<extra>)" to the stages.log file.

on_stage(name: str, status: str, duration_s: float, extra: str = "")
    Called immediately after a stage completes.
    run.py: prints a timed line "[??? <name>  <duration_s>s" above the spinner.
    Also appends "END    <name>  <status>  <duration_s>s" to stages.log.
```

Stage names visible on the console (in order for a slow-mode run):

| Stage name             | When shown                                           |
| ---------------------- | ---------------------------------------------------- |
| `compile:scheduler`    | Bazel compiling `agent_generated_scheduler`          |
| `compile:benchmark_main` | Bazel relinking `benchmark_main` (slow mode only)  |
| `[<stem>] ir_convert`  | `ir_converter_main` for each design                  |
| `[<stem>] opt_main`    | `opt_main` for each design                           |
| `[<stem>] AI scheduler`| `benchmark_main` executing the AI's C++ code         |
| `[<stem>] codegen`     | `codegen_main` (fast mode only)                      |

The `[<stem>]` prefix is added automatically when multiple designs are being evaluated, so the user can tell which design each stage belongs to. With a single design the prefix is omitted.

After each stage the spinner resets to "waiting for next stage..." until the next `on_stage_start` fires.

The success line printed after a completed iteration shows all score components:

```
??stages=<N>  area=<A>um簡  max_stg_dly=<D>ps  bal_cv=<B>  regs=<R>  runtime=<T>s  score=<S>  | build=<B>s
    score = stg(<n>)?W + dly(<d>)?W + bal(<b>)?W + area(<a>)?W + flp(<f>)?W + rt(<r>)?W = <S>
```

All six bracketed values are normalized ratios in [0, 1]. Example with `clock_period=25000`, iter6:

```
??stages=7  area=26065um簡  max_stg_dly=16651ps  bal_cv=0.085  regs=0  runtime=1.2s  score=1.6476  | build=11s
    score = stg(0.438)?1.0 + dly(0.666)?2.0 + bal(0.085)?1.5 + area(0.521)?0.2 + flp(0.000)?0.5 + rt(0.001)?0.5 = 1.6476
```

---

## Part 4 ??XLS pipeline, command level

### Stage 1: DSLX ??IR

```bash
ir_converter_main \
    --dslx_stdlib_path=<xls_src>/xls/dslx/stdlib \
    --top=<auto-detected> \
    design.x
```

`_detect_dslx_top()` scans the DSLX source for the last non-test `proc`/`fn` and sets it as the package top so downstream tools do not need the flag again.

### Stage 2: IR ??optimized IR

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

Prints a human-readable report including `Critical path delay`, `Total area`, `Total pipeline flops`, and a per-stage pipeline breakdown. A nonzero exit code is tolerated when the `Pipeline:` section is present ??proc-network designs may fail the register-reset lowering pass inside benchmark_main after they have already printed their scheduling stats.

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

## Part 5 ??`--ppa_mode` matrix

| Mode      | Per-iteration build targets                              | Runs benchmark_main? | Runs codegen_main per iter? | Proc support? | Score terms non-zero                           | Status      |
| --------- | -------------------------------------------------------- | -------------------- | --------------------------- | ------------- | ---------------------------------------------- | ----------- |
| `fast`    | agent_scheduler + codegen_main + opt_main + ir_converter | No                   | Yes (required)              | **No**        | stages, pipeline_reg_bits                      | Implemented |
| `medium`  | same as fast (Yosys planned)                             | No                   | Yes                         | No            | stages, reg_bits, gate_count (planned)         | Placeholder |
| `slow`    | agent_scheduler + benchmark_main only                    | Yes, each iter       | **No** (built at startup)   | **Yes**       | stages, reg_bits, area, delay, runtime_s       | Implemented |
| `slowest` | same as slow (Yosys+OpenROAD planned)                    | Yes                  | **No**                      | No            | stages, reg_bits, silicon area, WNS (planned)  | Placeholder |

Key difference in `slow` mode: `codegen_main` is **not** included in the per-iteration build targets. It is built once at startup (during the initial binary probe). This avoids relinking it on every iteration ??`benchmark_main` alone provides all needed PPA metrics in slow mode.

Implementation references:

- `xls_tools/build.py :: XLSBuilder.iteration_targets_for_mode(mode)`
  Returns `[agent_generated_scheduler, benchmark_main]` for slow/slowest; includes codegen_main + helpers for fast/medium.
- `xls_tools/pipeline.py :: XLSPipeline.run(..., ppa_mode=..., benchmark_timeout=...)`
  Gates the `benchmark_main` subprocess on mode; treats codegen failure as non-fatal in slow mode when benchmark_main produced metrics.
- `alphaevolve/evaluator.py :: Evaluator.__init__(..., ppa_mode="fast")`
  Stores the mode and threads it into both `builder.build(...)` and `pipeline.run(...)`.
- `run.py :: parse_args()`
  Declares `--ppa_mode {fast,medium,slow,slowest}` and `--benchmark_timeout N` (default 1800).

Placeholder modes emit a warning to stderr and then evaluate as if `fast`, so the loop keeps making progress.

---

## Part 6 ??Scoring

All metrics are converted into **unit-free ratios or penalties** before weighting, eliminating unit mismatch between picoseconds, square-micrometers, and seconds. Lower score is better.

```
score = (num_stages           / REF_STAGES)    ? stage_weight    ??penalise depth
      + (effective_flop_bits  / REF_FLOP_BITS) ? flop_weight     ??penalise register bits
      + (total_area_um簡       / REF_AREA_UM2)  ? area_weight     ??penalise area
      + (max_stage_delay_ps   / REF_CLOCK_PS)  ? delay_weight    ??penalise tight stages
      + balance_penalty                        ? balance_weight  ??penalise skew + clock overload
      + (scheduler_runtime_s  / REF_TIMEOUT_S) ? runtime_weight  ??penalise slow algos
```

Defaults from `alphaevolve/ppa_metrics.py` (overridable via `configs/evolve_config.yaml`):

| Parameter        | Default | Type      | Notes |
| ---------------- | ------- | --------- | ----- |
| `stage_weight`   | 0.0     | weight    | Pipeline depth penalty. Usually kept at 0 when stage count is fixed or when timing is the main signal. |
| `flop_weight`    | 0.5     | weight    | Pipeline register bit penalty. |
| `area_weight`    | 0.0     | weight    | Combinational area rarely changes with scheduling; often left off unless slow-mode area is being trusted heavily. |
| `delay_weight`   | 2.0     | weight    | Max stage delay penalty ??primary differentiator between scheduler quality. |
| `balance_weight` | 1.5     | weight    | Clock-aware stage-shape penalty. Lower is better; it grows when stage delays are uneven and/or multiple stages exceed the target clock. |
| `runtime_weight` | 0.5     | weight    | Scheduler wall-clock penalty. Timeout gives `runtime_s=3600` ??normalized penalty of `3600/REF_TIMEOUT_S`. |
| `ref_stages`     | 16      | reference | Set automatically from `pipeline_stages` in YAML if fixed. |
| `ref_flop_bits`  | 10000   | reference | Max expected pipeline register bits. |
| `ref_area_um2`   | 50000   | reference | Max expected combinational area (um簡). |
| `ref_clock_ps`   | ??      | reference | **Set from `--clock_period`** each run. Must not be left at default 1000. |
| `ref_timeout_s`  | 1800    | reference | Set from `--benchmark_timeout`. |

**Key metric: `max_stage_delay_ps`**

This is the maximum combinational delay across all pipeline stages, extracted from the `nodes: N, delay: Xps` lines in `benchmark_main` output. It replaces the old `critical_path_ps` (total design critical path) in the delay term. `critical_path_ps` was constant regardless of scheduling decisions; `max_stage_delay_ps` directly reflects how well the scheduler balanced stages.

**Key metric: balance penalty ??clock-aware stage shape**

The balance term uses per-stage utilization relative to the target clock:

```
u_i             = stage_delay_i / REF_CLOCK_PS
spread          = population_std(u_i)
overload        = sqrt(mean(max(0, u_i - 1)^2))
balance_penalty = spread + 2 * overload
```

- **0** = all stages are evenly balanced and every stage is at or below the target clock.
- Higher values mean the schedule is skewed and/or multiple stages are over the target clock.

This complements `max_stage_delay_ps` rather than replacing it. The delay term still captures the single worst stage; the balance term captures distribution shape and multi-stage overload across the whole pipeline.

`configure_scoring()` in `ppa_metrics.py` accepts both weight and reference keyword arguments and can be called at any point to update the module globals for the current process.

Aggregation across multiple designs (`evaluator._run_pipeline_on_designs`):

- `total_stages`, `total_flops`, `total_area` are **summed** across designs.
- `max_stage_delay`, `max_min_clock`, and `max_runtime_s` are the **maximum** across designs.
  (`max_runtime_s` uses the worst scheduler runtime across all designs, since the slowest design determines whether the iteration is practical.)

---

## Part 7 ??Per-run output layout

```
results/<timestamp>/
??? candidates_db.sqlite        Every candidate: code, score, diff, metrics, status.
??? evolution_log.csv           Flat CSV of every candidate (for plotting).
??? ppa_report.json             Final summary with the top 3 candidates.
??? best_algorithm.patch        Unified diff against the baseline scheduler.
??? eval_runs/
    ??? iter<NNNN>_island<K>_stages.log   Real-time stage log written as each XLS
    ??                                    stage starts and completes. Written by the
    ??                                    on_stage_start / on_stage callbacks in run.py.
    ??                                    Use `tail -f` during a slow run to watch
    ??                                    which stage is active and how long each takes.
    ??? iter<NNNN>_island<K>_attempts.log Per-iteration compile-retry record. One entry
    ??                                    per attempt (attempt number, build status, notes).
    ??                                    Written after every attempt ??data from failed
    ??                                    retries is not lost even if the loop continues.
    ??? iter<NNNN>_island<K>/
        ??? <design>/
            ??? <design>.ir                    after ir_converter_main
            ??? <design>_opt.ir                after opt_main
            ??? <design>_block_metrics.textproto   codegen_main (when it succeeds)
            ??? <design>_schedule.textproto        codegen_main (when it succeeds)
            ??? <design>.v                         codegen_main (when it succeeds)
            ??? <design>_benchmark.txt             benchmark_main (slow modes); or
                                                   "benchmark_main skipped" in fast mode
```

---

## Part 8 ??Data-flow summary

```
INPUTS                         EVOLUTION ENGINE                   OUTPUTS
??????                         ????????????????                   ???????

designs/*.x           ?????算?
designs/*_benchmark.txt ???算?   (optional baseline AI context)
                            ??configs/                    ??  For each iteration:
  evolve_config.yaml ??????算?
  ppa_constraints.yaml ????算?   1. sampler.py  ??AI ??new C++ body
                            ??       (reference sources + variants +
knowledge/**/*.md  ????????算?         baseline context + compile_error on retry)
                            ??                   ??xls/ (source)    ?????????算?                    ??  xls/scheduling/           ??  2. evaluator.py  ??splice ??Bazel
    agent_generated_         ??          rebuild agent + codegen +
    scheduler.cc ????????????算?           opt + ir_converter
    (the ONLY file mutated)  ??          (+ benchmark_main if slow)
                            ??                   ??                            ??                   ??                            ??  3. pipeline.py  DSLX ??IR ??opt ??                            ??          [benchmark_main if slow] ??                            ??          codegen (non-fatal fail in slow
                            ??          when benchmark succeeded)
                            ??                   ??                            ??                   ??                            ??  4. ppa_metrics.py  ??score
                            ??                   ??                            ??                   ??                            ??  5. db.insert, island.add, migrate
                            ??                   ??                            ??                   ??                            ??  6. restore baseline; next iteration
                            ???????????????????????                                    ??                                    ??                         results/<timestamp>/
                           best_algorithm.patch   ??? apply to XLS
                           ppa_report.json              for permanent adoption
                           evolution_log.csv
                           candidates_db.sqlite
                           eval_runs/iter*_island*/
```

---

## Part 9 ??What is intentionally untouched

By design, the following XLS files are never modified by the evolution loop:

- `xls/scheduling/sdc_scheduler.cc` ??XLS still uses it for `--scheduling_strategy=sdc`; we just stop invoking that strategy.
- `xls/scheduling/min_cut_scheduler.cc` ??same; referenced only as prompt context so the AI can learn XLS scheduling APIs.
- `xls/scheduling/run_pipeline_schedule.cc` ??already dispatches `AGENT` correctly.
- `xls/scheduling/scheduling_options.h` ??defines the `SchedulingStrategy::AGENT` enum.
- `xls/scheduling/BUILD` ??`agent_generated_scheduler` is already a dep of `run_pipeline_schedule`.
- `alphaevolve/database.py` and `alphaevolve/ppa_metrics.py` ??schema and parsing are stable.
- `alphaevolve/sampler.py` ??only the prompt template body is changed when tuning the AI.

The XLS source tree is always restored to its original state after each iteration. No iteration leaves the tree in a mutated state.

---

## Part 10 ??Architectural decisions

### Why a standalone scheduler file?

Earlier approaches mutated `SDCSchedulingModel::SetObjective()`, `ComputeCombinationalDelayConstraints()`, and `MinCutScheduler::Schedule()` inside XLS proper. That made every iteration expensive (the SDC LP solver is entangled with ortools) and caused frequent compile failures because the AI had to respect many XLS-internal invariants across multiple files.

The current design collapses all mutation targets into one standalone file that XLS explicitly opts into via `--scheduling_strategy=agent`. Three consequences:

1. **Incremental builds are much cheaper.** The agent file is small, has few reverse-deps, and does not link against ortools. Per-iteration relink is limited to the scheduler + the thin set of tools that include it.
2. **The AI contract is tractable.** The function has a fixed signature, five named helper functions, and a small set of `ScheduleBounds` APIs. Compile failures are easier for the AI to fix because the scope is narrow.
3. **We can skip `benchmark_main` in the common case.** `codegen_main` already emits a `block_metrics` textproto with stages and register bits ??enough signal for fast-mode evolution. `benchmark_main` (LLVM/JIT, slow build) is only invoked when the user asks for `--ppa_mode slow`.

### Why `--clock_period` is a CLI flag, not a YAML value

The clock period is a fixed hardware constraint for the entire run. Keeping it exclusively in the CLI argument prevents any YAML edit, config override, or AI-generated code from silently changing the timing target mid-run. The YAML files (`ppa_constraints.yaml`, `evolve_config.yaml`) hold only tuning knobs ??not the constraints themselves.

### Why islands start cold (no pre-seeding)

Seeding islands with the baseline scheduler's PPA would require running the full XLS pipeline before the evolution begins ??adding latency and coupling the startup to the current binary state. Instead, islands start empty and the first iteration's parent is `None`. The cold-start iteration generates a scheduler from scratch (guided by the AI prompt, reference sources, and any baseline context files). This is cheaper and avoids a class of startup errors where the baseline binary does not yet support `--scheduling_strategy=agent`.

### Why proc designs need `--ppa_mode slow`

XLS's `codegen_main --generator=pipeline` does not support proc networks (proc designs require `--generator=block` plus reset/state handling, which the pipeline generator does not provide). In `--ppa_mode fast`, `benchmark_main` is not run, so proc designs have no fallback PPA source when `codegen_main` fails. In `--ppa_mode slow`, `benchmark_main` runs the scheduler standalone and prints timing metrics before any codegen step, so its output is available even when `codegen_main` fails ??allowing proc designs to be scored correctly.

### AI output sanitization

The AI is instructed not to emit `#include` directives or wrap its output in `namespace xls {}`. These rules are stated prominently in the prompt because violating them causes compile failures: the output is spliced mid-file inside an existing `namespace xls` block, so any re-wrapping or extra includes result in redefinition errors. Compile errors are captured and fed back to the AI for a retry (up to `max_build_retries=3` attempts per iteration).

