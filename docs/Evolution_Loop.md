# AlphaEvolve-XLS — Evolution Loop

> Part of the system architecture docs.
> See also: [Architecture.md](Architecture.md) · [Scoring_PPA.md](Scoring_PPA.md) · [Output_Decisions.md](Output_Decisions.md)

---

## Part 3.1 — The evolution loop

```
python run.py \
    --input_file designs/sha256/sha256.x \
    --clock_period 1000 \
    --iterations 20 \
    --ppa_mode fast \
    --backend codex
```

### Startup sequence

1. Load `ppa_constraints.yaml` + `evolve_config.yaml`.
2. Apply CLI overrides (`--mutation_target`, `--backend`, `--model`, `--ppa_mode`, `--num_islands`, `--island_id`).
3. Inject `clock_period_ps` from `--clock_period` into `ppa_cfg`. The YAML files do not store this value — the CLI is the single source of truth.
4. Validate `ppa_mode ∈ {fast, medium, slow, slowest}`. `medium`/`slowest` emit a placeholder warning and run as `fast`.
5. Construct `XLSBuilder` + `XLSPipeline` + `Sampler` + `Evaluator` + `IslandManager` + `CandidateDB`.
6. Probe existing binaries with `supports_agent_strategy()`. If `--scheduling_strategy=agent` is missing from the binary → incremental Bazel rebuild.
7. If `ppa_mode` is `slow` or `slowest`: build `benchmark_main` at startup.
8. For each design folder, check for `<stem>_benchmark.txt`. If found, load as optional baseline context for the AI prompt. Islands start empty (cold); no pre-seeding.

### Per-iteration steps

**Step 1 — Island selection** (`islands.py :: IslandManager.select_island(iteration)`)
- `--num_islands 1` → always island 0
- `--island_id N` → always island N
- otherwise → round-robin by iteration

**Step 2 — Parent selection** (`IslandManager.select_parent(island)`)
- Tournament (size 2) over island's successful candidates
- Falls back to global best in DB
- Returns `None` on cold start (first iteration of a fresh run)

**Step 3 — Mutation instruction** (`IslandManager.mutation_instruction_for(island, iteration)`)

Iterations 0–2 → bootstrap instructions (3 diverse families):
- `0`: DAG-DP — forward pass, `cost(v,c) = Σ max(0, c − assigned[u]) × bw`
- `1`: stage-load balancer — equalize node count per stage
- `2`: two-pass critical-path-first — zero-mobility nodes ASAP, rest ALAP

Iterations 3+ → rotating variants for `agent_scheduler`:
- `0`: register-pressure-aware list scheduler
- `1`: ASAP-first heuristic with delay-model tie-breaking
- `2`: mobility-driven greedy
- `3`: deterministic multistage heuristic with lookahead

All variants append a `_RUNTIME_WARNING` reminding the AI about SHA-256's ~800 IR nodes, the 30-min timeout, the `runtime_s=3600` penalty, and the O(n×W) algorithm requirement.

**Step 4 — AI sampling** (see Part 3.2)

**Step 5 — Evaluate** (`evaluator.py :: Evaluator.evaluate(...)`)

```
5a. _splice_function(source, signature, new_body)
5b. builder.backup(target_file)  →  builder.apply(target_file, new_source)
5c. Build:
      fast|medium → one step: agent_scheduler + codegen_main + opt_main + ir_converter_main
      slow|slowest → two steps:
        "compile:scheduler"     = agent_generated_scheduler only
        "compile:benchmark_main" = benchmark_main (relinks with new scheduler object)
5d. Build failure → restore .bak; trim error to ≤60 lines; feed back as compile_error;
    retry up to max_build_retries=3.
5e. run_failed (compiled but no valid PPA) → break retry loop immediately; do NOT retry.
5f. Success → run XLS pipeline on each benchmark design.
5g. Restore backup.
```

**Step 6 — XLS pipeline** (see [Scoring_PPA.md](Scoring_PPA.md) for the command details)

**Step 7 — PPA extraction** (see [Scoring_PPA.md](Scoring_PPA.md))

**Step 8 — Record + migrate**
- `db.insert(candidate)`
- `island.add(candidate)` — truncated to 50 by score
- If score < best: write `results/<run>/best_algorithm.patch` (unified diff)
- Every `migration_interval` iterations: copy global best into every island (cross-pollination)

---

## Part 3.2 — How the AI is prompted

### Call chain

```
run.py
  └─ sampler.sample(...)          builds prompt, picks backend
       └─ Sampler._call_codex()   shells out to codex CLI
            └─ codex exec -m gpt-5.4 --sandbox read-only --ephemeral -o /tmp/out.cpp -
                 (prompt on stdin; model response written to -o file)
       └─ Sampler._extract_cpp()  strips markdown fences
  └─ evaluator.evaluate(generated_code)
```

### Prompt construction (`sampler.py`)

