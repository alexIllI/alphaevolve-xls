#!/usr/bin/env python3
"""
run.py — AlphaEvolve-XLS Main Entry Point
──────────────────────────────────────────
Evolves XLS scheduling algorithms to improve hardware design PPA
(Power/Performance/Area) using AI-generated C++ implementations.

Usage:
  python run.py --input_file designs/mac/mac.x --iterations 20

  python run.py \\
    --input_file designs/mac/mac.x \\
    --ppa_constraints configs/ppa_constraints.yaml \\
    --evolve_config configs/evolve_config.yaml \\
    --iterations 50 \\
    --output_dir results/mac_exp_001 \\
    --mutation_target sdc_objective \\
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
        choices=["sdc_objective", "delay_constraints", "min_cut"],
        default=None,
        help="Which XLS function to evolve. Overrides evolve_config.yaml mutation_types.",
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

    # CLI overrides
    if args.mutation_target:
        evo_cfg["mutation_types"] = [args.mutation_target]
    if args.backend:
        evo_cfg["ai_backend"] = args.backend
    if args.model:
        evo_cfg["ai_model"] = args.model

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
        bazel_bin_dir=bazel_bin if builder.is_built() else None,
        dslx_stdlib_path=xls_src / "xls" / "dslx" / "stdlib",  # for float32 etc.
    )

    # ── Ensure static tools are built once (benchmark_main, etc.) ────────────
    bm_path = xls_src / "bazel-bin" / "xls" / "dev_tools" / "benchmark_main"
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

    # ── Check binary availability ─────────────────────────────────────────────
    if not builder.is_built():
        if prebuilt and (prebuilt / "codegen_main").exists():
            console.print("[yellow]⚠ XLS not yet built from source. Using pre-built binary.[/]")
            console.print("[yellow]  Source build required for algorithm mutation.[/]")
            console.print(f"[dim]  Run: cd {xls_src} && bazel build -c opt //xls/tools:...[/]")
            if not args.dry_run:
                console.print("[red]  Cannot evolve algorithms without a source build.[/]")
                console.print("[yellow]  Use --dry_run to test the pipeline with pre-built binaries.[/]")
                return 1
        else:
            console.print("[red]ERROR:[/] No XLS binary found (neither built nor pre-built).")
            console.print("  Run the setup script or Docker to build XLS first.")
            return 1

    # ── Dry run: just validate the pipeline ───────────────────────────────────
    if args.dry_run:
        console.print("[bold yellow]DRY RUN — testing pipeline only (no AI calls)[/]")
        for design in design_files:
            run_dir = output_dir / "dry_run" / design.stem
            result = pipeline.run(
                dslx_file=design,
                output_dir=run_dir,
                clock_period_ps=ppa_cfg.get("clock_period_ps", 1000),
                delay_model=ppa_cfg.get("delay_model", "unit"),
                generator=ppa_cfg.get("generator", "pipeline"),
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
                    f"score={ppa.score:.0f}  [dim]({bm_src})[/]"
                )
            else:
                console.print(f"  [red]✗[/] {design.stem}: {result.stderr[:200]}")
        console.print("[bold green]Dry run complete.[/]")
        return 0

    # ── Initialize evolution components ───────────────────────────────────────
    from alphaevolve.database import CandidateDB
    from alphaevolve.sampler import Sampler
    from alphaevolve.evaluator import Evaluator, MUTATION_TARGETS
    from alphaevolve.islands import IslandManager
    from alphaevolve.ppa_metrics import PPAMetrics

    db_path = output_dir / "candidates_db.sqlite"
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
    )

    # --num_islands 1  → single island, linear evolution (no rotation)
    # --island_id N    → pin all iterations to island N (useful for debugging)
    n_islands = args.num_islands if args.num_islands is not None else evo_cfg.get("num_islands", 4)
    island_mgr = IslandManager(
        db=db,
        num_islands=n_islands,
        migration_interval=evo_cfg.get("migration_interval", 5),
        mutation_types=evo_cfg.get("mutation_types", ["sdc_objective"]),
        pinned_island_id=args.island_id,  # None = normal round-robin
    )

    # Save run config
    db.set_meta("config", {"ppa": ppa_cfg, "evo": evo_cfg, "args": vars(args)})
    db.set_meta("start_time", datetime.now(timezone.utc).isoformat())

    # ── Read baseline source ───────────────────────────────────────────────────
    sdc_src_path = xls_src / "xls" / "scheduling" / "sdc_scheduler.cc"
    sdc_source = sdc_src_path.read_text(encoding="utf-8")

    best_score = float("inf")
    best_candidate_id = None

    # ── Seed islands with baseline (original un-mutated code) ─────────────────
    console.rule("[bold cyan]Seeding Islands with Baseline PPA")
    console.print("  Evaluating original sdc_scheduler.cc (no AI mutation)...")

    baseline_by_mt: dict = {}  # mutation_type → EvalResult, computed once per mt
    for island in island_mgr.islands:
        mt = island.mutation_type
        if mt not in baseline_by_mt:
            with console.status(f"  [yellow]Baseline pipeline for mutation_type={mt}...[/]"):
                baseline = evaluator.evaluate_baseline(mt)
            baseline_by_mt[mt] = baseline
            if baseline.ppa.feasible:
                db.insert(baseline.candidate)  # correct method: insert(), not add_candidate()
                console.print(
                    f"  [green]✓[/] {mt} baseline: "
                    f"{baseline.candidate.num_stages} stages, "
                    f"crit_path={baseline.ppa.critical_path_ps}ps, "
                    f"area={baseline.ppa.total_area_um2:.1f}um², "
                    f"score={baseline.candidate.ppa_score:.0f}"
                )
            else:
                console.print(f"  [yellow]⚠[/] {mt} baseline pipeline failed — islands start cold")
        # Seed every island for this mt (not just the first one found)
        bl = baseline_by_mt[mt]
        if bl.ppa.feasible:
            island_mgr.record(bl.candidate, island)
            best_score = min(best_score, bl.candidate.ppa_score)

    console.print()
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

        mutation_instruction = island_mgr.mutation_instruction_for(island, iteration)

        console.print(
            f"\n[bold]Iteration {iteration + 1}/{args.iterations}[/] "
            f"Island {island.id} | target=[cyan]{mutation_type}[/] | "
            f"best_score={best_score:.0f}"
        )

        max_retries = evo_cfg.get("max_build_retries", 3)
        last_compile_error: str | None = None
        result = None

        for attempt in range(1, max_retries + 1):
            attempt_label = f"attempt {attempt}/{max_retries}" if attempt > 1 else ""

            # ── Sample new algorithm from AI ──────────────────────────────────
            try:
                generated_code = sampler.sample(
                    mutation_target=mutation_type,
                    mutation_instruction=mutation_instruction,
                    current_function_source=current_source,
                    sdc_scheduler_source=sdc_source,
                    best_score=best_score,
                    best_num_stages=parent_stages,
                    best_reg_bits=parent_regs,
                    best_delay_ps=parent_delay,
                    parent_score=parent_score,
                    parent_num_stages=parent_stages,
                    parent_reg_bits=parent_regs,
                    parent_delay_ps=parent_delay,
                    compile_error=last_compile_error,   # None on first attempt
                )
            except Exception as e:
                log.error(f"AI sampling failed: {e}")
                break   # sampling itself failed — no point retrying

            # ── Evaluate (patch → build → run → score) ────────────────────────
            status_label = f"[yellow]Building XLS{' (retry)' if attempt > 1 else ''}...[/]"
            with console.status(status_label):
                result = evaluator.evaluate(
                    iteration=iteration,
                    island_id=island.id,
                    parent_id=parent.id if parent else None,
                    mutation_type=mutation_type,
                    generated_code=generated_code,
                )

            c = result.candidate

            if c.build_status == "success":
                break   # ✓ compiled — exit retry loop

            # Extract compile error for next attempt
            last_compile_error = c.notes or "Build failed (no error details)"
            # Trim to the most useful part: just the clang errors
            if "error:" in last_compile_error:
                error_lines = [
                    l for l in last_compile_error.splitlines()
                    if "error:" in l or "note:" in l
                ]
                last_compile_error = "\n".join(error_lines[:30])

            if attempt < max_retries:
                console.print(
                    f"  [yellow]⟳ compile failed (attempt {attempt}/{max_retries}), "
                    f"sending error to AI for retry...[/]"
                )

        if result is None:
            continue   # AI sampling failed entirely, skip iteration

        c = result.candidate
        island_mgr.record(c, island)

        status_icon = "[green]✓[/]" if c.build_status == "success" else "[red]✗[/]"
        console.print(
            f"  {status_icon} status={c.build_status} | "
            f"stages={c.num_stages} regs={c.pipeline_reg_bits} "
            f"score={c.ppa_score:.0f} | "
            f"build={c.build_duration_s:.0f}s"
        )

        if c.build_status == "success" and c.ppa_score < best_score:
            best_score = c.ppa_score
            best_candidate_id = c.id
            console.print(
                f"  [bold green]★ New best! score={best_score:.0f} "
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
        "iterations": args.iterations,
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
            str(c.num_stages), str(c.pipeline_reg_bits), f"{c.ppa_score:.0f}",
        )
    console.print(table)

    console.print(f"\n[bold green]Results saved to:[/] {output_dir}")
    console.print(f"  evolution_log.csv   — all iteration metrics")
    console.print(f"  ppa_report.json     — final PPA summary")
    console.print(f"  best_algorithm.patch — apply to {xls_src}/xls/scheduling/sdc_scheduler.cc")
    console.print(f"  candidates_db.sqlite — full SQLite database")

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
