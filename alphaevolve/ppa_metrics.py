"""
alphaevolve/ppa_metrics.py
──────────────────────────
Parse XLS schedule textproto output and generated Verilog to extract
PPA (Power/Performance/Area) proxy metrics:

  - num_stages:           pipeline depth (fewer is better for latency)
  - pipeline_reg_bits:    total register bits across stage boundaries
                          (fewer is better for area/power)
  - max_stage_delay_ps:   critical-path delay of the worst stage
                          (lower → can run at faster clock)
  - feasible:             True if codegen succeeded and schedule is valid

Composite score (lower = better):
    score = num_stages * STAGE_WEIGHT + pipeline_reg_bits * REG_WEIGHT
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


# ── Tunable scoring weights ────────────────────────────────────────────────────
# Stages are typically the dominant PPA driver for pipeline depth research.
STAGE_WEIGHT = 1000
REG_WEIGHT = 1


@dataclass
class PPAMetrics:
    num_stages: int = 0
    pipeline_reg_bits: int = 0
    max_stage_delay_ps: int = 0
    min_clock_period_ps: int = 0
    feasible: bool = False
    score: float = float("inf")

    def __post_init__(self):
        if self.feasible:
            self.score = (
                self.num_stages * STAGE_WEIGHT
                + self.pipeline_reg_bits * REG_WEIGHT
            )


def parse_schedule(schedule_path: Path | str) -> PPAMetrics:
    """
    Parse a PipelineSchedule textproto file produced by codegen_main's
    --output_schedule_path flag.

    The textproto format (PackageScheduleProto → PipelineScheduleProto):

        schedules {
          key: "__pkg__fn"
          value {
            stages {
              stage: 0
              timed_nodes { node: "..." node_delay_ps: 100 path_delay_ps: 200 }
              ...
            }
            length: 3
            min_clock_period_ps: 200
          }
        }

    We extract num_stages from `length`, max path_delay from `path_delay_ps`,
    and min_clock_period_ps.
    """
    text = Path(schedule_path).read_text(encoding="utf-8")

    # Extract `length` (number of pipeline stages)
    length_match = re.search(r"\blength\s*:\s*(\d+)", text)
    num_stages = int(length_match.group(1)) if length_match else 0

    # Extract minimum clock period reported by XLS
    min_clock_match = re.search(r"\bmin_clock_period_ps\s*:\s*(\d+)", text)
    min_clock_ps = int(min_clock_match.group(1)) if min_clock_match else 0

    # Max critical-path delay across all timed nodes
    path_delays = [int(m) for m in re.findall(r"\bpath_delay_ps\s*:\s*(\d+)", text)]
    max_stage_delay = max(path_delays) if path_delays else 0

    return PPAMetrics(
        num_stages=num_stages,
        pipeline_reg_bits=0,      # filled by parse_verilog below
        max_stage_delay_ps=max_stage_delay,
        min_clock_period_ps=min_clock_ps,
        feasible=True,
    )


def parse_verilog(verilog_path: Path | str) -> int:
    """
    Count pipeline register bits from generated Verilog.
    XLS emits pipeline registers as:
        reg [N:0] p0_..._reg;
    We sum (N+1) for each such declaration.
    Returns total register bits.
    """
    text = Path(verilog_path).read_text(encoding="utf-8")
    total_bits = 0
    # Match: reg [N:0] <name>; or reg <name>;  (1-bit registers)
    for m in re.finditer(r"\breg\s+(?:\[(\d+):(\d+)\]\s+)?\w+\s*;", text):
        if m.group(1) is not None:
            high, low = int(m.group(1)), int(m.group(2))
            total_bits += high - low + 1
        else:
            total_bits += 1
    return total_bits


def extract_ppa(
    schedule_path: Path | str | None,
    verilog_path: Path | str | None,
) -> PPAMetrics:
    """
    Extract full PPA metrics from XLS pipeline outputs.
    Gracefully handles missing files.
    """
    if not schedule_path or not Path(schedule_path).exists():
        return PPAMetrics(feasible=False)
    if not verilog_path or not Path(verilog_path).exists():
        return PPAMetrics(feasible=False)

    metrics = parse_schedule(schedule_path)
    metrics.pipeline_reg_bits = parse_verilog(verilog_path)
    # Recompute score now that reg_bits is populated
    metrics.__post_init__()
    return metrics