```python
# sampler.py — sample() renders the Jinja2 template
template = self._jinja.get_template("implement_agent_scheduler.txt")
user_prompt = template.render(
    mutation_instruction=mutation_instruction,
    current_source=current_source,           # current .cc file text
    reference_source_bundle=reference_source_bundle,  # other XLS schedulers for API context
    baseline_benchmark_context=baseline_benchmark_context,  # optional <stem>_benchmark.txt
    compile_error=compile_error,             # None on first attempt; Clang output on retry
    target_file_path=target_file_path,
    ...
)
```

### Backend: codex

```python
result = subprocess.run(
    ["codex", "exec",
     "-m", self.model,            # e.g. "gpt-5.4"
     "--sandbox", "read-only",    # text generation only; no shell
     "--skip-git-repo-check",
     "--ephemeral",               # no session persistence
     "-o", str(output_file),      # model response written here (not stdout)
     "-",                         # read prompt from stdin
    ],
    input=full_prompt, capture_output=True, text=True, timeout=300,
)
```

The `-o` flag is key — `codex exec` writes the model's final message to a file, avoiding stdout mixing with Codex's own progress logs.

### Backend: openai (fallback)

`--backend openai` uses `Sampler._call_openai()` via the Python SDK with `OPENAI_API_KEY`. Prompt content is identical; the `--sandbox` and `--ephemeral` restrictions do not apply.

### Compile-error injection

```jinja2
{% if compile_error %}
== PREVIOUS ATTEMPT FAILED TO COMPILE ==
Compiler output:
{{ compile_error }}

Fix every compiler issue before writing the next version.
{% endif %}
```

### Key design decisions

- **No API key for codex** — uses session auth from interactive `codex` login.
- **`--sandbox read-only`** — model cannot execute shell commands.
- **`--ephemeral`** — no conversation history between calls.
- **300 s timeout on AI call** — separate from 1800 s XLS build timeout.

---

## Part 3.3 — Compile failure and retry loop

### Retry logic

```
for attempt in 1 .. max_build_retries:  # default 3, from evolve_config.yaml

    generated_code = sampler.sample(..., compile_error=last_compile_error)
    result         = evaluator.evaluate(..., generated_code=generated_code)
    append iter<N>_island<K>_attempts.log

    if result.build_status == "success":  break
    if result.build_status == "run_failed": break  # do NOT retry

    last_compile_error = trim(result.notes)   # build_failed only
```

### Error trimming

Bazel output can be thousands of lines. `run.py` filters before passing to the AI:

```python
for i, line in enumerate(lines):
    if any(tag in line for tag in ("error:", "warning:", "note:", "^ ")):
        if i > 0: kept.append(lines[i-1])
        kept.append(line)
        if i + 1 < len(lines): kept.append(lines[i+1])
last_compile_error = "\n".join(dict.fromkeys(kept))  # capped at 60 lines
```

### Build status values

| Status | Meaning | Retried? |
|--------|---------|----------|
| `success` | Bazel returned 0; compiled and linked. | — |
| `build_failed` | Clang rejected the AI's C++. | **Yes** — up to `max_build_retries` |
| `run_failed` | Compiled fine; no design produced valid PPA (timeout, proc in fast mode, infeasible schedule). | **No** — runtime outcome, not a code defect |

### Restore safety

`evaluator.evaluate()` wraps everything in `try/finally`. The `finally` always calls `builder.restore(target_file)`, ensuring `agent_generated_scheduler.cc` is always returned to its baseline state. On startup, `run.py` also auto-restores any leftover `.bak` file from a previously killed run.

---

## Part 3.4 — Console output and stage callbacks

Two callbacks are threaded `run.py → evaluator.py → pipeline.py`:

```
on_stage_start(name, extra="")  → updates Rich spinner label; appends to stages.log
on_stage(name, status, duration_s, extra="")  → prints timed line; appends to stages.log
```

Stage names in order (slow mode):

| Stage name | When shown |
|---|---|
| `compile:scheduler` | Bazel compiling `agent_generated_scheduler` |
| `compile:benchmark_main` | Bazel relinking `benchmark_main` (slow only) |
| `[<stem>] ir_convert` | `ir_converter_main` for each design |
| `[<stem>] opt_main` | `opt_main` for each design |
| `[<stem>] AI scheduler` | `benchmark_main` executing the AI's C++ (slow only) |
| `[<stem>] codegen` | `codegen_main` (fast mode) |

The `[<stem>]` prefix is added automatically when multiple designs are evaluated.

### Score line format

```
✓ stages=<N>  area=<A>um²  max_stg_dly=<D>ps  bal_cv=<B>  regs=<R>  runtime=<T>s  score=<S>
    score = stg(<n>)×W + dly(<d>)×W + bal(<b>)×W + area(<a>)×W + flp(<f>)×W + rt(<r>)×W = <S>
```
