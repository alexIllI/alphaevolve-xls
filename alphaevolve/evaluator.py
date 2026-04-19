"""
alphaevolve/evaluator.py
────────────────────────
Orchestrates one full evaluation cycle for a candidate:
  backup → apply generated code → build → run XLS pipeline → extract PPA → restore on failure

The Evaluator operates on a single MUTATION TARGET: AgentGeneratedScheduler()
in xls/scheduling/agent_generated_scheduler.cc. It splices the AI-generated
function into the source file using a marker-based approach (locate the
function by signature, replace its body), then diffs against the original.
"""

from __future__ import annotations

import difflib
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from alphaevolve.database import Candidate
from alphaevolve.ppa_metrics import extract_ppa, PPAMetrics
from xls_tools.build import XLSBuilder, BuildResult
from xls_tools.pipeline import XLSPipeline, PipelineResult


LOG = logging.getLogger(__name__)


# ── Mutation target registry ───────────────────────────────────────────────────
# Only one mutation target is supported: the agent-generated scheduler.
# Maps short name → (file_relative_to_xls_src, function_signature_prefix)
MUTATION_TARGETS: dict[str, tuple[str, str]] = {
    "agent_scheduler": (
        "xls/scheduling/agent_generated_scheduler.cc",
        "absl::StatusOr<ScheduleCycleMap> AgentGeneratedScheduler(",
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
        ppa_mode: str = "fast",
    ):
        self.xls_src = Path(xls_src)
        self.builder = builder
        self.pipeline = pipeline
        self.design_files = [Path(f) for f in design_files]
        self.ppa_constraints = ppa_constraints
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.ppa_mode = ppa_mode

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
        sanitized_code, sanitization_notes = self._sanitize_generated_code(generated_code)
        if sanitization_notes:
            LOG.warning(
                "Sanitized AI output for %s: %s",
                mutation_type,
                ", ".join(sanitization_notes),
            )
        new_source = self._splice_function(original_source, signature, sanitized_code)
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

        build_result: BuildResult | None = None
        aggregate_ppa = PPAMetrics(feasible=False)
        error_msg = ""

        try:
            # ── Build ─────────────────────────────────────────────────────────
            build_result = self.builder.build(
                XLSBuilder.iteration_targets_for_mode(self.ppa_mode)
            )
            if not build_result.success:
                # Store the full stderr — truncation happens in run.py when
                # the error is fed back to the AI (where brevity matters).
                # Here we keep everything so post-run analysis has the full
                # clang/linker output available in the DB and attempt logs.
                error_msg = (
                    f"Bazel build failed in {build_result.duration_seconds:.1f}s:\n"
                    f"{build_result.stderr}"
                )
            else:
                # ── Run XLS pipeline on all benchmark designs ─────────────────
                aggregate_ppa, error_msg = self._run_pipeline_on_designs(
                    iteration, island_id
                )
        finally:
            # Always restore — even if build or pipeline throws (e.g. TimeoutExpired)
            self.builder.restore(target_file)
            self.builder.cleanup_backups()

        total_duration = time.monotonic() - t_start

        if build_result is not None and not build_result.success:
            return self._make_failed(
                iteration, island_id, parent_id, mutation_type,
                target_file_rel, generated_code, diff,
                "build_failed", error_msg,
                t_start, build_result=build_result,
            )

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
            max_stage_delay_ps=aggregate_ppa.critical_path_ps,
            min_clock_period_ps=aggregate_ppa.min_clock_period_ps,
            ppa_score=aggregate_ppa.score,
            build_duration_s=build_result.duration_seconds if build_result else 0.0,
            total_duration_s=total_duration,
            notes=error_msg,
        )
        return EvalResult(
            candidate=candidate,
            ppa=aggregate_ppa,
            build_result=build_result,
        )

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _run_pipeline_on_designs(
        self,
        iteration: int,
        island_id: int,
    ) -> tuple[PPAMetrics, str]:
        """
        Run the XLS pipeline on all benchmark designs and aggregate PPA.

        Returns (PPAMetrics, error_message). PPAMetrics.feasible=False when no
        design produced valid PPA (run_failed). error_message is non-empty when
        a pipeline stage timed out or all designs failed.

        Subprocess timeouts (e.g. AI-generated scheduler stuck in infinite loop)
        are caught here and converted into run_failed instead of crashing the run.
        """
        import subprocess as _sp

        total_stages = 0
        total_flops  = 0
        total_area   = 0.0
        max_delay    = 0
        max_min_clock = 0
        any_feasible = False
        timeout_errors: list[str] = []

        per_design_overrides = self.ppa_constraints.get("per_design", {})
        for design in self.design_files:
            cstr = {**self.ppa_constraints, **per_design_overrides.get(design.stem, {})}
            run_dir = self.output_dir / f"iter{iteration:04d}_island{island_id}" / design.stem
            try:
                result = self.pipeline.run(
                    dslx_file=design,
                    output_dir=run_dir,
                    clock_period_ps=cstr.get("clock_period_ps", 1000),
                    pipeline_stages=cstr.get("pipeline_stages"),
                    delay_model=cstr.get("delay_model", "unit"),
                    generator=cstr.get("generator", "pipeline"),
                    ppa_mode=self.ppa_mode,
                )
            except _sp.TimeoutExpired as exc:
                # AI-generated scheduler hung (infinite loop / excessive complexity).
                # Treat as run_failed for this design; continue to next design.
                msg = (
                    f"{design.stem}: pipeline stage timed out "
                    f"({exc.timeout:.0f}s) — scheduler likely too slow"
                )
                LOG.warning(msg)
                timeout_errors.append(msg)
                continue

            if not result.success:
                continue

            ppa = extract_ppa(
                schedule_path=result.schedule_path,
                verilog_path=result.verilog_path,
                block_metrics_path=result.block_metrics_path,
                benchmark_output=result.benchmark_output,
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
            err = "; ".join(timeout_errors) if timeout_errors else "no design met constraints"
            return PPAMetrics(feasible=False), err

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
        return agg, ""

    @staticmethod
    def _sanitize_generated_code(generated_code: str) -> tuple[str, list[str]]:
        text = generated_code.strip()
        notes: list[str] = []

        if text.startswith("```"):
            opening = re.match(r"^```[a-zA-Z0-9_+-]*\s*\n", text)
            if opening:
                text = text[opening.end():]
                notes.append("removed opening code fence")
            if text.endswith("```"):
                text = text[:-3]
                notes.append("removed closing code fence")
            text = text.strip()

        # Strip ALL #include lines — the AI must never emit them.
        # The original .cc file already has the necessary includes; any
        # #include in the AI output will land in the middle of the spliced
        # file and cause a compile error regardless of where they appear
        # (even after a leading comment, which the old prefix-only approach
        # could not catch).
        lines = text.splitlines()
        filtered_lines = [ln for ln in lines if not ln.strip().startswith("#include ")]
        if len(filtered_lines) < len(lines):
            notes.append("removed #include lines")
        # Also drop blank lines that were sandwiched between removed includes
        # at the very top (keeps the output clean).
        while filtered_lines and not filtered_lines[0].strip():
            filtered_lines.pop(0)
        text = "\n".join(filtered_lines).strip()

        namespace_match = re.match(r"^\s*namespace\s+xls\s*\{", text)
        if namespace_match:
            open_brace = text.find("{", namespace_match.start())
            depth = 0
            close_brace = -1
            for i in range(open_brace, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        close_brace = i
                        break
            if close_brace != -1:
                trailer = text[close_brace + 1 :].strip()
                if not trailer or trailer.startswith("//"):
                    text = text[open_brace + 1 : close_brace].strip()
                    notes.append("removed outer namespace xls wrapper")

        return text.strip() + "\n", notes

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
