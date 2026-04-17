"""
xls_tools/pipeline.py
─────────────────────
Runs the full XLS DSLX → IR → opt → (codegen + benchmark_main) pipeline.

The scheduling strategy is always ``agent`` — we do not use sdc / min_cut /
random / asap from this harness. The AI-evolved code lives in
``xls/scheduling/agent_generated_scheduler.cc`` and is dispatched by XLS when
``--scheduling_strategy=agent`` is passed.

PPA mode controls how much work is done per iteration:
  - fast     (default): codegen_main only; parse block_metrics textproto for
                        pipeline_stages and total_pipeline_registers.
  - medium  : run Yosys `synth; stat` on the Verilog (future).
  - slow    : rebuild + run benchmark_main for asap7 area + delay.
  - slowest : Yosys synth_asap7 + OpenROAD (future).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BenchmarkOutput:
    """Parsed output from benchmark_main stdout."""
    critical_path_ps: int = 0
    total_delay_ps: int = 0
    total_area_um2: float = 0.0
    total_pipeline_flops: int = 0
    min_clock_period_ps: int = 0
    min_stage_slack_ps: int = 0
    num_stages: int = 0                # inferred from stage count in output
    raw_stdout: str = ""


@dataclass
class PipelineResult:
    success: bool
    verilog_path: Path | None
    schedule_path: Path | None
    block_metrics_path: Path | None
    benchmark_output: BenchmarkOutput | None   # ← primary PPA source
    ir_path: Path | None
    top_function: str | None
    stdout: str
    stderr: str
    error_stage: str | None = None


class XLSPipeline:
    """
    Runs DSLX → IR → optimized IR → Verilog + PPA benchmarks using XLS binaries.
    """

    _TOOL_PATHS = {
        "ir_converter_main": "xls/dslx/ir_convert/ir_converter_main",
        "opt_main":           "xls/tools/opt_main",
        "codegen_main":       "xls/tools/codegen_main",
        "benchmark_main":     "xls/dev_tools/benchmark_main",
    }

    def __init__(
        self,
        prebuilt_bin_dir: Path | str | None = None,
        bazel_bin_dir: Path | str | None = None,
        dslx_stdlib_path: Path | str | None = None,
        tmp_dir: Path | str | None = None,
    ):
        self.prebuilt_bin_dir = Path(prebuilt_bin_dir) if prebuilt_bin_dir else None
        self.bazel_bin_dir = Path(bazel_bin_dir) if bazel_bin_dir else None
        self.dslx_stdlib_path = Path(dslx_stdlib_path) if dslx_stdlib_path else None
        self.tmp_dir = Path(tmp_dir) if tmp_dir else None

        if not self.prebuilt_bin_dir and not self.bazel_bin_dir:
            raise ValueError("Must specify at least one of prebuilt_bin_dir or bazel_bin_dir")

    def _bin(self, name: str) -> Path | None:
        if self.bazel_bin_dir:
            rel = self._TOOL_PATHS.get(name, f"xls/tools/{name}")
            bazel_root = self.bazel_bin_dir.parent.parent
            p = bazel_root / rel
            if p.exists():
                return p
        if self.prebuilt_bin_dir:
            p = self.prebuilt_bin_dir / name
            if p.exists():
                return p
        return None

    def _require_bin(self, name: str) -> Path:
        p = self._bin(name)
        if p is None:
            raise FileNotFoundError(f"Binary '{name}' not found")
        return p

    def _run(self, cmd: list, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(c) for c in cmd],
            capture_output=True,
            text=True,
            timeout=300,
            **kwargs,
        )

    def _detect_top(self, ir_text: str) -> str | None:
        """Extract the package-top entity name from IR text (the 'top' declaration)."""
        # IR emits:  top proc __pkg__name_next<...>(...)  or  top fn __pkg__name
        m = re.search(r"^top\s+(?:proc|fn|block)\s+(\S+?)[\s(<]", ir_text, re.MULTILINE)
        if m:
            return m.group(1)
        # Fallback: first proc/fn declaration
        m = re.search(r"^(?:fn|proc|block)\s+(\S+?)[\s(]", ir_text, re.MULTILINE)
        return m.group(1) if m else None

    @staticmethod
    def _detect_dslx_top(dslx_text: str) -> str | None:
        """
        Find the top entity name from a DSLX source file.
        Priority:
          1. Last 'pub proc <name>' or 'pub fn <name>' (non-test)
          2. Last 'proc <name>' or 'fn <name>' (non-test)
        Excludes #[test_proc] decorated procs.
        """
        # Remove lines that come after #[test_proc] or #[test]
        # by simply finding the last pub proc/fn before any test annotation
        lines = dslx_text.splitlines()
        top = None
        in_test = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('#[test'):
                in_test = True
                continue
            if in_test and (stripped.startswith('proc ') or stripped.startswith('fn ')):
                in_test = False   # test block ended, back to normal
                continue
            if not in_test:
                m = re.match(r'^(?:pub\s+)?(?:proc|fn)\s+(\w+)', stripped)
                if m:
                    top = m.group(1)
        return top

    def run(
        self,
        dslx_file: Path | str,
        output_dir: Path | str,
        clock_period_ps: int = 1000,
        pipeline_stages: int | None = None,
        delay_model: str = "unit",
        area_model: str = "asap7",
        generator: str = "pipeline",
        ppa_mode: str = "fast",
        # The legacy parameters below are ignored — scheduling strategy is now
        # hard-coded to ``agent`` and benchmark_main is only invoked when
        # ppa_mode=="slow". They are kept in the signature for backward
        # compatibility with existing callers.
        scheduling_strategy: str | None = None,
        use_benchmark_main: bool | None = None,
    ) -> PipelineResult:
        """
        Full DSLX → Verilog + PPA pipeline. Always uses --scheduling_strategy=agent.

        Stages:
          1. ir_converter_main  (.x → .ir)
          2. opt_main           (.ir → .opt.ir)
          3. benchmark_main     [only if ppa_mode=="slow"] — asap7 area + delay
          4. codegen_main       (.opt.ir → .v + schedule + block_metrics)
        """
        # Force agent strategy regardless of caller arguments.
        scheduling_strategy = "agent"
        run_benchmark_main = (ppa_mode == "slow")
        dslx_file = Path(dslx_file)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        stem = dslx_file.stem
        ir_path            = output_dir / f"{stem}.ir"
        opt_ir_path        = output_dir / f"{stem}_opt.ir"
        verilog_path       = output_dir / f"{stem}.v"
        schedule_path      = output_dir / f"{stem}_schedule.textproto"
        block_metrics_path = output_dir / f"{stem}_block_metrics.textproto"
        benchmark_log_path = output_dir / f"{stem}_benchmark.txt"

        def _fail(stage, res):
            return PipelineResult(
                success=False, verilog_path=None, schedule_path=None,
                block_metrics_path=None, benchmark_output=None,
                ir_path=ir_path if ir_path.exists() else None,
                top_function=None, stdout=res.stdout, stderr=res.stderr,
                error_stage=stage,
            )

        # ── Stage 1: DSLX → IR ──────────────────────────────────────────────────
        # Detect top from DSLX source first (accurate for multi-proc designs)
        dslx_text = dslx_file.read_text(encoding="utf-8")
        dslx_top = self._detect_dslx_top(dslx_text)

        ir_cmd = [self._require_bin("ir_converter_main"), str(dslx_file)]
        if self.dslx_stdlib_path:
            ir_cmd += [f"--dslx_stdlib_path={self.dslx_stdlib_path}"]
        if dslx_top:
            ir_cmd += [f"--top={dslx_top}"]   # sets package top in the IR
        result = self._run(ir_cmd)
        if result.returncode != 0:
            return _fail("ir_convert", result)

        ir_text = result.stdout
        ir_path.write_text(ir_text, encoding="utf-8")

        top = self._detect_top(ir_text)
        if not top:
            return PipelineResult(
                success=False, verilog_path=None, schedule_path=None,
                block_metrics_path=None, benchmark_output=None,
                ir_path=ir_path, top_function=None,
                stdout=result.stdout,
                stderr="Could not detect top from IR",
                error_stage="ir_convert",
            )

        # ── Stage 2: Optimize IR ─────────────────────────────────────────────────
        # Don't pass --top; package top was already set by ir_converter_main.
        result = self._run([self._require_bin("opt_main"), str(ir_path)])
        if result.returncode != 0:
            return _fail("opt", result)
        opt_ir_path.write_text(result.stdout, encoding="utf-8")

        # ── Stage 3: benchmark_main → primary PPA report ─────────────────────────
        benchmark_out = None
        bm_bin = self._bin("benchmark_main")
        if run_benchmark_main and bm_bin:
            bm_cmd = [
                bm_bin, str(opt_ir_path),
                # No --top: auto-detected from package top set by ir_converter_main
                f"--delay_model={delay_model}",
                f"--area_model={area_model}",
                f"--scheduling_strategy={scheduling_strategy}",
                "--run_evaluators=false",
                "--generator=pipeline",
                f"--clock_period_ps={clock_period_ps}",
            ]
            if pipeline_stages is not None:
                bm_cmd += [f"--pipeline_stages={pipeline_stages}"]

            bm_result = self._run(bm_cmd)
            bm_text = bm_result.stdout + bm_result.stderr
            benchmark_log_path.write_text(bm_text, encoding="utf-8")

            # Parse even on non-zero exit: benchmark_main for proc networks
            # completes scheduling (prints Critical path, delay per stage) but
            # then fails at internal block codegen lowering. The scheduling
            # metrics are what we need, and they're printed before codegen.
            parsed = parse_benchmark_stdout(bm_text)
            if parsed.critical_path_ps > 0 or parsed.num_stages > 0 or "Pipeline:" in bm_text:
                benchmark_out = parsed
                benchmark_out.raw_stdout = bm_text
        else:
            message = (
                f"benchmark_main skipped (ppa_mode={ppa_mode})\n"
                if not run_benchmark_main
                else "benchmark_main not built yet\n"
            )
            benchmark_log_path.write_text(message, encoding="utf-8")

        # ── Stage 4: codegen_main → Verilog + block_metrics ─────────────────────
        codegen_cmd = [
            self._require_bin("codegen_main"), str(opt_ir_path),
            # No --top: auto-detected from package top set by ir_converter_main
            f"--generator={generator}",
            f"--delay_model={delay_model}",
            f"--output_verilog_path={verilog_path}",
        ]
        if generator == "pipeline":
            codegen_cmd += [
                f"--clock_period_ps={clock_period_ps}",
                f"--output_schedule_path={schedule_path}",
                f"--scheduling_strategy={scheduling_strategy}",
                f"--block_metrics_path={block_metrics_path}",
            ]
            if pipeline_stages is not None:
                codegen_cmd += [f"--pipeline_stages={pipeline_stages}"]

        result = self._run(codegen_cmd)
        # codegen_main failure is non-fatal if benchmark_main already produced PPA.
        # Proc networks (e.g. matmul_4x4) may fail codegen_main's register-reset
        # validation while benchmark_main handles them fine.
        codegen_ok = result.returncode == 0
        if not codegen_ok and benchmark_out is None:
            return _fail("codegen", result)   # no PPA at all → hard fail

        return PipelineResult(
            success=True,
            verilog_path=verilog_path if (codegen_ok and verilog_path.exists()) else None,
            schedule_path=schedule_path if (codegen_ok and schedule_path.exists()) else None,
            block_metrics_path=block_metrics_path if (codegen_ok and block_metrics_path.exists()) else None,
            benchmark_output=benchmark_out,
            ir_path=ir_path,
            top_function=top,
            stdout=result.stdout,
            stderr=result.stderr,
        )


def parse_benchmark_stdout(text: str) -> BenchmarkOutput:
    """
    Parse benchmark_main stdout and aggregate across all function/proc sections.

    For simple functions:    one global header + one Pipeline section.
    For proc networks:       one global header (may be empty shell) + N per-function
                             sections. We must aggregate across all sections to get
                             meaningful metrics.

    Aggregation strategy (handles both cases):
      critical_path_ps      = MAX of all "delay: Xps" in pipeline nodes lines
                              + MAX of all "Critical path delay: Xps"
      total_pipeline_flops  = SUM of all "Total pipeline flops: X"
      total_area_um2        = SUM of all "Total area: X um2"
      num_stages            = distinct [Stage X] labels; or count of "Pipeline:" blocks

    Key output patterns:
        Critical path delay: 4ps
        Total delay: 15ps
        Total area: 3386.0000 um2
        [Stage  0]     nodes:  12, delay:   4ps     ← multi-stage
                                   nodes:  31, delay:   72ps  ← single-stage
        Total pipeline flops: 0 (0 dups,    0 constant)
        Min stage slack: 996
    """
    # ── critical_path_ps: max of stated critical path headers + all stage delays ─
    cp_from_header = max(
        (int(m.group(1)) for m in re.finditer(r"Critical path delay:\s*(\d+)ps", text)),
        default=0,
    )
    # "nodes: N, delay: Xps"  — appears for every pipeline section regardless of stage count
    stage_delay_matches = re.finditer(
        r"(?:\[\s*Stage\s+\d+\]\s*nodes:\s*\d+,\s*delay:\s*(\d+)ps)|"
        r"(?:^\s*nodes:\s*\d+,\s*delay:\s*(\d+)ps)",
        text,
        re.MULTILINE,
    )
    cp_from_stages = max(
        (int(m.group(1) or m.group(2)) for m in stage_delay_matches),
        default=0,
    )
    critical_path_ps = max(cp_from_header, cp_from_stages)

    # ── total_pipeline_flops: SUM across all procs ────────────────────────────
    total_pipeline_flops = sum(
        int(m.group(1)) for m in re.finditer(r"Total pipeline flops:\s*(\d+)", text)
    )

    # ── total_area_um2: SUM across all procs ─────────────────────────────────
    total_area_um2 = sum(
        float(m.group(1)) for m in re.finditer(r"Total area:\s*([\d.]+)\s*um2", text)
    )

    # ── total_delay_ps: SUM of all "Total delay: Xps" ────────────────────────
    total_delay_ps = sum(
        int(m.group(1)) for m in re.finditer(r"Total delay:\s*(\d+)ps", text)
    )

    # ── num_stages: [Stage N] labels or Pipeline: block count ─────────────────
    stage_nums = set(int(m.group(1)) for m in re.finditer(r"\[Stage\s+(\d+)\]", text))
    if stage_nums:
        num_stages = len(stage_nums)
    else:
        num_stages = text.count("Pipeline:")

    # ── scalar fields (first match) ───────────────────────────────────────────
    def _int(pattern):
        m = re.search(pattern, text)
        return int(m.group(1)) if m else 0

    return BenchmarkOutput(
        critical_path_ps     = critical_path_ps,
        total_delay_ps       = total_delay_ps,
        total_area_um2       = total_area_um2,
        total_pipeline_flops = total_pipeline_flops,
        min_clock_period_ps  = _int(r"Min clock period ps:\s*(\d+)"),
        min_stage_slack_ps   = _int(r"Min stage slack:\s*(-?\d+)"),
        num_stages           = num_stages,
    )
