"""
xls_tools/pipeline.py
─────────────────────
Runs the full XLS DSLX → IR → opt → codegen pipeline.
Supports both the pre-built binary release and the custom-built binary
(after Bazel build).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PipelineResult:
    success: bool
    verilog_path: Path | None
    schedule_path: Path | None
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
        (e.g. /mnt/d/final/xls-v0.0.0-...-linux-x64)
      - bazel_bin_dir: use binaries from a custom Bazel build
        (e.g. /mnt/d/final/xls/bazel-bin/xls/tools)

    Priority: bazel_bin_dir > prebuilt_bin_dir if both are given.
    """

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

    def _bin(self, name: str) -> Path:
        """Resolve a binary, preferring custom Bazel build."""
        if self.bazel_bin_dir:
            p = self.bazel_bin_dir / name
            if p.exists():
                return p
        if self.prebuilt_bin_dir:
            p = self.prebuilt_bin_dir / name
            if p.exists():
                return p
        raise FileNotFoundError(
            f"Binary '{name}' not found in "
            f"{self.bazel_bin_dir or '(none)'} or "
            f"{self.prebuilt_bin_dir or '(none)'}"
        )

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
        # XLS IR: "fn __pkg__fn(...)" — we want the mangled name
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
    ) -> PipelineResult:
        """
        Full DSLX → Verilog pipeline.

        Args:
            dslx_file:       Input .x DSLX source file.
            output_dir:      Directory to write IR, Verilog, schedule.
            clock_period_ps: Target clock period in picoseconds.
            pipeline_stages: Force N pipeline stages (None = auto).
            delay_model:     XLS delay model name ('unit', 'sky130', 'asap7').
            generator:       Codegen mode ('pipeline', 'combinational').

        Returns:
            PipelineResult with paths to all outputs.
        """
        dslx_file = Path(dslx_file)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        stem = dslx_file.stem
        ir_path = output_dir / f"{stem}.ir"
        opt_ir_path = output_dir / f"{stem}_opt.ir"
        verilog_path = output_dir / f"{stem}.v"
        schedule_path = output_dir / f"{stem}_schedule.textproto"

        # ── Stage 1: DSLX → IR ──────────────────────────────────────────────
        result = self._run([self._bin("ir_converter_main"), str(dslx_file)])
        if result.returncode != 0:
            return PipelineResult(
                success=False, verilog_path=None, schedule_path=None,
                ir_path=None, top_function=None,
                stdout=result.stdout, stderr=result.stderr,
                error_stage="ir_convert",
            )
        ir_text = result.stdout
        ir_path.write_text(ir_text, encoding="utf-8")

        # ── Detect top function name ─────────────────────────────────────────
        top = self._detect_top(ir_text)
        if not top:
            return PipelineResult(
                success=False, verilog_path=None, schedule_path=None,
                ir_path=ir_path, top_function=None,
                stdout=result.stdout,
                stderr="Could not detect top function from IR",
                error_stage="ir_convert",
            )

        # ── Stage 2: Optimize IR ─────────────────────────────────────────────
        result = self._run([
            self._bin("opt_main"), str(ir_path), f"--top={top}"
        ])
        if result.returncode != 0:
            return PipelineResult(
                success=False, verilog_path=None, schedule_path=None,
                ir_path=ir_path, top_function=top,
                stdout=result.stdout, stderr=result.stderr,
                error_stage="opt",
            )
        opt_ir_path.write_text(result.stdout, encoding="utf-8")

        # ── Stage 3: Codegen → Verilog ───────────────────────────────────────
        codegen_cmd = [
            self._bin("codegen_main"), str(opt_ir_path),
            f"--generator={generator}",
            f"--delay_model={delay_model}",
            f"--output_verilog_path={verilog_path}",
        ]
        if generator == "pipeline":
            codegen_cmd += [f"--clock_period_ps={clock_period_ps}"]
            codegen_cmd += [f"--output_schedule_path={schedule_path}"]
            if pipeline_stages is not None:
                codegen_cmd += [f"--pipeline_stages={pipeline_stages}"]

        result = self._run(codegen_cmd)
        if result.returncode != 0:
            return PipelineResult(
                success=False, verilog_path=None,
                schedule_path=None, ir_path=ir_path, top_function=top,
                stdout=result.stdout, stderr=result.stderr,
                error_stage="codegen",
            )

        return PipelineResult(
            success=True,
            verilog_path=verilog_path if verilog_path.exists() else None,
            schedule_path=schedule_path if schedule_path.exists() else None,
            ir_path=ir_path,
            top_function=top,
            stdout=result.stdout,
            stderr=result.stderr,
        )
