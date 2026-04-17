"""
xls_tools/build.py
──────────────────
Wraps incremental Bazel builds of the XLS codegen_main binary.

Workflow per iteration:
  1. backup()   — save original source file
  2. apply()    — write AI-generated C++ to the target source file
  3. build()    — run bazel build (incremental, only changed .cc + deps)
  4. restore()  — on failure, restore original from backup
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BuildResult:
    success: bool
    duration_seconds: float
    stdout: str
    stderr: str
    binary_path: Path | None = None


class XLSBuilder:
    """Manages incremental Bazel builds of XLS codegen_main."""

    TOOL_PATHS = {
        "ir_converter_main": "xls/dslx/ir_convert/ir_converter_main",
        "opt_main": "xls/tools/opt_main",
        "codegen_main": "xls/tools/codegen_main",
        "benchmark_main": "xls/dev_tools/benchmark_main",
    }

    # ── Build target groups ────────────────────────────────────────────────────
    # Every iteration rebuilds the agent_generated_scheduler library and the
    # tools that link against it. codegen_main must be relinked after any
    # scheduler change; opt_main / ir_converter_main are listed so they stay
    # in sync with a fresh XLS tree after a clean build.
    BOOTSTRAP_TARGETS = [
        "//xls/scheduling:agent_generated_scheduler",
        "//xls/tools:codegen_main",
        "//xls/tools:opt_main",
        "//xls/dslx/ir_convert:ir_converter_main",
    ]

    ITERATION_TARGETS = [
        "//xls/scheduling:agent_generated_scheduler",
        "//xls/tools:codegen_main",
        "//xls/tools:opt_main",
        "//xls/dslx/ir_convert:ir_converter_main",
    ]

    # The fastest iteration target set — only what codegen_main needs.
    AGENT_ITERATION_TARGETS = [
        "//xls/scheduling:agent_generated_scheduler",
        "//xls/tools:codegen_main",
    ]

    # benchmark_main links against LLVM/JIT (~122 MB binary). It is only
    # needed when ppa_mode == "slow"; in that case it joins the per-iteration
    # rebuild so asap7 metrics reflect the latest scheduler.
    BENCHMARK_TARGETS = [
        "//xls/dev_tools:benchmark_main",
    ]

    @classmethod
    def iteration_targets_for_mode(cls, ppa_mode: str) -> list[str]:
        """
        Return the per-iteration rebuild list for the chosen PPA mode.
            fast / medium : agent scheduler + codegen_main (no benchmark_main).
            slow          : include benchmark_main so asap7 metrics refresh.
            slowest       : currently same as slow (Yosys/OpenROAD happen
                            outside Bazel).
        """
        base = list(cls.AGENT_ITERATION_TARGETS)
        if ppa_mode in ("slow", "slowest"):
            base.extend(cls.BENCHMARK_TARGETS)
        return base

    def __init__(
        self,
        xls_src: Path | str,
        bazel_jobs: int = 8,
        bazel_bin: str = "bazel",
    ):
        self.xls_src = Path(xls_src)
        self.bazel_jobs = bazel_jobs
        self.bazel_bin = bazel_bin
        self._backups: dict[Path, Path] = {}

        if not self.xls_src.is_dir():
            raise FileNotFoundError(f"XLS source not found: {self.xls_src}")

    # ── File management ────────────────────────────────────────────────────────

    def backup(self, source_file: Path | str) -> Path:
        """Save a copy of source_file; returns backup path."""
        src = Path(source_file)
        backup = src.with_suffix(src.suffix + ".bak")
        shutil.copy2(src, backup)
        self._backups[src] = backup
        return backup

    def apply(self, source_file: Path | str, new_content: str) -> None:
        """Overwrite source_file with new_content (AI-generated C++)."""
        Path(source_file).write_text(new_content, encoding="utf-8")

    def restore(self, source_file: Path | str | None = None) -> None:
        """Restore from backup. Restores all backed-up files if None."""
        targets = (
            [Path(source_file)]
            if source_file
            else list(self._backups.keys())
        )
        for src in targets:
            bak = self._backups.get(src)
            if bak and bak.exists():
                shutil.copy2(bak, src)
                bak.unlink()
                del self._backups[src]

    def cleanup_backups(self) -> None:
        """Remove all .bak files (call after successful build + acceptance)."""
        for src, bak in list(self._backups.items()):
            if bak.exists():
                bak.unlink()
        self._backups.clear()

    # ── Build ──────────────────────────────────────────────────────────────────

    def build_bootstrap(self, include_benchmark_main: bool = False) -> BuildResult:
        """
        Build static targets (benchmark_main etc.) once at startup.
        These are NOT rebuilt on every iteration — only on first run.
        """
        targets = list(self.BOOTSTRAP_TARGETS)
        if include_benchmark_main:
            targets.extend(self.BENCHMARK_TARGETS)
        return self._run_build(targets, timeout=7200)  # 2h for first LLVM compile

    def build_static(self) -> BuildResult:
        """Backward-compatible wrapper for older call sites."""
        return self.build_bootstrap(include_benchmark_main=True)

    def build(self, targets: list[str] | None = None) -> BuildResult:
        """
        Run an incremental Bazel build of the per-iteration targets only
        (codegen_main, opt_main, ir_converter_main — NO benchmark_main).
        Returns BuildResult with success flag, duration, and logs.
        """
        return self._run_build(targets or self.ITERATION_TARGETS, timeout=900)  # 15 min max

    def _run_build(self, targets: list[str], timeout: int) -> BuildResult:
        """Internal: run `bazel build -c opt -j N <targets>`."""
        cmd = [
            self.bazel_bin,
            "build",
            "-c", "opt",
            "-j", str(self.bazel_jobs),
            "--show_progress_rate_limit=10",
            *targets,
        ]

        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.xls_src,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            elapsed = time.monotonic() - start
            return BuildResult(
                success=False,
                duration_seconds=elapsed,
                stdout="",
                stderr=f"Build timed out after {elapsed:.0f}s",
            )
        except FileNotFoundError:
            return BuildResult(
                success=False,
                duration_seconds=0.0,
                stdout="",
                stderr=f"Bazel binary not found: {self.bazel_bin}",
            )

        elapsed = time.monotonic() - start
        success = proc.returncode == 0

        binary_path = None
        if success:
            binary_path = self.xls_src / "bazel-bin" / "xls" / "tools" / "codegen_main"

        return BuildResult(
            success=success,
            duration_seconds=elapsed,
            stdout=proc.stdout,
            stderr=proc.stderr,
            binary_path=binary_path if (binary_path and binary_path.exists()) else None,
        )

    def binary_path(self, tool: str = "codegen_main") -> Path:
        """Return the path to a built XLS binary."""
        rel = self.TOOL_PATHS.get(tool, f"xls/tools/{tool}")
        return self.xls_src / "bazel-bin" / rel

    def is_built(self, tool: str = "codegen_main") -> bool:
        """Check whether the XLS binary has been built."""
        return self.binary_path(tool).exists()
