# AlphaEvolve-XLS — System Architecture

> This document covers **what gets evolved** and the **file map**.
> See also: [Evolution_Loop.md](Evolution_Loop.md) · [Scoring_PPA.md](Scoring_PPA.md) · [Output_Decisions.md](Output_Decisions.md)

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

XLS dispatches this when `--scheduling_strategy=agent` is passed to any XLS tool. The dispatch is in `xls/scheduling/run_pipeline_schedule.cc`:

```
if      strategy == MIN_CUT → MinCutScheduler::Schedule()
else if strategy == AGENT   → AgentGeneratedScheduler()   ← mutated each iteration
else if strategy == RANDOM  → random walk
else                        → SDCSchedulingModel / ASAP
```

We do **not** modify the dispatch code, `scheduling_options.h`, or any XLS BUILD files — they are already correct. The BUILD dependency `//xls/scheduling:agent_generated_scheduler` is already a dep of `run_pipeline_schedule`, so relinking codegen/benchmark tools picks up the new object automatically.

### The scheduler contract

1. Call `bounds->PropagateBounds()` **once** before the main loop.
2. Walk nodes via `TopoSort(f)`.
3. Skip any node where `IsUntimed(node)` is true.
4. For each timed node, compute `lb = max(bounds->lb(node), max_assigned_cycle_of_operands)`.
5. Cap `ub = min(bounds->ub(node), pipeline_stages − 1)` when `pipeline_stages > 0`.
6. Choose a cycle in `[lb, ub]` using a principled heuristic.
7. Record in `ScheduleCycleMap` (`absl::flat_hash_map<Node*, int64_t>`).
8. Pin with `bounds->TightenNodeLb(node, cycle)` and `bounds->TightenNodeUb(node, cycle)`.
9. **Never** call `bounds->PropagateBounds()` inside the per-node loop — it is O(n) per call and causes timeouts on large designs (SHA-256 has ~800 IR nodes).
10. Return the completed map.

Reusable helpers already in the file: `NodeBitCount`, `NodeFanout`. The AI may add helpers in a fresh anonymous namespace block above the target function.

### Dry-run stub

`agent_generated_scheduler.cc` contains a fast ASAP stub gated by the `XLS_AGENT_DRY_RUN` env var. When `--dry_run` is passed, `run.py` sets this variable so the pipeline validates end-to-end without AI calls or builds. The splicer replaces the entire function body each iteration, so the stub is only active between iterations (it is always restored from `.bak` after evaluation).

---

## Part 1A — Inside `agent_generated_scheduler.cc`

### File zones

```
Zone 1  License header + #include directives          Static. Never touched by evolution.
Zone 2  namespace xls {                               Static.
Zone 3  Anonymous namespace blocks (helpers)          Grows — prior iteration helpers accumulate.
Zone 4  AgentGeneratedScheduler() — full function     Replaced every iteration by the splicer.
Zone 5  }  // namespace xls                           Static.
```

### Section A — Dry-run stub (inside Zone 4)

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

Active only when `--dry_run` is passed. Replaced by the AI's output on every real iteration.

### Section B — The heuristic (what evolution replaces)

Below the dry-run guard is the live scheduler. The current evolved body uses a multi-factor cost function weighing:
- timing overflow (quadratic penalty for exceeding `clock_period_ps`)
- register pressure (bit-width of values crossing a pipeline boundary)
- stage load (node count per stage — lower variance is better)
- criticality (fanout × bit-width / scheduling mobility)

### How `_splice_function` works

