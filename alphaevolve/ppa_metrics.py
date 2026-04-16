"""
alphaevolve/ppa_metrics.py
──────────────────────────
Extract PPA metrics from XLS pipeline outputs.

Priority / source hierarchy:
  1. benchmark_main stdout  (primary) — uses asap7 area model, delay model critical path
     → critical_path_ps, total_area_um2, total_pipeline_flops, num_stages
  2. block_metrics textproto (secondary) — from codegen_main --block_metrics_path
     → flop_count (XLS-internal accurate), per-type delay breakdown
  3. Verilog regex fallback (tertiary) — approximate register counting

PPA Score (lower = better):
    score = num_stages        * STAGE_WEIGHT
          + pipeline_flops    * FLOP_WEIGHT
          + area_um2          * AREA_WEIGHT
          + critical_path_ps  * DELAY_WEIGHT
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xls_tools.pipeline import BenchmarkOutput


# ── Scoring weights ────────────────────────────────────────────────────────────
#
# Defaults are tuned for scheduler evolution:
# - delay is important because scheduling directly changes stage balance
# - total area is included, but at lower weight because benchmark_main's
#   operation-area estimate is mostly structural IR area and often changes less
#   than schedule-dependent metrics
# - stage count remains a latency proxy, but is no longer dominant
# - power is disabled by default because benchmark_main does not provide a
#   direct power metric; callers may opt into the pipeline-flop proxy if desired
STAGE_WEIGHT = 200.0
FLOP_WEIGHT = 0.0
AREA_WEIGHT = 1.0
DELAY_WEIGHT = 1.0


def configure_scoring(
    *,
    stage_weight: float | None = None,
    flop_weight: float | None = None,
    area_weight: float | None = None,
    delay_weight: float | None = None,
) -> None:
    """Override module scoring weights for the current process."""
    global STAGE_WEIGHT, FLOP_WEIGHT, AREA_WEIGHT, DELAY_WEIGHT

    if stage_weight is not None:
        STAGE_WEIGHT = float(stage_weight)
    if flop_weight is not None:
        FLOP_WEIGHT = float(flop_weight)
    if area_weight is not None:
        AREA_WEIGHT = float(area_weight)
    if delay_weight is not None:
        DELAY_WEIGHT = float(delay_weight)


@dataclass
class PPAMetrics:
    # ── From benchmark_main (primary) ─────────────────────────────────────────
    critical_path_ps: int = 0        # max delay in the critical path (ps)
    total_delay_ps: int = 0          # sum of all node delays (ps)
    total_area_um2: float = 0.0      # total area from asap7 area model (um²)
    total_pipeline_flops: int = 0    # pipeline register bits from benchmark

    # ── From schedule textproto ────────────────────────────────────────────────
    num_stages: int = 0
    min_clock_period_ps: int = 0
    min_stage_slack_ps: int = 0

    # ── From block_metrics textproto (fallback / corroboration) ───────────────
    flop_count: int = 0              # XLS-internal accurate flip-flop bits
    max_reg_to_reg_delay_ps: int = 0
    max_input_to_reg_delay_ps: int = 0
    max_reg_to_output_delay_ps: int = 0
    max_feedthrough_path_delay_ps: int = 0

    # ── Derived ───────────────────────────────────────────────────────────────
    feasible: bool = False
    score: float = float("inf")

    # Backward-compat: pipeline_reg_bits alias
    @property
    def pipeline_reg_bits(self) -> int:
        return self.flop_count or self.total_pipeline_flops

    # Effective register count: prefer benchmark (more complete) over block_metrics
    @property
    def effective_flop_count(self) -> int:
        return self.total_pipeline_flops if self.total_pipeline_flops > 0 else self.flop_count

    def _compute(self):
        """Recompute score from current field values."""
        if self.feasible:
            self.score = (
                self.num_stages          * STAGE_WEIGHT
                + self.effective_flop_count * FLOP_WEIGHT
                + self.total_area_um2    * AREA_WEIGHT
                + self.critical_path_ps  * DELAY_WEIGHT
            )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _re_int(pattern: str, text: str, default: int = 0) -> int:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else default

def _re_float(pattern: str, text: str, default: float = 0.0) -> float:
    m = re.search(pattern, text)
    return float(m.group(1)) if m else default


# ── Parser 1: benchmark_main stdout ──────────────────────────────────────────

def parse_benchmark_output(bm: "BenchmarkOutput") -> dict:
    """Extract PPA fields from a BenchmarkOutput struct."""
    return {
        "critical_path_ps":      bm.critical_path_ps,
        "total_delay_ps":        bm.total_delay_ps,
        "total_area_um2":        bm.total_area_um2,
        "total_pipeline_flops":  bm.total_pipeline_flops,
        "num_stages":            bm.num_stages,
        "min_clock_period_ps":   bm.min_clock_period_ps,
        "min_stage_slack_ps":    bm.min_stage_slack_ps,
    }


# ── Parser 2: Schedule textproto ─────────────────────────────────────────────

def parse_schedule(schedule_path: Path | str) -> dict:
    """Parse PackageScheduleProto textproto → num_stages + min_clock_period_ps."""
    text = Path(schedule_path).read_text(encoding="utf-8")
    return {
        "num_stages":          _re_int(r"\blength\s*:\s*(\d+)", text),
        "min_clock_period_ps": _re_int(r"\bmin_clock_period_ps\s*:\s*(\d+)", text),
    }


# ── Parser 3: Block metrics textproto ────────────────────────────────────────

def parse_block_metrics(block_metrics_path: Path | str) -> dict:
    """Parse XlsMetricsProto textproto → flop_count + delay breakdown."""
    text = Path(block_metrics_path).read_text(encoding="utf-8")
    return {
        "flop_count":                   _re_int(r"\bflop_count\s*:\s*(\d+)", text),
        "max_reg_to_reg_delay_ps":      _re_int(r"\bmax_reg_to_reg_delay_ps\s*:\s*(\d+)", text),
        "max_input_to_reg_delay_ps":    _re_int(r"\bmax_input_to_reg_delay_ps\s*:\s*(\d+)", text),
        "max_reg_to_output_delay_ps":   _re_int(r"\bmax_reg_to_output_delay_ps\s*:\s*(\d+)", text),
        "max_feedthrough_path_delay_ps":_re_int(r"\bmax_feedthrough_path_delay_ps\s*:\s*(\d+)", text),
    }


# ── Parser 4: Verilog fallback ────────────────────────────────────────────────

def parse_verilog_fallback(verilog_path: Path | str) -> int:
    text = Path(verilog_path).read_text(encoding="utf-8")
    total = 0
    for m in re.finditer(r"\breg\s+(?:\[(\d+):(\d+)\]\s+)?\w+\s*;", text):
        if m.group(1) is not None:
            total += int(m.group(1)) - int(m.group(2)) + 1
        else:
            total += 1
    return total


# ── Main extraction function ──────────────────────────────────────────────────

def extract_ppa(
    schedule_path:      Path | str | None = None,
    verilog_path:       Path | str | None = None,
    block_metrics_path: Path | str | None = None,
    benchmark_output:   "BenchmarkOutput | None" = None,
) -> PPAMetrics:
    """
    Extract full PPA metrics. Source priority:
      1. benchmark_output    → num_stages, area, critical_path, flops (most complete)
      2. schedule textproto  → num_stages, min_clock (fallback / corroboration)
      3. block_metrics       → flop_count, per-delay-type breakdown
      4. Verilog regex       → approximate flop count (last resort)
    """
    m = PPAMetrics()

    has_benchmark = benchmark_output is not None and benchmark_output.critical_path_ps > 0
    has_schedule  = schedule_path and Path(schedule_path).exists()
    has_bm_file   = block_metrics_path and Path(block_metrics_path).exists()
    has_verilog   = verilog_path and Path(verilog_path).exists()

    if not has_benchmark and not has_schedule:
        return PPAMetrics(feasible=False)

    m.feasible = True

    # ── 1. benchmark_main (primary) ───────────────────────────────────────────
    if has_benchmark:
        bm = parse_benchmark_output(benchmark_output)
        m.critical_path_ps     = bm["critical_path_ps"]
        m.total_delay_ps       = bm["total_delay_ps"]
        m.total_area_um2       = bm["total_area_um2"]
        m.total_pipeline_flops = bm["total_pipeline_flops"]
        m.num_stages           = bm["num_stages"]
        m.min_clock_period_ps  = bm["min_clock_period_ps"]
        m.min_stage_slack_ps   = bm["min_stage_slack_ps"]

    # ── 2. Schedule textproto (fills num_stages if benchmark didn't) ──────────
    if has_schedule:
        sched = parse_schedule(schedule_path)
        if m.num_stages == 0:
            m.num_stages = sched["num_stages"]
        if m.min_clock_period_ps == 0:
            m.min_clock_period_ps = sched["min_clock_period_ps"]

    # ── 3. Block metrics (more accurate flop/delay from XLS internals) ────────
    if has_bm_file:
        bm_data = parse_block_metrics(block_metrics_path)
        m.flop_count                    = bm_data["flop_count"]
        m.max_reg_to_reg_delay_ps       = bm_data["max_reg_to_reg_delay_ps"]
        m.max_input_to_reg_delay_ps     = bm_data["max_input_to_reg_delay_ps"]
        m.max_reg_to_output_delay_ps    = bm_data["max_reg_to_output_delay_ps"]
        m.max_feedthrough_path_delay_ps = bm_data["max_feedthrough_path_delay_ps"]
        # Use block_metrics for critical path if benchmark didn't give it
        if m.critical_path_ps == 0:
            m.critical_path_ps = max(
                m.max_reg_to_reg_delay_ps,
                m.max_input_to_reg_delay_ps,
                m.max_reg_to_output_delay_ps,
            )

    # ── 4. Verilog fallback for flop count ────────────────────────────────────
    elif has_verilog and m.flop_count == 0 and m.total_pipeline_flops == 0:
        m.flop_count = parse_verilog_fallback(verilog_path)

    m._compute()
    return m
