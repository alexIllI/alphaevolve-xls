#!/usr/bin/env python3
"""
run.py — AlphaEvolve-XLS Main Entry Point
──────────────────────────────────────────
Evolves XLS scheduling algorithms to improve hardware design PPA
(Power/Performance/Area) using AI-generated C++ implementations.

The mutation target is always AgentGeneratedScheduler() in
xls/scheduling/agent_generated_scheduler.cc. XLS dispatches it when the
scheduling strategy is ``agent``.

Usage:
  python run.py --input_file designs/mac/mac.x --iterations 20

  python run.py \\
    --input_file designs/mac/mac.x \\
    --ppa_constraints configs/ppa_constraints.yaml \\
    --evolve_config configs/evolve_config.yaml \\
    --iterations 50 \\
    --output_dir results/mac_exp_001 \\
    --ppa_mode fast \\
    --backend openai
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

# Load environment variables from .env (if present)
load_dotenv()

# ── Project root (directory containing this file) ─────────────────────────────
PROJECT_ROOT = Path(__file__).parent.resolve()

# ── Logging ───────────────────────────────────────────────────────────────────
console = Console()


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AlphaEvolve-XLS: AI-driven HLS scheduling algorithm research",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--input_file", required=True,
        help="Path to the primary DSLX design file (.x). Additional designs can be "
             "specified in --extra_designs.",
    )
    parser.add_argument(
        "--clock_period", type=int, required=True,
        help=(
            "Target clock period in picoseconds. The scheduler must fit all operations "
            "within this timing budget per pipeline stage. This value is a fixed "
            "constraint — it is never modified by the evolution process. "
            "Example: --clock_period 1000  (1 ns, suitable for most arithmetic designs). "
            "Wide multipliers or float FMA units may require 1500–2000 ps."
        ),
    )
    parser.add_argument(
        "--extra_designs", nargs="*", default=[],
        help="Optional additional DSLX designs to include in PPA evaluation.",
    )
    parser.add_argument(
        "--ppa_constraints",
        default=str(PROJECT_ROOT / "configs" / "ppa_constraints.yaml"),
        help="Path to PPA constraints YAML. Default: configs/ppa_constraints.yaml",
    )
    parser.add_argument(
        "--evolve_config",
        default=str(PROJECT_ROOT / "configs" / "evolve_config.yaml"),
        help="Path to evolution config YAML. Default: configs/evolve_config.yaml",
    )
    parser.add_argument(
        "--iterations", type=int, default=10,
        help="Number of evolution iterations. Default: 10",
    )
    parser.add_argument(
        "--output_dir", default=None,
        help="Output directory for results. Default: results/<timestamp>",
    )
    parser.add_argument(
        "--xls_src",
        default=os.environ.get("XLS_SRC_PATH", "/mnt/d/final/xls"),
        help="Path to XLS source clone. Default: $XLS_SRC_PATH or /mnt/d/final/xls",
    )
    parser.add_argument(
        "--xls_prebuilt",
        default=os.environ.get("XLS_BIN_PATH", ""),
        help="Path to pre-built XLS binary directory (fallback if Bazel build not done).",
    )
    parser.add_argument(
        "--mutation_target",
        choices=["agent_scheduler"],
        default="agent_scheduler",
        help=(
            "Mutation target. Only 'agent_scheduler' is supported — the AI "
            "evolves xls/scheduling/agent_generated_scheduler.cc. Kept as a "
            "CLI flag for forward compatibility."
        ),
    )
    parser.add_argument(
        "--ppa_mode",
        choices=["fast", "medium", "slow", "slowest"],
        default=None,
        help=(
            "PPA evaluation depth per iteration. Overrides evolve_config.yaml:\n"
            "  fast    — codegen_main only; parse block_metrics for stages + reg_bits\n"
            "  medium  — (future) Yosys synth; stat\n"
            "  slow    — rebuild benchmark_main each iter for asap7 area + delay\n"
            "  slowest — (future) Yosys synth_asap7 + OpenROAD"
        ),
    )
    parser.add_argument(
        "--backend", choices=["openai", "codex"], default=None,
        help="AI backend. Overrides evolve_config.yaml ai_backend.",
    )
    parser.add_argument(
        "--model", default=None,
        help="AI model name. Overrides evolve_config.yaml ai_model.",
    )
    parser.add_argument(
        "--num_islands", type=int, default=None,
        help=(
            "Number of independent island populations. Overrides evolve_config.yaml. "
            "Use 1 to disable island rotation (pure linear evolution, all iterations "
            "on one island, simplest behaviour). Default: num_islands from config (4)."
        ),
    )
    parser.add_argument(
        "--island_id", type=int, default=None,
        help=(
            "Pin all iterations to a single island by ID (0-indexed). Useful for "
            "debugging a specific mutation strategy without round-robin rotation. "
            "E.g. --island_id 0 always uses island 0. Ignored if num_islands=1."
        ),
    )
    parser.add_argument(
        "--benchmark_timeout", type=int, default=1800,
        help=(
            "Seconds before benchmark_main is killed. Default: 1800 (30 min). "
            "A timed-out run records runtime_s=3600 as a score penalty. "
            "Use a smaller value (e.g. 300) to fail fast during debugging."
        ),
    )
    parser.add_argument(
        "--dry_run", action="store_true",
        help="Run the pipeline without calling the AI (validates setup only).",
    )
    parser.add_argument(
        "--log_level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING"],
    )
    return parser.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    args = parse_args()
    setup_logging(args.log_level)
    log = logging.getLogger("alphaevolve")

    # ── Load configs ──────────────────────────────────────────────────────────
    with open(args.ppa_constraints) as f:
        ppa_cfg = yaml.safe_load(f)
    with open(args.evolve_config) as f:
        evo_cfg = yaml.safe_load(f)

    from alphaevolve.ppa_metrics import configure_scoring
    import alphaevolve.ppa_metrics as _ppa_mod

    # CLI overrides
    if args.mutation_target:
        evo_cfg["mutation_types"] = [args.mutation_target]
    if args.backend:
        evo_cfg["ai_backend"] = args.backend
    if args.model:
        evo_cfg["ai_model"] = args.model
    if args.ppa_mode:
        evo_cfg["ppa_mode"] = args.ppa_mode

    # Clock period comes exclusively from --clock_period; it is a fixed constraint
    # and is never modified by the evolution process or the AI agent.
    ppa_cfg["clock_period_ps"] = args.clock_period

    # Resolve the active ppa_mode. Default is "fast" — codegen_main only.
    ppa_mode = str(evo_cfg.get("ppa_mode", "fast")).lower()
    if ppa_mode not in ("fast", "medium", "slow", "slowest"):
        console.print(f"[red]ERROR:[/] invalid ppa_mode={ppa_mode!r}. Expected fast|medium|slow|slowest.")
        return 1
    evo_cfg["ppa_mode"] = ppa_mode
    if ppa_mode in ("medium", "slowest"):
        console.print(
            f"[yellow]ppa_mode={ppa_mode!r} is a placeholder; evaluation will run as 'fast' "
            "until Yosys/OpenROAD integration lands.[/]"
        )

    # Score tuning for scheduler evolution.
    # When pipeline_stages is fixed in ppa_constraints.yaml every candidate
    # produces the same stage count, making the stage term a constant offset
    # that cannot differentiate candidates.  Auto-zero stage_weight in that
    # case unless the user has explicitly set it in evolve_config.yaml.
    _pipeline_stages = ppa_cfg.get("pipeline_stages")
    _stage_weight_cfg = evo_cfg.get("stage_weight")
    if _pipeline_stages is not None and _stage_weight_cfg is None:
        # Stages fixed by config → stage term is constant for every candidate.
        # Set weight to 0 automatically so it doesn't pollute the score signal.
        _stage_weight_cfg = 0.0

    configure_scoring(
        stage_weight=_stage_weight_cfg,
        flop_weight=evo_cfg.get("power_weight", evo_cfg.get("reg_weight")),
        area_weight=evo_cfg.get("area_weight"),
        delay_weight=evo_cfg.get("delay_weight"),
        balance_weight=evo_cfg.get("balance_weight"),
        runtime_weight=evo_cfg.get("runtime_weight"),
        ref_stages=_pipeline_stages if _pipeline_stages else evo_cfg.get("ref_stages", 16),
        ref_flop_bits=evo_cfg.get("ref_flop_bits"),
        ref_area_um2=evo_cfg.get("ref_area_um2"),
        ref_clock_ps=args.clock_period,
        ref_timeout_s=args.benchmark_timeout,
    )

    # ── Output directory ──────────────────────────────────────────────────────
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_dir = PROJECT_ROOT / "results" / ts
    output_dir.mkdir(parents=True, exist_ok=True)

    console.rule("[bold cyan]AlphaEvolve-XLS")
    console.print(f"  Output directory : [green]{output_dir}[/]")
    console.print(f"  XLS source       : [green]{args.xls_src}[/]")
    console.print(f"  Iterations       : [green]{args.iterations}[/]")
    console.print(f"  AI backend       : [green]{evo_cfg['ai_backend']} / {evo_cfg['ai_model']}[/]")
    console.print(f"  Mutation targets : [green]{evo_cfg['mutation_types']}[/]")
    console.print(f"  Clock period     : [green]{args.clock_period} ps[/]")
    console.print(f"  Pipeline stages  : [green]{'fixed → ' + str(_pipeline_stages) if _pipeline_stages else 'free (scheduler decides)'}[/]")
    console.print(f"  PPA mode         : [green]{ppa_mode}[/]")
    if _pipeline_stages is not None and _ppa_mod.STAGE_WEIGHT == 0.0:
        console.print(
            "  [dim]stage_weight auto-zeroed: pipeline_stages is fixed, so the stage term "
            "is a constant across all candidates and cannot aid ranking.[/]"
        )
    console.print(
        "  Score formula    : "
        f"[green]"
        f"(stg/{_ppa_mod.REF_STAGES})×{_ppa_mod.STAGE_WEIGHT} "
        f"+ (dly/{_ppa_mod.REF_CLOCK_PS}ps)×{_ppa_mod.DELAY_WEIGHT} "
        f"+ bal_cv×{_ppa_mod.BALANCE_WEIGHT} "
        f"+ (area/{_ppa_mod.REF_AREA_UM2:.0f}um²)×{_ppa_mod.AREA_WEIGHT} "
        f"+ (flp/{_ppa_mod.REF_FLOP_BITS})×{_ppa_mod.FLOP_WEIGHT} "
        f"+ (rt/{_ppa_mod.REF_TIMEOUT_S:.0f}s)×{_ppa_mod.RUNTIME_WEIGHT}"
        f"[/]"
    )
    console.print()

    # ── Design files ──────────────────────────────────────────────────────────
    design_files = [Path(args.input_file)] + [Path(d) for d in args.extra_designs]
    for d in design_files:
        if not d.exists():
            console.print(f"[red]ERROR:[/] Design file not found: {d}")
            return 1

    # ── XLS tool wrappers ─────────────────────────────────────────────────────
    from xls_tools.build import XLSBuilder
    from xls_tools.pipeline import XLSPipeline

    xls_src = Path(args.xls_src)
    builder = XLSBuilder(
        xls_src=xls_src,
        bazel_jobs=evo_cfg.get("bazel_jobs", 8),
    )

    # Determine binary paths
    bazel_bin = xls_src / "bazel-bin" / "xls" / "tools"
    prebuilt = Path(args.xls_prebuilt) if args.xls_prebuilt else None

    pipeline = XLSPipeline(
        prebuilt_bin_dir=prebuilt,
        bazel_bin_dir=bazel_bin if builder.is_built("codegen_main") else None,
        dslx_stdlib_path=xls_src / "xls" / "dslx" / "stdlib",
        benchmark_timeout=args.benchmark_timeout,
    )

    # ── Check for leftover .bak (previous run crashed before restore) ────────
    from alphaevolve.evaluator import MUTATION_TARGETS
    for _mt, (_rel, _) in MUTATION_TARGETS.items():
        _target_file = xls_src / _rel
        _bak_file = _target_file.with_suffix(_target_file.suffix + ".bak")
        if _bak_file.exists():
            console.print(
                f"[bold yellow]⚠ WARNING:[/] Found leftover backup: {_bak_file.name}\n"
                f"  A previous run crashed before restoring the original source.\n"
                f"  Auto-restoring from backup now..."
            )
            _target_file.write_text(_bak_file.read_text(encoding="utf-8"), encoding="utf-8")
            _bak_file.unlink()
            console.print(f"  [green]✓[/] Restored {_target_file.name} from backup.")

    # benchmark_main is only needed for ppa_mode in ("slow", "slowest").
    needs_benchmark_main = ppa_mode in ("slow", "slowest")

    # ── Ensure static tools are built once (benchmark_main, etc.) ────────────
    bm_path = xls_src / "bazel-bin" / "xls" / "dev_tools" / "benchmark_main"
    if needs_benchmark_main:
        if not bm_path.exists():
            console.print("[yellow]benchmark_main not found — building static targets (one-time, may take a while)...[/]")
            static_result = builder.build_static()
            if not static_result.success:
                console.print("[red]WARNING:[/] benchmark_main build failed — area metrics unavailable.")
                console.print(f"  [dim]{static_result.stderr[-300:]}[/]")
            else:
                console.print(f"[green]✓[/] Static tools built in {static_result.duration_seconds:.0f}s")
        else:
            console.print(f"[dim]benchmark_main already built, skipping static build[/]")
    else:
        console.print(
            f"[dim]Skipping benchmark_main build (ppa_mode={ppa_mode}); "
            "PPA will come from codegen_main block_metrics.[/]"
        )

    # ── Check binary availability ─────────────────────────────────────────────
    bootstrap_missing = any(
        not builder.is_built(tool)
        for tool in ("codegen_main", "opt_main", "ir_converter_main")
    )
    if bootstrap_missing:
        console.print("[yellow]Building XLS runtime tools required for the evolution loop...[/]")
        static_result = builder.build_bootstrap(include_benchmark_main=False)
        if not static_result.success:
            console.print("[red]WARNING:[/] XLS runtime bootstrap build failed.")
            console.print(f"  [dim]{static_result.stderr[-300:]}[/]")
        else:
            console.print(f"[green]✓[/] XLS runtime tools built in {static_result.duration_seconds:.0f}s")

    if not all(builder.is_built(tool) for tool in ("codegen_main", "opt_main", "ir_converter_main")):
        if prebuilt and (prebuilt / "codegen_main").exists():
            console.print("[yellow]⚠ XLS not yet built from source. Using pre-built binary.[/]")
            console.print("[yellow]  Source build required for algorithm mutation.[/]")
            console.print(f"[dim]  Run: cd {xls_src} && bazel build -c opt //xls/tools:...[/]")
            if not args.dry_run:
                console.print("[red]  Cannot evolve algorithms without a source build.[/]")
                return 1
        else:
            console.print("[red]ERROR:[/] No XLS binary found (neither built nor pre-built).")
            console.print("  Run the setup script or Docker to build XLS first.")
            return 1

    # ── Dry run: just validate the pipeline ───────────────────────────────────
    if args.dry_run:
        os.environ["XLS_AGENT_DRY_RUN"] = "1"
        console.print("[bold yellow]DRY RUN — testing pipeline only (no AI calls)[/]")
        # Only rebuild if the current binary doesn't recognise --scheduling_strategy=agent.
        # The probe exits in ~50 ms, so subsequent dry_runs skip the build entirely.
        if not builder.supports_agent_strategy():
            console.print("[dim]Agent strategy not in binary — rebuilding (incremental, fast)...[/]")
            dry_build = builder.build(targets=XLSBuilder.AGENT_ITERATION_TARGETS)
            if not dry_build.success:
                console.print(f"[red]Dry-run build failed:[/]\n{dry_build.stderr[-500:]}")
                os.environ.pop("XLS_AGENT_DRY_RUN", None)
                return 1
            console.print(f"[green]✓[/] Agent scheduler built in {dry_build.duration_seconds:.0f}s")
        else:
            console.print("[dim]Agent strategy already in binary, skipping rebuild[/]")
        _per_design = ppa_cfg.get("per_design", {})
        for design in design_files:
            cstr = {**ppa_cfg, **_per_design.get(design.stem, {})}
            run_dir = output_dir / "dry_run" / design.stem
            result = pipeline.run(
                dslx_file=design,
                output_dir=run_dir,
                clock_period_ps=cstr.get("clock_period_ps", 1000),
                delay_model=cstr.get("delay_model", "unit"),
                generator=cstr.get("generator", "pipeline"),
                ppa_mode=ppa_mode,
            )
            if result.success:
                from alphaevolve.ppa_metrics import extract_ppa
                ppa = extract_ppa(
                    schedule_path=result.schedule_path,
                    verilog_path=result.verilog_path,
                    block_metrics_path=result.block_metrics_path,
                    benchmark_output=result.benchmark_output,
                )
                bm_src = "benchmark_main" if result.benchmark_output else "schedule textproto"
                console.print(
                    f"  [green]✓[/] {design.stem}: "
                    f"{ppa.num_stages} stages, "
                    f"{ppa.effective_flop_count} flops, "
                    f"crit_path={ppa.critical_path_ps}ps, "
                    f"area={ppa.total_area_um2:.1f}um², "
                    f"score={ppa.score:.4f}  [dim]({bm_src})[/]"
                )
            else:
                console.print(f"  [red]✗[/] {design.stem}: {result.stderr[:200]}")
        os.environ.pop("XLS_AGENT_DRY_RUN", None)
        console.print("[bold green]Dry run complete.[/]")
        return 0

    # ── Initialize evolution components ───────────────────────────────────────
    from alphaevolve.database import CandidateDB
    from alphaevolve.sampler import Sampler
    from alphaevolve.evaluator import Evaluator, MUTATION_TARGETS
    from alphaevolve.islands import IslandManager
    from alphaevolve.ppa_metrics import PPAMetrics

    db_path = output_dir / "candidates_db.sqlite"
    if db_path.exists():
        # Archive the old database so the new run starts clean.
        # Previous results are preserved in the archive file; the Top-3
        # table and evolution_log.csv will only reflect the current run.
        from datetime import datetime as _dt
        _ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        _archive = db_path.with_name(f"candidates_db_{_ts}.sqlite")
        db_path.rename(_archive)
        console.print(
            f"  [yellow]⚠ Output directory already contains a previous run.[/]\n"
            f"  [dim]Old database archived → {_archive.name}[/]\n"
            f"  [dim]Starting fresh — results below are from this run only.[/]"
        )
    db = CandidateDB(db_path)

    sampler = Sampler(
        backend=evo_cfg["ai_backend"],
        model=evo_cfg["ai_model"],
        api_key=os.environ.get("OPENAI_API_KEY", ""),
    )

    evaluator = Evaluator(
        xls_src=xls_src,
        builder=builder,
        pipeline=pipeline,
        design_files=design_files,
        ppa_constraints=ppa_cfg,
        output_dir=output_dir / "eval_runs",
        ppa_mode=ppa_mode,
    )

    # --num_islands 1  → single island, linear evolution (no rotation)
    # --island_id N    → pin all iterations to island N (useful for debugging)
    n_islands = args.num_islands if args.num_islands is not None else evo_cfg.get("num_islands", 4)
    mutation_types = list(evo_cfg.get("mutation_types", ["agent_scheduler"]))
    if n_islands == 1 and len(mutation_types) > 1:
        selected = mutation_types[0]
        console.print(
            "[yellow]Single-island mode uses one mutation target only; "
            f"selecting '{selected}'. Use --mutation_target to choose another.[/]"
        )
        mutation_types = [selected]
    island_mgr = IslandManager(
        db=db,
        num_islands=n_islands,
        migration_interval=evo_cfg.get("migration_interval", 5),
        mutation_types=mutation_types,
        pinned_island_id=args.island_id,  # None = normal round-robin
    )

    # Save run config
    db.set_meta("config", {"ppa": ppa_cfg, "evo": evo_cfg, "args": vars(args)})
    db.set_meta("start_time", datetime.now(timezone.utc).isoformat())

    def build_reference_source_bundle(mutation_type: str) -> str:
        refs: list[tuple[str, str]] = []
        if mutation_type == "agent_scheduler":
            refs = [
                ("Current standalone scheduler", "xls/scheduling/agent_generated_scheduler.cc"),
                ("SDC scheduler", "xls/scheduling/sdc_scheduler.cc"),
                ("Min-cut scheduler", "xls/scheduling/min_cut_scheduler.cc"),
                ("Scheduler dispatch", "xls/scheduling/run_pipeline_schedule.cc"),
                ("Scheduling options", "xls/scheduling/scheduling_options.h"),
            ]
        else:
            refs = [("SDC scheduler", "xls/scheduling/sdc_scheduler.cc")]

        parts: list[str] = []
        for title, rel_path in refs:
            path = xls_src / rel_path
            if path.exists():
                parts.append(f"=== {title} ({rel_path}) ===\n{path.read_text(encoding='utf-8')}")
        return "\n\n".join(parts)

    best_score = float("inf")
    best_candidate_id = None

    # ── Optional per-design baseline benchmark context ─────────────────────────
    # If a design folder contains  <design_stem>_benchmark.txt  (generated by any
    # XLS benchmark_main run, regardless of scheduler), its contents are injected
    # into the AI prompt as a reference target.  Completely optional — missing
    # files are silently skipped.
    baseline_benchmark_context: str = ""
    _baseline_parts: list[str] = []
    for design in design_files:
        bm_file = design.parent / f"{design.stem}_benchmark.txt"
        if bm_file.exists():
            _baseline_parts.append(
                f"=== Baseline benchmark_main output for {design.stem} ===\n"
                + bm_file.read_text(encoding="utf-8").strip()
            )
    if _baseline_parts:
        baseline_benchmark_context = "\n\n".join(_baseline_parts)
        loaded = [d.stem for d in design_files
                  if (d.parent / f"{d.stem}_benchmark.txt").exists()]
        console.print(f"[dim]  Loaded baseline benchmark context: {', '.join(loaded)}[/]")

    # ── Evolution loop ─────────────────────────────────────────────────────────

    console.rule("[bold cyan]Starting Evolution Loop")

    for iteration in range(args.iterations):
        island = island_mgr.select_island(iteration)
        parent = island_mgr.select_parent(island)

        parent_score = parent.ppa_score if parent else float("inf")
        parent_stages = parent.num_stages if parent else 0
        parent_regs = parent.pipeline_reg_bits if parent else 0
        parent_delay = parent.max_stage_delay_ps if parent else 0

        mutation_type = island.mutation_type
        target_file_rel, signature = MUTATION_TARGETS[mutation_type]

        # Get current function source
        current_source = (xls_src / target_file_rel).read_text(encoding="utf-8")
        reference_source_bundle = build_reference_source_bundle(mutation_type)

        mutation_instruction = island_mgr.mutation_instruction_for(island, iteration)

        _best_label = f"{best_score:.4f}" if best_score < float("inf") else "none"
        console.print(
            f"\n[bold]Iteration {iteration + 1}/{args.iterations}[/] "
            f"Island {island.id} | target=[cyan]{mutation_type}[/] | "
            f"best_score={_best_label}"
        )

        max_retries = evo_cfg.get("max_build_retries", 3)
        last_compile_error: str | None = None
        result = None

        # Per-iteration attempt log — written for every attempt so post-run
        # analysis can replay exactly what the AI generated and what failed.
        attempt_log_path = (
            evaluator.output_dir
            / f"iter{iteration:04d}_island{island.id}_attempts.log"
        )
        attempt_log_lines: list[str] = []

        for attempt in range(1, max_retries + 1):
            attempt_label = f"attempt {attempt}/{max_retries}" if attempt > 1 else ""

            # ── Sample new algorithm from AI ──────────────────────────────────
            try:
                generated_code = sampler.sample(
                    mutation_target=mutation_type,
                    mutation_instruction=mutation_instruction,
                    current_source=current_source,
                    reference_source_bundle=reference_source_bundle,
                    best_score=best_score if best_score < float("inf") else None,
                    best_num_stages=parent_stages,
                    best_reg_bits=parent_regs,
                    best_delay_ps=parent_delay,
                    parent_score=parent_score,
                    parent_num_stages=parent_stages,
                    parent_reg_bits=parent_regs,
                    parent_delay_ps=parent_delay,
                    clock_period_ps=ppa_cfg.get("clock_period_ps", 1000),
                    pipeline_stages=ppa_cfg.get("pipeline_stages"),
                    baseline_benchmark_context=baseline_benchmark_context or None,
                    compile_error=last_compile_error,   # None on first attempt
                    target_file_path=target_file_rel,
                )
            except Exception as e:
                log.error(f"AI sampling failed: {e}")
                attempt_log_lines.append(
                    f"=== Attempt {attempt}/{max_retries} — AI SAMPLING FAILED ===\n"
                    f"Error: {e}\n"
                )
                break   # sampling itself failed — no point retrying

            # ── Evaluate (patch → build → run → score) ────────────────────────
            # on_stage_start  → updates the spinner text so the user knows which
            #                   stage is currently blocking + writes to stage log
            # on_stage        → prints a completion line above the spinner
            # Both also append to a real-time log file the user can tail -f.
            _STAGE_ICONS = {
                "ok":      "[green]✓[/]",
                "failed":  "[red]✗[/]",
                "skipped": "[dim]↷[/]",
                "timeout": "[yellow]⏱[/]",
                "no-ppa":  "[yellow]?[/]",
            }

            _stage_log_path = (
                evaluator.output_dir
                / f"iter{iteration:04d}_island{island.id}_stages.log"
            )
            _stage_log_lines: list[str] = []

            def _slog(msg: str) -> None:
                import datetime as _dt
                ts = _dt.datetime.now().strftime("%H:%M:%S")
                _stage_log_lines.append(f"[{ts}] {msg}")
                _stage_log_path.write_text(
                    "\n".join(_stage_log_lines) + "\n", encoding="utf-8"
                )

            def _on_stage_start(name: str, extra: str = "") -> None:
                extra_str = f"  ({extra})" if extra else ""
                _spinner_status.update(
                    f"[yellow]▷ {name}{extra_str}[/]"
                )
                _slog(f"START  {name}{extra_str}")

            def _on_stage(name: str, st: str, duration_s: float, extra: str = "") -> None:
                icon = _STAGE_ICONS.get(st, "[dim]·[/]")
                extra_str = f"  [dim]{extra}[/]" if extra else ""
                console.print(
                    f"    {icon} {name:<22} {duration_s:5.1f}s{extra_str}",
                    highlight=False,
                )
                _slog(f"END    {name} [{st}] {duration_s:.1f}s{('  ' + extra) if extra else ''}")
                _spinner_status.update("[dim]waiting for next stage...[/]")

            _slog(f"attempt={attempt}/{max_retries}  iteration={iteration+1}  island={island.id}")
            _retry_tag = f" (retry {attempt})" if attempt > 1 else ""
            with console.status(f"[yellow]▷ compile{_retry_tag}...[/]") as _spinner_status:
                result = evaluator.evaluate(
                    iteration=iteration,
                    island_id=island.id,
                    parent_id=parent.id if parent else None,
                    mutation_type=mutation_type,
                    generated_code=generated_code,
                    on_stage_start=_on_stage_start,
                    on_stage=_on_stage,
                )

            c = result.candidate

            # ── Log this attempt to disk (all attempts, win or lose) ──────────
            attempt_log_lines.append(
                f"=== Attempt {attempt}/{max_retries} "
                f"| status={c.build_status} "
                f"| build={c.build_duration_s:.1f}s ===\n"
                f"--- Generated code ---\n{generated_code}\n"
                f"--- Notes / error ---\n{c.notes or '(none)'}\n"
            )
            attempt_log_path.write_text(
                "\n".join(attempt_log_lines), encoding="utf-8"
            )

            if c.build_status == "success":
                break   # ✓ compiled and scheduled — exit retry loop

            # ── run_failed: scheduler compiled but produced no feasible schedule ─
            # Don't retry as a "compile error" — the C++ was valid; the schedule
            # is infeasible for this clock period / design.  Stop retrying now so
            # we don't waste two more AI calls mislabelled as compile fixes.
            if c.build_status == "run_failed":
                break

            # ── build_failed: compiler/linker rejected the C++ ───────────────
            # Build the error string fed back to the AI on the next attempt.
            # Keep more context than just error:/note: lines so the AI can
            # locate the offending construct (include surrounding lines too).
            raw_error = c.notes or "Build failed (no error details)"
            if "error:" in raw_error:
                error_lines = raw_error.splitlines()
                # Keep every line that carries a clang error, note, or the
                # immediately surrounding context (file:line: patterns).
                useful: list[str] = []
                for i, ln in enumerate(error_lines):
                    if any(tag in ln for tag in ("error:", "note:", "warning:", " ^ ")):
                        # Include one line of context before and after each hit
                        for j in range(max(0, i - 1), min(len(error_lines), i + 2)):
                            if error_lines[j] not in useful:
                                useful.append(error_lines[j])
                last_compile_error = "\n".join(useful[:60])
            else:
                last_compile_error = raw_error[:3000]

            if attempt < max_retries:
                console.print(
                    f"  [yellow]⟳ compile failed (attempt {attempt}/{max_retries}), "
                    f"sending error to AI for retry...[/]"
                )

        if result is None:
            continue   # AI sampling failed entirely, skip iteration

        c = result.candidate
        island_mgr.record(c, island)

        if c.build_status == "success":
            ppa = result.ppa
            # Build normalized score breakdown string.
            t = ppa.normalized_terms()
            sw  = _ppa_mod.STAGE_WEIGHT
            fw  = _ppa_mod.FLOP_WEIGHT
            aw  = _ppa_mod.AREA_WEIGHT
            dw  = _ppa_mod.DELAY_WEIGHT
            bw  = _ppa_mod.BALANCE_WEIGHT
            rw  = _ppa_mod.RUNTIME_WEIGHT
            parts = [f"stg({t['stage']:.3f})×{sw}"]
            if dw:
                parts.append(f"dly({t['delay']:.3f})×{dw}")
            if bw:
                parts.append(f"bal({t['balance']:.3f})×{bw}")
            if aw:
                parts.append(f"area({t['area']:.3f})×{aw}")
            if fw:
                parts.append(f"flp({t['flop']:.3f})×{fw}")
            if rw:
                parts.append(f"rt({t['runtime']:.3f})×{rw}")
            formula = " + ".join(parts)
            console.print(
                f"  [green]✓[/] "
                f"stages=[cyan]{c.num_stages}[/]  "
                f"area=[cyan]{ppa.total_area_um2:.0f}[/]um²  "
                f"max_stg_dly=[cyan]{ppa.max_stage_delay_ps}[/]ps  "
                f"bal_cv=[cyan]{t['balance']:.3f}[/]  "
                f"regs=[cyan]{c.pipeline_reg_bits}[/]  "
                f"runtime=[cyan]{ppa.scheduler_runtime_s:.1f}[/]s  "
                f"score=[bold cyan]{c.ppa_score:.4f}[/]  "
                f"| build={c.build_duration_s:.0f}s",
                highlight=False,
            )
            console.print(
                f"    [dim]score = {formula} = {c.ppa_score:.4f}[/]",
                highlight=False,
            )
        elif c.build_status == "run_failed":
            console.print(
                f"  [yellow]~[/] schedule infeasible for this clock period — "
                f"no design met constraint | build={c.build_duration_s:.0f}s"
            )
        else:
            console.print(
                f"  [red]✗[/] build failed | build={c.build_duration_s:.0f}s"
            )

        if c.build_status == "success" and c.ppa_score < best_score:
            best_score = c.ppa_score
            best_candidate_id = c.id
            console.print(
                f"  [bold green]★ New best! score={best_score:.4f} "
                f"({c.num_stages} stages, {c.pipeline_reg_bits} reg bits)[/]"
            )
            # Save best diff immediately
            (output_dir / "best_algorithm.patch").write_text(c.source_diff)

        island_mgr.maybe_migrate(iteration)


    # ── Save final results ─────────────────────────────────────────────────────
    console.rule("[bold cyan]Evolution Complete")

    # Evolution log CSV
    db.to_csv(output_dir / "evolution_log.csv")

    # PPA report
    best_candidates = db.best(n=3)
    report = {
        "best_score": best_score,
        "total_evaluated": len(db.all_successful()),
        "best_candidates": [
            {
                "id": c.id,
                "iteration": c.iteration,
                "mutation_type": c.mutation_type,
                "num_stages": c.num_stages,
                "pipeline_reg_bits": c.pipeline_reg_bits,
                "max_stage_delay_ps": c.max_stage_delay_ps,
                "ppa_score": c.ppa_score,
            }
            for c in best_candidates
        ],
    }
    (output_dir / "ppa_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )

    # Summary table
    table = Table(title="Top 3 Candidates")
    table.add_column("ID", style="dim")
    table.add_column("Iter")
    table.add_column("Target")
    table.add_column("Stages", style="cyan")
    table.add_column("Reg Bits", style="cyan")
    table.add_column("Score", style="bold green")
    for c in best_candidates:
        table.add_row(
            str(c.id), str(c.iteration), c.mutation_type,
            str(c.num_stages), str(c.pipeline_reg_bits), f"{c.ppa_score:.4f}",
        )
    console.print(table)

    console.print(f"\n[bold green]Results saved to:[/] {output_dir}")
    console.print(f"  evolution_log.csv   — all iteration metrics")
    console.print(f"  ppa_report.json     — final PPA summary")
    if best_candidates:
        console.print(f"  best_algorithm.patch — patch target recorded inside unified diff")
    else:
        console.print(f"  best_algorithm.patch — created when a successful candidate improves the best score")
    console.print(f"  candidates_db.sqlite — full SQLite database")

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
