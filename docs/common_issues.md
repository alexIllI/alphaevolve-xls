# AlphaEvolve-XLS — Common Issues

Practical diagnostics for problems that frequently come up during evolution runs.

---

## Issue 1 — Every iteration produces `stages=1`

**Symptom:** Every successful candidate shows `stages=1` regardless of algorithm. The score is
dominated by the `delay` and `balance` terms; the `stage` component is always just `1/ref_stages`,
and `balance_cv_norm` is 0 (trivially balanced — only one stage).

**Cause: clock period is too loose for the delay model.**

With `--delay_model unit`, every IR node costs exactly 1 ps. SHA256 has a critical path of
399 nodes = 399 ps. Any `clock_period >= 399` allows all nodes to fit in a single stage with
zero timing overflow — so the scheduler correctly produces 1 stage and the score formula
rewards it (fewer stages = lower score).

```
sha256 critical path (unit model): 399 ps
clock_period = 3000 → 7.5× the critical path → trivially 1 stage
clock_period =  500 → 1.25× the critical path → still 1 stage
clock_period =  399 → exactly at the limit → still 1 stage
clock_period =   50 → 8 stages minimum → real scheduling decisions needed
```

**Fix A — Tighten the clock period.**

Pick a clock period below the critical path divided by your target stage count:

```bash
# Force ~8 stages for sha256 with unit model
python run.py --input_file designs/sha256/sha256.x --clock_period 50 ...

# Force ~4 stages
python run.py --input_file designs/sha256/sha256.x --clock_period 100 ...
```

**Fix B — Pin the stage count directly.**

Set `pipeline_stages` in `configs/ppa_constraints.yaml`. This tells XLS (and the AI) that
exactly N stages are required. The scheduler must distribute nodes across all N stages.

```yaml
# configs/ppa_constraints.yaml
pipeline_stages: 4   # forces sha256 into exactly 4 stages
```

With a fixed stage count, the optimization problem becomes non-trivial: the AI must decide
which of the 4,919 SHA256 nodes goes into each of the 4 stages to minimize register pressure,
area, and critical-path delay simultaneously. This is where heuristics can meaningfully
differ from each other and from SDC.

**Fix C — Use real timing with `--ppa_mode slow` and `delay_model: asap7`.**

With ASAP7 delays, individual nodes have realistic picosecond costs (adders, multipliers, XOR
chains are all different). SHA256's critical path is much longer in real ps, so even a moderate
clock period forces multi-stage scheduling naturally.

```yaml
# configs/ppa_constraints.yaml
delay_model: "asap7"
pipeline_stages: null   # let the clock and real delays determine stage count
```

```bash
python run.py --input_file designs/sha256/sha256.x \
  --clock_period 500 --ppa_mode slow ...
```

**When does 1 stage actually matter?**

If you care only about combinational area and critical-path delay (not pipeline depth), 1 stage
is a valid and interesting result — the scheduler is still choosing how to pack the combinational
logic. But for register-pressure and pipeline-latency experiments, fix the stage count.

---

## Issue 2 — "schedule infeasible" and "no design met constraints" are not the same thing

**Symptom:** An iteration prints something like:

```
↷ AI scheduler   37.6s
✗ run_failed  — no design met constraints
```

or benchmark_main logs contain "schedule infeasible for this clock period".

**Common misreading:** "The clock period is too tight — I should loosen it."

**Actual meaning:** These two error strings come from entirely different root causes.

### The three real causes

| What you see | Actual cause | How to tell |
|---|---|---|
| "schedule infeasible for this clock period" | AI scheduler returned a **cycle assignment that violates `[lb, ub]` bounds** — XLS's own verifier rejected the map. | `AI scheduler` stage shows short runtime (< timeout). Schedule ran but produced invalid output. |
| "no design met constraints" (short runtime) | Same as above, or `benchmark_main` printed no `Pipeline:` section (scheduler crashed internally). | `AI scheduler` stage shows runtime well under the timeout. |
| "no design met constraints" (runtime ≈ 3600s) | **Timeout** — AI scheduler ran for the full timeout period (O(n²) algorithm on 5000-node graph). | `AI scheduler` stage shows `runtime=3600s`. |

The clock period is **not** a likely cause at `clock_period >= 399` (unit model) for SHA256,
because any clock that accommodates the critical path produces a feasible schedule — the
problem is the schedule the AI returned, not the hardware constraint.

### What causes "schedule infeasible" (invalid ScheduleCycleMap)

XLS verifies the returned `ScheduleCycleMap` against the constraint system after
`AgentGeneratedScheduler()` returns. If any of the following are true, it rejects the schedule:

- A node is assigned to cycle `c < bounds->lb(node)` or `c > bounds->ub(node)`
- A node is assigned to an earlier cycle than one of its operands (data dependency violated)
- The AI used `bounds->ub(node)` from a stale read (before `PropagateBounds()` was called)
  and assigned a node beyond the legal upper bound

