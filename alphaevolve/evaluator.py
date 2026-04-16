"""
alphaevolve/evaluator.py
────────────────────────
Orchestrates one full evaluation cycle for a candidate:
  backup → apply generated code → build → run XLS pipeline → extract PPA → restore on failure

The Evaluator operates on a single MUTATION TARGET: one function in sdc_scheduler.cc.
It splices the AI-generated function into the source file using a marker-based approach
(locate the function by signature, replace its body), then diffs against the original.
"""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from alphaevolve.database import Candidate
from alphaevolve.ppa_metrics import extract_ppa, PPAMetrics
from xls_tools.build import XLSBuilder, BuildResult
from xls_tools.pipeline import XLSPipeline, PipelineResult


# ── Mutation target registry ───────────────────────────────────────────────────
# Maps short name → (file_relative_to_xls_src, function_signature_prefix)
MUTATION_TARGETS: dict[str, tuple[str, str]] = {
    "sdc_objective": (
        "xls/scheduling/sdc_scheduler.cc",
        "void SDCSchedulingModel::SetObjective(",
    ),
    "delay_constraints": (
        "xls/scheduling/sdc_scheduler.cc",
        "absl::flat_hash_map<Node*, std::vector<Node*>>\nComputeCombinationalDelayConstraints(",
    ),
    "min_cut": (
        "xls/scheduling/min_cut_scheduler.cc",
        "absl::StatusOr<ScheduleCycleMap> MinCutScheduler(",
    ),
}


@dataclass
class EvalResult:
    candidate: Candidate
    ppa: PPAMetrics
    build_result: BuildResult | None = None
    pipeline_result: PipelineResult | None = None
    error: str = ""


