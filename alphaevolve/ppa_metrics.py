"""
alphaevolve/ppa_metrics.py
──────────────────────────
Parse XLS codegen outputs to extract PPA (Power/Performance/Area) metrics.

Two data sources (used together):
  1. Schedule textproto  (--output_schedule_path)
     → num_stages (pipeline depth / latency)
     → min_clock_period_ps (XLS's own clock estimate)

  2. Block metrics textproto  (--block_metrics_path, XlsMetricsProto)
     → flop_count          total flip-flop bits (accurate area proxy from XLS internals)
     → max_reg_to_reg_delay_ps   critical path between pipeline registers
     → max_input_to_reg_delay_ps first-stage combinational delay
     → max_reg_to_output_delay_ps last-stage combinational delay

  Together these replace the old regex-based Verilog register counting.

Composite score (lower = better):
    score = num_stages * STAGE_WEIGHT
          + flop_count * REG_WEIGHT
          + critical_path_ps * DELAY_WEIGHT
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


# ── Scoring weights ────────────────────────────────────────────────────────────
STAGE_WEIGHT  = 1000   # pipeline stages dominate (latency + throughput)
REG_WEIGHT    = 1      # per-flipflop-bit area proxy
DELAY_WEIGHT  = 0      # delay already encoded in feasibility (clock constraint met)
                       # set > 0 if you want to optimise for faster-than-constraint timing


@dataclass
class PPAMetrics:
    # ── From schedule textproto ────────────────────────────────────────────────
    num_stages: int = 0
    min_clock_period_ps: int = 0

    # ── From block metrics textproto (XlsMetricsProto / BlockMetricsProto) ────
    flop_count: int = 0              # total flip-flop bits (XLS-computed, accurate)
    max_reg_to_reg_delay_ps: int = 0 # critical path: register → register
    max_input_to_reg_delay_ps: int = 0
    max_reg_to_output_delay_ps: int = 0
    max_feedthrough_path_delay_ps: int = 0

    # ── Derived ───────────────────────────────────────────────────────────────
    critical_path_ps: int = 0        # max across all delay metrics
    feasible: bool = False
    score: float = float("inf")

    # Backward-compat alias (old code used pipeline_reg_bits)
    @property
    def pipeline_reg_bits(self) -> int:
        return self.flop_count

    def _compute(self):
        self.critical_path_ps = max(
            self.max_reg_to_reg_delay_ps,
            self.max_input_to_reg_delay_ps,
            self.max_reg_to_output_delay_ps,
            self.max_feedthrough_path_delay_ps,
        )
        if self.feasible:
            self.score = (
                self.num_stages   * STAGE_WEIGHT
                + self.flop_count * REG_WEIGHT
                + self.critical_path_ps * DELAY_WEIGHT
            )


def _re_int(pattern: str, text: str, default: int = 0) -> int:
    """Extract first integer matching pattern from text."""
    m = re.search(pattern, text)
    return int(m.group(1)) if m else default


# ── Parser 1: Schedule textproto ──────────────────────────────────────────────

def parse_schedule(schedule_path: Path | str) -> dict:
    """
    Parse PackageScheduleProto textproto → return dict with:
      num_stages, min_clock_period_ps
    """
    text = Path(schedule_path).read_text(encoding="utf-8")
    return {
        "num_stages":        _re_int(r"\blength\s*:\s*(\d+)", text),
        "min_clock_period_ps": _re_int(r"\bmin_clock_period_ps\s*:\s*(\d+)", text),
    }


# ── Parser 2: Block metrics textproto (XlsMetricsProto) ──────────────────────

def parse_block_metrics(block_metrics_path: Path | str) -> dict:
    """
    Parse XlsMetricsProto textproto written by codegen_main --block_metrics_path.

    Proto structure:
        block_metrics {           ← XlsMetricsProto wrapper
          flop_count: 128
          feedthrough_path_exists: false
          delay_model: "unit"
          max_reg_to_reg_delay_ps: 4
          max_input_to_reg_delay_ps: 4
          max_reg_to_output_delay_ps: 0
          max_feedthrough_path_delay_ps: 0
          bill_of_materials { ... }
        }

    Returns dict of integer fields. Missing fields default to 0.
    """
    text = Path(block_metrics_path).read_text(encoding="utf-8")
    return {
        "flop_count":                  _re_int(r"\bflop_count\s*:\s*(\d+)", text),
        "max_reg_to_reg_delay_ps":     _re_int(r"\bmax_reg_to_reg_delay_ps\s*:\s*(\d+)", text),
        "max_input_to_reg_delay_ps":   _re_int(r"\bmax_input_to_reg_delay_ps\s*:\s*(\d+)", text),
        "max_reg_to_output_delay_ps":  _re_int(r"\bmax_reg_to_output_delay_ps\s*:\s*(\d+)", text),
        "max_feedthrough_path_delay_ps": _re_int(r"\bmax_feedthrough_path_delay_ps\s*:\s*(\d+)", text),
    }


# ── Backward-compat Verilog register counter (fallback only) ──────────────────

def parse_verilog_fallback(verilog_path: Path | str) -> int:
    """
    Count pipeline register bits from generated Verilog (regex-based).
    Used as fallback when block_metrics_path is unavailable.
    """
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
    schedule_path: Path | str | None,
    verilog_path: Path | str | None,
    block_metrics_path: Path | str | None = None,
) -> PPAMetrics:
    """
    Extract full PPA metrics from XLS pipeline outputs.

    Priority for register/delay metrics:
      1. block_metrics_path (XlsMetricsProto) — most accurate, direct from XLS internals
      2. verilog_path regex fallback — approximate, kept for backward compat

    num_stages always comes from the schedule textproto.
    """
    if not schedule_path or not Path(schedule_path).exists():
        return PPAMetrics(feasible=False)

    m = PPAMetrics(feasible=True)

    # ── Schedule: num_stages + min_clock ──────────────────────────────────────
    sched = parse_schedule(schedule_path)
    m.num_stages         = sched["num_stages"]
    m.min_clock_period_ps = sched["min_clock_period_ps"]

    # ── Block metrics: flop_count + delays (preferred) ────────────────────────
    if block_metrics_path and Path(block_metrics_path).exists():
        bm = parse_block_metrics(block_metrics_path)
        m.flop_count                   = bm["flop_count"]
        m.max_reg_to_reg_delay_ps      = bm["max_reg_to_reg_delay_ps"]
        m.max_input_to_reg_delay_ps    = bm["max_input_to_reg_delay_ps"]
        m.max_reg_to_output_delay_ps   = bm["max_reg_to_output_delay_ps"]
        m.max_feedthrough_path_delay_ps = bm["max_feedthrough_path_delay_ps"]

    # ── Verilog fallback (no block_metrics) ───────────────────────────────────
    elif verilog_path and Path(verilog_path).exists():
        m.flop_count = parse_verilog_fallback(verilog_path)

    m._compute()
    return m