These are bugs in the AI-generated scheduler, not clock-constraint problems.

### What causes timeout (runtime ≈ 3600s)

The AI generated an O(n²) or worse algorithm. Common patterns:

- Calling `bounds->PropagateBounds()` after every node assignment — O(n) per call × 5000 nodes = O(n²)
- A nested loop over all nodes inside the per-node scoring function
- Repeated full-graph passes in a repair or refinement loop

SHA256 has ~5,000 IR nodes. An O(n²) scheduler performs ~25 million inner-loop iterations
just for bounds propagation alone — this reliably hits the 30-minute timeout.

### How to distinguish the failure modes

Check the `eval_runs/iter<N>_island<K>_stages.log` for the failing iteration:

```
# Timeout (O(n²) scheduler):
START  AI scheduler   running in benchmark_main  timeout=1800s
END    AI scheduler   ok   runtime=1800s          ← hit the limit

# Invalid schedule (scheduler bug):
START  AI scheduler   running in benchmark_main  timeout=1800s
END    AI scheduler   ok   runtime=12.3s          ← fast, but schedule rejected
```

Short runtime + run_failed = invalid schedule. Long runtime + run_failed = timeout.

### What to do

**For invalid schedule errors:**
- The AI is not respecting `bounds->lb/ub` — check that it reads bounds correctly and does
  not assign nodes outside their feasible windows.
- The AI may be ignoring `IsUntimed(node)` — untimed nodes must be skipped.
- The AI may be computing lb incorrectly — lb must be at least `max(assigned_cycle[pred])`
  for all timed predecessors.

**For timeouts:**
- The AI is using an O(n²) algorithm. The most common cause is calling
  `bounds->PropagateBounds()` inside the per-node loop. This must be called **once**
  before the loop, not after every assignment.
- See the complexity constraint in the mutation instructions — the correct lb-tracking
  pattern is O(degree) per node, not O(n).

---

## Issue 3 — Agent scheduler is much slower than SDC on the same design

**Symptom:** SDC finishes in ~2.5s for SHA256; the agent scheduler takes 37s or more even
when it produces the identical 1-stage schedule.

**Cause: `bounds->PropagateBounds()` called inside the scheduling loop.**

`bounds->PropagateBounds()` walks the entire constraint graph — O(n) per call. Called once
per node in a 5000-node graph = O(n²) = ~25 million propagation steps. SDC sets up one LP
and solves it in one pass without this overhead.

The fix is to call `PropagateBounds()` **once before the main loop** and then compute each
node's lower bound manually:

```cpp
XLS_RETURN_IF_ERROR(bounds->PropagateBounds());  // ONCE

for (Node* node : topo_nodes) {
    if (IsUntimed(node)) continue;

    // O(degree), not O(n):
    int64_t lb = bounds->lb(node);
    for (Node* pred : node->operands()) {
        if (!IsUntimed(pred) && assigned_cycles.count(pred))
            lb = std::max(lb, assigned_cycles.at(pred));
    }
    int64_t ub = bounds->ub(node);
    lb = std::min(lb, ub);

    // ... pick cycle in [lb, ub] ...

    assigned_cycles[node] = best;
    cycle_map[node] = best;
    XLS_RETURN_IF_ERROR(bounds->TightenNodeLb(node, best));  // O(1)
    XLS_RETURN_IF_ERROR(bounds->TightenNodeUb(node, best));  // O(1)
    // NO PropagateBounds() here
}
```

This pattern is now included in every mutation instruction sent to the AI.

**Can the agent ever match SDC's speed?**

Yes. A correct O(n×W) greedy scheduler (W = candidate window width, typically 1–20) on 5000
nodes with W=1 (single-stage case) runs in microseconds — faster than SDC's LP setup. The
agent's 37s runtime in the example above was entirely `PropagateBounds()` overhead, not
the scoring logic itself.

---

## Issue 4 — All first attempts fail to compile (`TopoSort` error)

**Symptom:** Every iteration shows `✗ compile:scheduler` on attempt 1 with an error like:

```
error: invalid range expression of type 'absl::StatusOr<std::vector<...>>'
note: did you mean to dereference with '*'?
for (Node* node : TopoSort(f)) { ...
```

**Cause:** `TopoSort(f)` returns `absl::StatusOr<std::vector<Node*>>`, not a vector directly.
The AI repeatedly writes the incorrect range-for form.

**Fix:** The prompt now includes a prominent API patterns section showing the correct idiom:

```cpp
// ✓ CORRECT
XLS_ASSIGN_OR_RETURN(std::vector<Node*> nodes, TopoSort(f));
for (Node* node : nodes) { ... }

// ✗ COMPILE ERROR
for (Node* node : TopoSort(f)) { ... }  // StatusOr is not iterable
```

If you still see this error after the prompt update, check that the AI is reading the
"CRITICAL API PATTERNS" section — it appears in every prompt before the output rules.
