"""
alphaevolve/ppa_metrics.py
──────────────────────────
Extract PPA metrics from XLS pipeline outputs and compute a normalized score.

Priority / source hierarchy:
  1. benchmark_main stdout  (primary) — uses asap7 area model, delay model
     → max_stage_delay_ps, stage_delays, total_area_um2, num_stages
  2. block_metrics textproto (secondary) — from codegen_main --block_metrics_path
     → flop_count (XLS-internal accurate), per-type delay breakdown
  3. Verilog regex fallback (tertiary) — approximate register counting

PPA Score (lower = better, all terms normalized to [0, 1] before weighting):

    score = (num_stages           / REF_STAGES)    * STAGE_WEIGHT    ← penalise depth
          + (effective_flop_count / REF_FLOP_BITS) * FLOP_WEIGHT     ← penalise reg bits
          + (total_area_um2       / REF_AREA_UM2)  * AREA_WEIGHT     ← penalise area
          + (max_stage_delay_ps   / REF_CLOCK_PS)  * DELAY_WEIGHT    ← penalise tight stages
          + balance_norm                            * BALANCE_WEIGHT  ← penalise uneven load
          + (scheduler_runtime_s  / REF_TIMEOUT_S) * RUNTIME_WEIGHT  ← penalise slow algos

All six metrics are normalised to [0, 1] before weighting, so weights are
directly comparable regardless of raw units (ps vs um² vs seconds).

balance_norm — stage-load Coefficient of Variation, normalised to [0, 1]:

  CV = population_std(stage_delays) / mean(stage_delays)

  The theoretical maximum CV for N stages is sqrt(N - 1) (all delay in one
  stage, all others at zero).  Dividing by sqrt(N - 1) maps CV onto [0, 1]:

    balance_norm = CV / sqrt(max(1, N - 1))

  0 = perfectly even distribution (all stages identical delay) — best.
  1 = worst possible imbalance (one stage holds the entire load).

  Unlike min_stage_slack — which only measures the single tightest stage and
  is mathematically equivalent to max_stage_delay — balance_norm sees the full
  distribution and penalises skewed schedules even when the max stage delay
  happens to be acceptable.

REF_CLOCK_PS and REF_TIMEOUT_S MUST be set each run via configure_scoring()
from --clock_period and --benchmark_timeout CLI arguments.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xls_tools.pipeline import BenchmarkOutput


# ── Scoring weights ────────────────────────────────────────────────────────────
# All terms are normalized to ~[0, 1] so weights are directly comparable.
# All positive: higher normalized value → worse (higher) score.
STAGE_WEIGHT   = 0.0   # pipeline depth:       fewer stages   → lower score
FLOP_WEIGHT    = 0.5   # pipeline register bits: fewer flops  → lower score
AREA_WEIGHT    = 0.0   # combinational area:    rarely changes with scheduling
DELAY_WEIGHT   = 2.0   # max stage delay:       lower delay   → lower score
BALANCE_WEIGHT = 1.5   # stage load CV:         even spread   → lower score (0=perfect)
RUNTIME_WEIGHT = 0.5   # scheduler wall time:   faster algo   → lower score

# ── Reference values for normalization ────────────────────────────────────────
# MUST call configure_scoring(ref_clock_ps=..., ref_timeout_s=...) each run.
REF_STAGES    = 16         # max practical pipeline depth (set from pipeline_stages)
REF_FLOP_BITS = 10_000    # max expected pipeline register bits
REF_AREA_UM2  = 50_000.0  # max expected combinational area (um²)
REF_CLOCK_PS  = 1_000     # clock period (ps) — override with --clock_period value
REF_TIMEOUT_S = 1_800.0   # benchmark timeout (s) — override with --benchmark_timeout


def configure_scoring(
    *,
    # Weights
    stage_weight:   float | None = None,
    flop_weight:    float | None = None,
    area_weight:    float | None = None,
    delay_weight:   float | None = None,
    balance_weight: float | None = None,
    runtime_weight: float | None = None,
    # Reference values for normalization
    ref_stages:    int   | None = None,
    ref_flop_bits: int   | None = None,
    ref_area_um2:  float | None = None,
    ref_clock_ps:  int   | None = None,
    ref_timeout_s: float | None = None,
) -> None:
    """Override module-level scoring weights and normalization references."""
    global STAGE_WEIGHT, FLOP_WEIGHT, AREA_WEIGHT, DELAY_WEIGHT, BALANCE_WEIGHT, RUNTIME_WEIGHT
    global REF_STAGES, REF_FLOP_BITS, REF_AREA_UM2, REF_CLOCK_PS, REF_TIMEOUT_S

    if stage_weight   is not None: STAGE_WEIGHT   = float(stage_weight)
    if flop_weight    is not None: FLOP_WEIGHT    = float(flop_weight)
    if area_weight    is not None: AREA_WEIGHT    = float(area_weight)
    if delay_weight   is not None: DELAY_WEIGHT   = float(delay_weight)
    if balance_weight is not None: BALANCE_WEIGHT = float(balance_weight)
    if runtime_weight is not None: RUNTIME_WEIGHT = float(runtime_weight)

    if ref_stages    is not None: REF_STAGES    = max(1,   int(ref_stages))
    if ref_flop_bits is not None: REF_FLOP_BITS = max(1,   int(ref_flop_bits))
    if ref_area_um2  is not None: REF_AREA_UM2  = max(1.0, float(ref_area_um2))
    if ref_clock_ps  is not None: REF_CLOCK_PS  = max(1,   int(ref_clock_ps))
    if ref_timeout_s is not None: REF_TIMEOUT_S = max(1.0, float(ref_timeout_s))


@dataclass
class PPAMetrics:
    # ── From benchmark_main (primary) ─────────────────────────────────────────
    critical_path_ps: int = 0       # total design CP (constant — dominated by CP header)
    max_stage_delay_ps: int = 0     # max delay across pipeline stages (schedule-sensitive)
    total_delay_ps: int = 0         # sum of all node delays (ps)
    total_area_um2: float = 0.0     # total area from asap7 area model (um²)
    total_pipeline_flops: int = 0   # pipeline register bits from benchmark
    stage_delays: list = field(default_factory=list)   # per-stage delay list (ps)

    # ── From schedule textproto ────────────────────────────────────────────────
    num_stages: int = 0
    min_clock_period_ps: int = 0
    min_stage_slack_ps: int = 0     # kept for display; not used in score

    # ── From block_metrics textproto (fallback / corroboration) ───────────────
    flop_count: int = 0
    max_reg_to_reg_delay_ps: int = 0
    max_input_to_reg_delay_ps: int = 0
    max_reg_to_output_delay_ps: int = 0
    max_feedthrough_path_delay_ps: int = 0

    # ── Scheduler execution time ───────────────────────────────────────────────
    scheduler_runtime_s: float = 0.0   # 3600.0 if timed out (full penalty)

    # ── Derived ───────────────────────────────────────────────────────────────
    feasible: bool = False
    score: float = float("inf")

    @property
    def pipeline_reg_bits(self) -> int:
        return self.flop_count or self.total_pipeline_flops

    @property
    def effective_flop_count(self) -> int:
        return self.total_pipeline_flops if self.total_pipeline_flops > 0 else self.flop_count

    @property
    def balance_cv_norm(self) -> float:
        """Normalised Coefficient of Variation of per-stage delays, in [0, 1].

        CV = population_std(stage_delays) / mean(stage_delays)
        Normalised by sqrt(N - 1) — the theoretical max CV for N stages.
        Returns 0.0 when there is only one stage or all delays are identical.
        """
        delays = self.stage_delays
        n = len(delays)
        if n <= 1:
            return 0.0
        mean = sum(delays) / n
        if mean == 0.0:
            return 0.0
        variance = sum((d - mean) ** 2 for d in delays) / n
        cv = math.sqrt(variance) / mean
        return min(cv / math.sqrt(n - 1), 1.0)

    def normalized_terms(self) -> dict[str, float]:
        """Return each score term as a normalized ratio in [0, 1].

        All ratios are unit-free.  balance_norm measures how evenly stage
        delays are distributed (0 = perfect balance, 1 = worst skew).
        """
        return {
            "stage":   self.num_stages          / max(1,   REF_STAGES),
            "flop":    self.effective_flop_count / max(1,   REF_FLOP_BITS),
            "area":    self.total_area_um2       / max(1.0, REF_AREA_UM2),
            "delay":   self.max_stage_delay_ps   / max(1,   REF_CLOCK_PS),
            "balance": self.balance_cv_norm,
            "runtime": self.scheduler_runtime_s  / max(1.0, REF_TIMEOUT_S),
        }

    def _compute(self) -> None:
        """Compute normalized score. Lower is better.

        Each metric is divided by its reference value first (unit-free ratio
        in [0, 1]), then multiplied by its weight.  Weights are directly
        comparable across metrics regardless of raw units.
        """
        if not self.feasible:
            return
        t = self.normalized_terms()
        self.score = (
            t["stage"]   * STAGE_WEIGHT
            + t["flop"]  * FLOP_WEIGHT
            + t["area"]  * AREA_WEIGHT
            + t["delay"] * DELAY_WEIGHT
            + t["balance"] * BALANCE_WEIGHT
            + t["runtime"] * RUNTIME_WEIGHT
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
        "max_stage_delay_ps":    bm.max_stage_delay_ps,
        "total_delay_ps":        bm.total_delay_ps,
        "total_area_um2":        bm.total_area_um2,
        "total_pipeline_flops":  bm.total_pipeline_flops,
        "num_stages":            bm.num_stages,
        "min_clock_period_ps":   bm.min_clock_period_ps,
        "min_stage_slack_ps":    bm.min_stage_slack_ps,
        "stage_delays":          bm.stage_delays,
        "scheduler_runtime_s":   bm.runtime_s,
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
        "flop_count":                    _re_int(r"\bflop_count\s*:\s*(\d+)", text),
        "max_reg_to_reg_delay_ps":       _re_int(r"\bmax_reg_to_reg_delay_ps\s*:\s*(\d+)", text),
        "max_input_to_reg_delay_ps":     _re_int(r"\bmax_input_to_reg_delay_ps\s*:\s*(\d+)", text),
        "max_reg_to_output_delay_ps":    _re_int(r"\bmax_reg_to_output_delay_ps\s*:\s*(\d+)", text),
        "max_feedthrough_path_delay_ps": _re_int(r"\bmax_feedthrough_path_delay_ps\s*:\s*(\d+)", text),
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
      1. benchmark_output    → num_stages, area, max_stage_delay, flops (most complete)
      2. schedule textproto  → num_stages, min_clock (fallback)
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
        m.max_stage_delay_ps   = bm["max_stage_delay_ps"]
        m.total_delay_ps       = bm["total_delay_ps"]
        m.total_area_um2       = bm["total_area_um2"]
        m.total_pipeline_flops = bm["total_pipeline_flops"]
        m.num_stages           = bm["num_stages"]
        m.min_clock_period_ps  = bm["min_clock_period_ps"]
        m.min_stage_slack_ps   = bm["min_stage_slack_ps"]
        m.stage_delays         = bm["stage_delays"]
        m.scheduler_runtime_s  = bm["scheduler_runtime_s"]

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
        if m.critical_path_ps == 0:
            m.critical_path_ps = max(
                m.max_reg_to_reg_delay_ps,
                m.max_input_to_reg_delay_ps,
                m.max_reg_to_output_delay_ps,
            )
        if m.max_stage_delay_ps == 0:
            m.max_stage_delay_ps = m.max_reg_to_reg_delay_ps or m.critical_path_ps

    # ── 4. Verilog fallback for flop count ────────────────────────────────────
    elif has_verilog and m.flop_count == 0 and m.total_pipeline_flops == 0:
        m.flop_count = parse_verilog_fallback(verilog_path)

    # Ensure max_stage_delay_ps is always set (fast mode: no benchmark data)
    if m.max_stage_delay_ps == 0:
        m.max_stage_delay_ps = m.critical_path_ps

    m._compute()
    return m
