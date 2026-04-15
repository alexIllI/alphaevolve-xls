"""
xls_tools/pipeline.py
─────────────────────
Runs the full XLS DSLX → IR → opt → codegen pipeline.

Key flags added vs. v1:
  --scheduling_strategy=sdc   ensures the (possibly mutated) SDC scheduler is used
  --block_metrics_path=...    writes BlockMetricsProto with real flop_count + delay
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PipelineResult:
    success: bool
    verilog_path: Path | None
    schedule_path: Path | None
    block_metrics_path: Path | None          # ← NEW: XlsMetricsProto textproto
    ir_path: Path | None
    top_function: str | None
    stdout: str
    stderr: str
    error_stage: str | None = None  # 'ir_convert' | 'opt' | 'codegen'


class XLSPipeline:
    """
    Runs DSLX → IR → optimized IR → Verilog codegen using XLS binaries.

    Two modes:
      - prebuilt_bin_dir: use the release binary directory
      - bazel_bin_dir: use binaries from a custom Bazel build

    Priority: bazel_bin_dir > prebuilt_bin_dir if both are given.
    """

    # Tool name → relative path within the Bazel build root
    _TOOL_PATHS = {
        "ir_converter_main": "xls/dslx/ir_convert/ir_converter_main",
        "opt_main":           "xls/tools/opt_main",
        "codegen_main":       "xls/tools/codegen_main",
        "benchmark_main":     "xls/dev_tools/benchmark_main",   # optional, build separately
    }

    def __init__(
        self,
        prebuilt_bin_dir: Path | str | None = None,
        bazel_bin_dir: Path | str | None = None,
        tmp_dir: Path | str | None = None,
    ):
        self.prebuilt_bin_dir = Path(prebuilt_bin_dir) if prebuilt_bin_dir else None
        self.bazel_bin_dir = Path(bazel_bin_dir) if bazel_bin_dir else None

        if not self.prebuilt_bin_dir and not self.bazel_bin_dir:
            raise ValueError("Must specify at least one of prebuilt_bin_dir or bazel_bin_dir")

        self.tmp_dir = Path(tmp_dir) if tmp_dir else None

    def _bin(self, name: str) -> Path | None:
        """
        Resolve a binary by name. Returns None if not found (for optional tools).
        Prefers Bazel build output over prebuilt release.
        """
        if self.bazel_bin_dir:
            rel = self._TOOL_PATHS.get(name, f"xls/tools/{name}")
            bazel_root = self.bazel_bin_dir.parent.parent   # .../xls/bazel-bin
            p = bazel_root / rel
            if p.exists():
                return p
        if self.prebuilt_bin_dir:
            p = self.prebuilt_bin_dir / name
            if p.exists():
                return p
        return None

    def _require_bin(self, name: str) -> Path:
        """Like _bin but raises if not found."""
        p = self._bin(name)
        if p is None:
            raise FileNotFoundError(
                f"Binary '{name}' not found in "
                f"{self.bazel_bin_dir or '(none)'} or "
                f"{self.prebuilt_bin_dir or '(none)'}"
            )
        return p

    def _run(self, cmd: list, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(c) for c in cmd],
            capture_output=True,
            text=True,
            timeout=120,
            **kwargs,
        )

    def _detect_top(self, ir_text: str) -> str | None:
        """Extract the fully-qualified top function name from IR text."""
        match = re.search(r"^fn\s+(\S+)\(", ir_text, re.MULTILINE)
        return match.group(1) if match else None

    def run(
        self,
        dslx_file: Path | str,
        output_dir: Path | str,
        clock_period_ps: int = 1000,
        pipeline_stages: int | None = None,
        delay_model: str = "unit",
        generator: str = "pipeline",
        scheduling_strategy: str = "sdc",    # ← ensures mutated SDC scheduler is used
    ) -> PipelineResult:
        """
        Full DSLX → Verilog pipeline.

        Args:
            dslx_file:            Input .x DSLX source file.
            output_dir:           Directory to write IR, Verilog, schedule, metrics.
            clock_period_ps:      Target clock period in picoseconds.
            pipeline_stages:      Force N pipeline stages (None = auto).
            delay_model:          XLS delay model name ('unit', 'sky130', 'asap7').
            generator:            Codegen mode ('pipeline', 'combinational').
            scheduling_strategy:  'sdc' (default), 'asap', 'min_cut', 'random'.
                                  MUST be 'sdc' to invoke our mutated scheduler.

        Returns:
            PipelineResult with paths to all outputs including block_metrics_path.
        """
        dslx_file = Path(dslx_file)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        stem = dslx_file.stem
        ir_path           = output_dir / f"{stem}.ir"
        opt_ir_path       = output_dir / f"{stem}_opt.ir"
        verilog_path      = output_dir / f"{stem}.v"
        schedule_path     = output_dir / f"{stem}_schedule.textproto"
        block_metrics_path = output_dir / f"{stem}_block_metrics.textproto"   # ← NEW

        _fail = lambda stage, res: PipelineResult(
            success=False, verilog_path=None, schedule_path=None,
            block_metrics_path=None, ir_path=ir_path if ir_path.exists() else None,
            top_function=None, stdout=res.stdout, stderr=res.stderr,
            error_stage=stage,
        )

        # ── Stage 1: DSLX → IR ──────────────────────────────────────────────────
        result = self._run([self._require_bin("ir_converter_main"), str(dslx_file)])
        if result.returncode != 0:
            return _fail("ir_convert", result)

        ir_text = result.stdout
        ir_path.write_text(ir_text, encoding="utf-8")

        top = self._detect_top(ir_text)
        if not top:
            return PipelineResult(
                success=False, verilog_path=None, schedule_path=None,
                block_metrics_path=None, ir_path=ir_path, top_function=None,
                stdout=result.stdout,
                stderr="Could not detect top function from IR",
                error_stage="ir_convert",
            )

        # ── Stage 2: Optimize IR ─────────────────────────────────────────────────
        result = self._run([self._require_bin("opt_main"), str(ir_path), f"--top={top}"])
        if result.returncode != 0:
            return _fail("opt", result)

        opt_ir_path.write_text(result.stdout, encoding="utf-8")

        # ── Stage 3: Codegen → Verilog (+ schedule + block metrics) ──────────────
        codegen_cmd = [
            self._require_bin("codegen_main"), str(opt_ir_path),
            f"--generator={generator}",
            f"--delay_model={delay_model}",
            f"--output_verilog_path={verilog_path}",
        ]
        if generator == "pipeline":
            codegen_cmd += [
                f"--clock_period_ps={clock_period_ps}",
                f"--output_schedule_path={schedule_path}",
                f"--scheduling_strategy={scheduling_strategy}",   # ← ensures SDC runs
                f"--block_metrics_path={block_metrics_path}",     # ← XlsMetricsProto output
            ]
            if pipeline_stages is not None:
                codegen_cmd += [f"--pipeline_stages={pipeline_stages}"]

        result = self._run(codegen_cmd)
        if result.returncode != 0:
            return _fail("codegen", result)

        return PipelineResult(
            success=True,
            verilog_path=verilog_path if verilog_path.exists() else None,
            schedule_path=schedule_path if schedule_path.exists() else None,
            block_metrics_path=block_metrics_path if block_metrics_path.exists() else None,
            ir_path=ir_path,
            top_function=top,
            stdout=result.stdout,
            stderr=result.stderr,
        )