```python
# evaluator.py — _splice_function(source, signature, new_body)
1. Regex-find `signature` in source.
2. Scan forward from match to find the opening `{` of the function body.
3. Brace-count forward until depth returns to 0 → brace_end.
4. Reconstruct:
     before = source[ : match.start() ]    # Zones 1-3 intact
     after  = source[ brace_end + 1 : ]   # Zone 5
     result = before + new_body + "\n" + after
```

### What the AI must produce

```cpp
// Optional: new helpers
namespace {
int64_t MyHelper(Node* node, ...) { ... }
}  // namespace

// Required: verbatim signature
absl::StatusOr<ScheduleCycleMap> AgentGeneratedScheduler(
    FunctionBase* f, int64_t pipeline_stages, int64_t clock_period_ps,
    const DelayEstimator& delay_estimator, sched::ScheduleBounds* bounds,
    absl::Span<const SchedulingConstraint> constraints) {
  // ... implementation ...
}
```

`_sanitize_generated_code` strips `#include` lines and unwraps any `namespace xls {}` before splicing, since Zone 2 already opens that namespace.

### Helper accumulation

Old helper namespaces remain in Zone 3 across iterations. They are in anonymous namespaces (no ODR violations) and unused symbols are discarded by the linker. They are visible to the AI as evolutionary memory.

---

## Part 2 — File map

```
alphaevolve-xls/
│
├── run.py                          Main entry point. CLI, config loading, build probe,
│                                   evolution loop, retry logic, console output.
│
├── configs/
│   ├── evolve_config.yaml          num_islands (default 1), ai_backend, ppa_mode,
│   │                               mutation_types: [agent_scheduler], score weights.
│   └── ppa_constraints.yaml        delay_model, generator (pipeline), optional
│                                   per-design pipeline_stages overrides.
│                                   clock_period_ps is NOT stored here —
│                                   it comes exclusively from --clock_period CLI flag.
│
├── designs/
│   ├── mac/mac.x                   32-bit multiply-accumulate (proc)
│   ├── fir_filter/fir.x            Simple FIR filter
│   ├── fir32/fir32.x               32-tap FIR filter
│   ├── dot_product/dot.x           8-element dot product
│   ├── gemm4x4_int/gemm4x4_int.x  4×4 integer matrix multiply
│   ├── idct/idct.x                 2D IDCT (Chen algorithm)
│   ├── irregular_fusion/           Irregular datapath fusion benchmark
│   ├── sha256/sha256.x             SHA-256 hash (64-round feedback, ~800 IR nodes)
│   ├── bitonic_sort/               bitonic_sort.x (library) + bitonic_sort_wrapper.x
│   ├── crc32/crc32.x               CRC-32 checksum
│   └── matmul4x4/matmul_4x4.x     4×4 float32 FMA systolic array (proc)
│   (each folder may hold <stem>_benchmark.txt — optional baseline AI context)
│
├── alphaevolve/
│   ├── sampler.py                  Builds Jinja2 prompt, calls codex/OpenAI SDK,
│   │                               strips markdown fences, returns C++ string.
│   │                               Key params: current_source, reference_source_bundle,
│   │                               baseline_benchmark_context, compile_error.
│   ├── evaluator.py                Splice → build → run → extract PPA → restore.
│   │                               MUTATION_TARGETS has ONE entry:
│   │                                 "agent_scheduler": (
│   │                                   "xls/scheduling/agent_generated_scheduler.cc",
│   │                                   "absl::StatusOr<ScheduleCycleMap>"
│   │                                   " AgentGeneratedScheduler(")
│   ├── ppa_metrics.py              Parses metrics from multiple sources (priority chain).
│   │                               Normalized scoring — see Scoring_PPA.md.
│   ├── database.py                 SQLite store: candidate, metrics, diff, status.
│   ├── islands.py                  Island population manager. mutation_type="agent_scheduler".
│   │                               4 instruction variants (register-pressure / ASAP /
│   │                               mobility / lookahead). pinned_island_id supported.
│   └── prompts/
│       ├── system.txt              AI persona.
│       ├── implement.txt           Legacy fallback template.
│       └── implement_agent_scheduler.txt  Active Jinja2 template used by sampler.py.
│
├── xls_tools/
│   ├── build.py                    Bazel wrapper. Per-iteration targets depend on ppa_mode.
│   │                               See Scoring_PPA.md for the mode matrix.
│   └── pipeline.py                 DSLX → IR → opt → [benchmark_main] → codegen.
│                                   Always uses --scheduling_strategy=agent.
│                                   Accepts ppa_mode, benchmark_timeout, on_stage callbacks.
│
├── knowledge/
│   ├── papers/*.md                 Scheduling-theory summaries.
│   └── heuristics/*.md             ASAP/ALAP/mobility references.
│
├── docs/                           This documentation.
├── plan/                           Architecture planning notes.
├── scripts/                        Ad-hoc validation helpers.
└── results/<timestamp>/            Per-run artefacts — see Output_Decisions.md.
```