class Evaluator:
    """Evaluates one AI-generated algorithm variant."""

    def __init__(
        self,
        xls_src: Path | str,
        builder: XLSBuilder,
        pipeline: XLSPipeline,
        design_files: list[Path | str],
        ppa_constraints: dict,
        output_dir: Path | str,
    ):
        self.xls_src = Path(xls_src)
        self.builder = builder
        self.pipeline = pipeline
        self.design_files = [Path(f) for f in design_files]
        self.ppa_constraints = ppa_constraints
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def evaluate(
        self,
        iteration: int,
        island_id: int,
        parent_id: int | None,
        mutation_type: str,
        generated_code: str,
    ) -> EvalResult:
        """
        Run a full evaluation of a generated C++ algorithm variant.
        Returns an EvalResult with the candidate and PPA metrics.
        """
        target_file_rel, signature = MUTATION_TARGETS[mutation_type]
        target_file = self.xls_src / target_file_rel
        t_start = time.monotonic()

        # ── Read original source ──────────────────────────────────────────────
        original_source = target_file.read_text(encoding="utf-8")

        # ── Splice generated code into source ─────────────────────────────────
        new_source = self._splice_function(original_source, signature, generated_code)
        if new_source is None:
            return self._make_failed(
                iteration, island_id, parent_id, mutation_type,
                target_file_rel, generated_code, original_source, original_source,
                "run_failed", f"Could not locate function '{signature}' in {target_file_rel}",
                t_start,
            )

        # Compute diff for database storage
        diff = self._unified_diff(original_source, new_source, target_file_rel)

        # ── Backup + apply ────────────────────────────────────────────────────
        self.builder.backup(target_file)
        self.builder.apply(target_file, new_source)

        # ── Build ─────────────────────────────────────────────────────────────
        build_result = self.builder.build()
        if not build_result.success:
            self.builder.restore(target_file)
            return self._make_failed(
                iteration, island_id, parent_id, mutation_type,
                target_file_rel, generated_code, diff,
                "build_failed",
                f"Bazel build failed in {build_result.duration_seconds:.1f}s:\n{build_result.stderr[-2000:]}",
                t_start, build_result=build_result,
            )

        # ── Run XLS pipeline on all benchmark designs ─────────────────────────
        aggregate_ppa = self._run_pipeline_on_designs(iteration, island_id)

        # ── Restore original (we keep the best separately via diffs) ──────────
        self.builder.restore(target_file)
        self.builder.cleanup_backups()

        total_duration = time.monotonic() - t_start

        candidate = Candidate(
            iteration=iteration,
            island_id=island_id,
            parent_id=parent_id,
            mutation_type=mutation_type,
            target_file=target_file_rel,
            source_diff=diff,
            generated_code=generated_code,
            build_status="success" if aggregate_ppa.feasible else "run_failed",
            num_stages=aggregate_ppa.num_stages,
            pipeline_reg_bits=aggregate_ppa.pipeline_reg_bits,
            max_stage_delay_ps=aggregate_ppa.max_stage_delay_ps,
            min_clock_period_ps=aggregate_ppa.min_clock_period_ps,
            ppa_score=aggregate_ppa.score,
            build_duration_s=build_result.duration_seconds,
            total_duration_s=total_duration,
        )
        return EvalResult(
            candidate=candidate,
            ppa=aggregate_ppa,
            build_result=build_result,
        )

    # ── Internal helpers ───────────────────────────────────────────────────────

    def evaluate_baseline(self, mutation_type: str) -> EvalResult:
        """
        Evaluate the UNMODIFIED sdc_scheduler.cc (no AI mutation) and return a
        baseline Candidate with iteration=-1.  Called once before the evolution loop
        to seed islands, so the AI always has a valid compilable parent to build on.
        """
        target_file_rel, _ = MUTATION_TARGETS[mutation_type]
        target_file = self.xls_src / target_file_rel
        original_source = target_file.read_text(encoding="utf-8")
        t_start = time.monotonic()

        aggregate_ppa = self._run_pipeline_on_designs(iteration=-1, island_id=-1)

        candidate = Candidate(
            iteration=-1,
            island_id=-1,
            parent_id=None,
            mutation_type=mutation_type,
            target_file=target_file_rel,
            source_diff="",
            generated_code="(baseline — original XLS code)",
            build_status="success" if aggregate_ppa.feasible else "run_failed",
            num_stages=aggregate_ppa.num_stages,
            pipeline_reg_bits=aggregate_ppa.pipeline_reg_bits,
            max_stage_delay_ps=aggregate_ppa.max_stage_delay_ps,
            min_clock_period_ps=aggregate_ppa.min_clock_period_ps,
            ppa_score=aggregate_ppa.score,
            build_duration_s=0.0,
            total_duration_s=time.monotonic() - t_start,
            notes="Baseline: original unmodified sdc_scheduler.cc",
        )
        return EvalResult(candidate=candidate, ppa=aggregate_ppa)

    def _run_pipeline_on_designs(self, iteration: int, island_id: int) -> PPAMetrics:

        """
        Run the XLS pipeline on all benchmark designs and aggregate PPA.
        Primary source: benchmark_main (area_um2, critical_path_ps, pipeline_flops)
        """
        total_stages = 0
        total_flops  = 0
        total_area   = 0.0
        max_delay    = 0
        max_min_clock = 0
        any_feasible = False

        for design in self.design_files:
            run_dir = self.output_dir / f"iter{iteration:04d}_island{island_id}" / design.stem
            result = self.pipeline.run(
                dslx_file=design,
                output_dir=run_dir,
                clock_period_ps=self.ppa_constraints.get("clock_period_ps", 1000),
                pipeline_stages=self.ppa_constraints.get("pipeline_stages"),
                delay_model=self.ppa_constraints.get("delay_model", "unit"),
                generator=self.ppa_constraints.get("generator", "pipeline"),
            )
            if not result.success:
                continue

            ppa = extract_ppa(
                schedule_path=result.schedule_path,
                verilog_path=result.verilog_path,
                block_metrics_path=result.block_metrics_path,
                benchmark_output=result.benchmark_output,   # ← primary PPA source
            )
            if not ppa.feasible:
                continue

            any_feasible  = True
            total_stages += ppa.num_stages
            total_flops  += ppa.effective_flop_count
            total_area   += ppa.total_area_um2
            max_delay     = max(max_delay, ppa.critical_path_ps)
            max_min_clock = max(max_min_clock, ppa.min_clock_period_ps)

        if not any_feasible:
            return PPAMetrics(feasible=False)

        agg = PPAMetrics(
            num_stages=total_stages,
            flop_count=total_flops,
            total_area_um2=total_area,
            total_pipeline_flops=total_flops,
            critical_path_ps=max_delay,
            min_clock_period_ps=max_min_clock,
            feasible=True,
        )
        agg._compute()
        return agg

    @staticmethod
    def _splice_function(source: str, signature: str, new_body: str) -> str | None:
        """
        Replace the body of a C++ function identified by `signature` with `new_body`.

        Strategy: find the signature, then find the matching `{...}` block using
        brace counting, and replace the entire function (signature + body).
        """
        # Normalize the signature for searching (collapse whitespace)
        sig_normalized = re.sub(r"\s+", r"\\s+", re.escape(signature.strip()))
        match = re.search(sig_normalized, source, re.DOTALL)
        if not match:
            # Try a simpler search on the first line of the signature
            first_line = signature.strip().split("\n")[0].split("(")[0].strip()
            match = re.search(re.escape(first_line), source)
            if not match:
                return None

        # Find the opening brace of the function body
        brace_start = source.find("{", match.start())
        if brace_start == -1:
            return None

        # Count braces to find the matching closing brace
        depth = 0
        brace_end = brace_start
        for i in range(brace_start, len(source)):
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
                if depth == 0:
                    brace_end = i
                    break

        # Reconstruct: keep everything before the function, inject new code, keep rest
        before = source[: match.start()]
        after = source[brace_end + 1 :]
        return before + new_body + "\n" + after

    @staticmethod
    def _unified_diff(original: str, modified: str, filename: str) -> str:
        orig_lines = original.splitlines(keepends=True)
        mod_lines = modified.splitlines(keepends=True)
        diff = difflib.unified_diff(
            orig_lines, mod_lines,
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            lineterm="",
        )
        return "".join(diff)

    def _make_failed(self, iteration, island_id, parent_id, mutation_type,
                     target_file_rel, generated_code, diff,
                     status, error, t_start, build_result=None) -> EvalResult:
        self.builder.restore()
        return EvalResult(
            candidate=Candidate(
                iteration=iteration,
                island_id=island_id,
                parent_id=parent_id,
                mutation_type=mutation_type,
                target_file=target_file_rel,
                source_diff=diff if isinstance(diff, str) else "",
                generated_code=generated_code,
                build_status=status,
                ppa_score=float("inf"),
                total_duration_s=time.monotonic() - t_start,
                notes=error,
            ),
            ppa=PPAMetrics(feasible=False),
            build_result=build_result,
            error=error,
        )
