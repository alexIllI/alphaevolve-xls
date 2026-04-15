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

    # Targets needed for the full DSLX→Verilog pipeline + optional benchmark
    # NOTE: ir_converter_main lives under dslx/ir_convert, not tools
    # benchmark_main: in dev_tools; gives area/delay estimates (built for future use)
    TARGETS = [
        "//xls/tools:codegen_main",
        "//xls/tools:opt_main",
        "//xls/dslx/ir_convert:ir_converter_main",
        "//xls/dev_tools:benchmark_main",
    ]

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

    def build(self) -> BuildResult:
        """
        Run an incremental Bazel build of the codegen pipeline targets.
        Returns BuildResult with success flag, duration, and logs.
        """
        cmd = [
            self.bazel_bin,
            "build",
            "-c", "opt",
            "-j", str(self.bazel_jobs),
            "--show_progress_rate_limit=10",
            *self.TARGETS,
        ]

        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.xls_src,
                capture_output=True,
                text=True,
                timeout=600,  # 10 min max for an incremental build
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
        return self.xls_src / "bazel-bin" / "xls" / "tools" / tool

    def is_built(self, tool: str = "codegen_main") -> bool:
        """Check whether the XLS binary has been built."""
        return self.binary_path(tool).exists()
