#!/usr/bin/env python3
"""
scripts/analyze_results.py
──────────────────────────
Cross-experiment PPA analysis and visualisation.

Reads evolution_log.csv from each experiment, the per-iteration benchmark_main
outputs to re-extract stage delays, and the SDC baseline files placed in each
design folder.  Produces four charts saved to --output_dir:

  1. score_progression.png  — ppa_score vs iteration, one line per experiment
  2. timing_utilization.png — max_stage_delay / clock_period vs iteration
  3. balance_cv.png         — balance_cv_norm vs iteration (lower = better spread)
  4. summary_radar.png      — radar chart comparing best agent vs SDC on 4 axes

Usage
-----
  python scripts/analyze_results.py \
    --results_dir   results/ \
    --experiments   final_exp_006 final_exp_007 final_exp_008 final_exp_009 final_exp_0010 \
    --designs_dir   designs/ \
    --output_dir    results/analysis/

Each experiment must have a matching entry in EXPERIMENT_META below (or pass
--meta_json PATH to override with a JSON file of the same shape).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path

# ── Experiment metadata ───────────────────────────────────────────────────────
# Maps experiment folder name → (design_stem, pipeline_stages, clock_period_ps,
#                                 sdc_benchmark_relative_path)
EXPERIMENT_META: dict[str, tuple[str, int, int, str]] = {
    "final_exp_006":  ("sha256",               8,  25_000, "sha256/sha_benchmark.txt"),
    "final_exp_007":  ("idct_chen",             8,  50_000, "idct/idct_benchmark.txt"),
    "final_exp_008":  ("gemm4x4_int",           4,   1_000, "gemm4x4_int/gemm4x4_int.benchmark.txt"),
    "final_exp_009":  ("fusion_tile",           4,   8_000, "irregular_fusion/fusion_tile_benchmark.txt"),
    "final_exp_0010": ("bitonic_sort_wrapper", 64,  70_000, "bitonic_sort/bitonic_sort_benchmark.txt"),
}

PRETTY_NAMES = {
    "final_exp_006":  "SHA-256 (8 stg, 25 ns)",
    "final_exp_007":  "IDCT (8 stg, 50 ns)",
    "final_exp_008":  "GEMM 4×4 (4 stg, 1 ns)",
    "final_exp_009":  "Fusion Tile (4 stg, 8 ns)",
    "final_exp_0010": "Bitonic Sort (64 stg, 70 ns)",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def balance_cv(delays: list[int]) -> float:
    """Normalised CV of stage delays: 0 = perfect balance, 1 = worst skew."""
    n = len(delays)
    if n <= 1:
        return 0.0
    mean = sum(delays) / n
    if mean == 0.0:
        return 0.0
    variance = sum((d - mean) ** 2 for d in delays) / n
    cv = math.sqrt(variance) / mean
    return min(cv / math.sqrt(n - 1), 1.0)


def parse_stage_delays(text: str) -> list[int]:
    """Extract per-stage delays from benchmark_main stdout."""
    delays: list[int] = []
    for m in re.finditer(
        r"\[Stage\s+\d+\]\s+flops:[^\n]*\n\s*nodes:\s*\d+,\s*delay:\s*(\d+)ps",
        text,
        re.MULTILINE,
    ):
        delays.append(int(m.group(1)))
    if not delays:
        # Single-stage fallback
        fb = re.search(r"^\s*nodes:\s*\d+,\s*delay:\s*(\d+)ps", text, re.MULTILINE)
        if fb:
            delays = [int(fb.group(1))]
    return delays


def parse_benchmark_file(path: Path) -> dict:
    """Parse a benchmark_main output file for summary metrics."""
    text = path.read_text(encoding="utf-8", errors="replace")
    delays = parse_stage_delays(text)

    cp_match = re.search(r"Critical path delay:\s*(\d+)ps", text)
    area_match = re.search(r"Total area:\s*([\d.]+)\s*um2", text)
    flops_match = re.search(r"Total pipeline flops:\s*(\d+)", text)
    stages_set = set(int(m.group(1)) for m in re.finditer(r"\[Stage\s+(\d+)\]", text))

    max_stg = max(delays) if delays else 0
    return {
        "stage_delays":       delays,
        "max_stage_delay_ps": max_stg,
        "num_stages":         len(stages_set) if stages_set else text.count("Pipeline:"),
        "total_area_um2":     float(area_match.group(1)) if area_match else 0.0,
        "total_pipeline_flops": int(flops_match.group(1)) if flops_match else 0,
        "critical_path_ps":   int(cp_match.group(1)) if cp_match else 0,
        "balance_cv":         balance_cv(delays),
    }


def load_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ── Per-iteration benchmark re-extraction ────────────────────────────────────

def extract_iter_benchmark(exp_dir: Path, design_stem: str) -> dict[int, dict]:
    """
    Walk eval_runs/ and return {iteration: parsed_benchmark_dict} for
    iterations where a benchmark file exists and was a success.
    """
    results: dict[int, dict] = {}
    eval_dir = exp_dir / "eval_runs"
    if not eval_dir.exists():
        return results
    for iter_dir in sorted(eval_dir.iterdir()):
        m = re.match(r"iter(\d+)_island\d+$", iter_dir.name)
        if not m or not iter_dir.is_dir():
            continue
        iteration = int(m.group(1))
        bm_file = iter_dir / design_stem / f"{design_stem}_benchmark.txt"
        if not bm_file.exists():
            continue
        try:
            results[iteration] = parse_benchmark_file(bm_file)
        except Exception:
            pass
    return results


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results_dir",  default="results",
                    help="Parent directory that contains experiment sub-folders.")
    ap.add_argument("--experiments",  nargs="+",
                    default=list(EXPERIMENT_META.keys()),
                    help="Experiment folder names to include.")
    ap.add_argument("--designs_dir",  default="designs",
                    help="Root designs directory (for SDC baseline files).")
    ap.add_argument("--output_dir",   default="results/analysis",
                    help="Where to write the PNG charts.")
    ap.add_argument("--meta_json",    default=None,
                    help="Optional JSON file overriding EXPERIMENT_META.")
    args = ap.parse_args()

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("ERROR: matplotlib and numpy are required.  pip install matplotlib numpy")
        return 1

    results_dir = Path(args.results_dir)
    designs_dir = Path(args.designs_dir)
    output_dir  = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    meta = EXPERIMENT_META
    if args.meta_json:
        meta = json.load(open(args.meta_json))

    experiments = [e for e in args.experiments if e in meta]
    if not experiments:
        print("No recognised experiments found.  Check --experiments and EXPERIMENT_META.")
        return 1

    # ── Load data ─────────────────────────────────────────────────────────────
    exp_data: dict[str, dict] = {}
    for exp in experiments:
        design, stages, clock, sdc_rel = meta[exp]
        exp_dir  = results_dir / exp
        csv_path = exp_dir / "evolution_log.csv"
        if not csv_path.exists():
            print(f"  [skip] {exp}: no evolution_log.csv")
            continue

        rows = load_csv(csv_path)
        # Keep only successful rows
        success = [r for r in rows if r.get("build_status") == "success"]

        # Re-extract per-iteration benchmark data (has stage_delays / balance_cv)
        iter_bm = extract_iter_benchmark(exp_dir, design)

        # SDC baseline
        sdc_path = designs_dir / sdc_rel
        sdc_info = parse_benchmark_file(sdc_path) if sdc_path.exists() else {}

        exp_data[exp] = {
            "design": design, "stages": stages, "clock": clock,
            "rows": success,
            "iter_bm": iter_bm,
            "sdc": sdc_info,
        }
        print(f"  Loaded {exp}: {len(success)} successful candidates, "
              f"{len(iter_bm)} benchmark snapshots, "
              f"SDC={'yes' if sdc_info else 'no'}")

    if not exp_data:
        print("No data loaded — aborting.")
        return 1

    colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    exp_colors = {exp: colors[i % len(colors)] for i, exp in enumerate(exp_data)}

    # ─────────────────────────────────────────────────────────────────────────
    # Chart 1 — Score progression
    # ─────────────────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 5))
    for exp, d in exp_data.items():
        iters, scores = [], []
        for r in d["rows"]:
            try:
                iters.append(int(r["iteration"]))
                scores.append(float(r["ppa_score"]))
            except (ValueError, KeyError):
                pass
        if iters:
            ax.scatter(iters, scores, color=exp_colors[exp], s=30, alpha=0.6)
            # Running best
            best, best_iters, bests = float("inf"), [], []
            for it, sc in sorted(zip(iters, scores)):
                if sc < best:
                    best = sc
                    best_iters.append(it)
                    bests.append(best)
            ax.step(best_iters, bests, where="post", color=exp_colors[exp],
                    linewidth=2, label=PRETTY_NAMES.get(exp, exp))

    ax.set_xlabel("Iteration")
    ax.set_ylabel("PPA Score (lower = better)")
    ax.set_title("Score Progression — Best Score per Experiment")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_dir / "score_progression.png", dpi=150)
    plt.close(fig)
    print(f"  → score_progression.png")

    # ─────────────────────────────────────────────────────────────────────────
    # Chart 2 — Timing utilisation (max_stage_delay / clock_period)
    # ─────────────────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 5))
    for exp, d in exp_data.items():
        clock = d["clock"]
        iters_util: list[tuple[int, float]] = []
        for it, bm in sorted(d["iter_bm"].items()):
            if bm["max_stage_delay_ps"] > 0:
                iters_util.append((it, bm["max_stage_delay_ps"] / clock))
        if iters_util:
            its, utils = zip(*iters_util)
            ax.plot(its, utils, "o-", color=exp_colors[exp], markersize=5,
                    linewidth=1.5, label=PRETTY_NAMES.get(exp, exp))

        # SDC reference line
        sdc = d["sdc"]
        if sdc.get("max_stage_delay_ps", 0) > 0:
            ax.axhline(sdc["max_stage_delay_ps"] / clock,
                       color=exp_colors[exp], linestyle="--", linewidth=1,
                       alpha=0.7)

    ax.set_xlabel("Iteration")
    ax.set_ylabel("Timing Utilisation  (max_stage_delay / clock_period)")
    ax.set_title("Timing Utilisation per Iteration\n(dashed = SDC baseline; lower = more slack)")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_dir / "timing_utilization.png", dpi=150)
    plt.close(fig)
    print(f"  → timing_utilization.png")

    # ─────────────────────────────────────────────────────────────────────────
    # Chart 3 — Balance CV
    # ─────────────────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 5))
    for exp, d in exp_data.items():
        iters_cv: list[tuple[int, float]] = []
        for it, bm in sorted(d["iter_bm"].items()):
            cv = bm.get("balance_cv", 0.0)
            iters_cv.append((it, cv))
        if iters_cv:
            its, cvs = zip(*iters_cv)
            ax.plot(its, cvs, "o-", color=exp_colors[exp], markersize=5,
                    linewidth=1.5, label=PRETTY_NAMES.get(exp, exp))

        # SDC reference line
        sdc = d["sdc"]
        if "balance_cv" in sdc:
            ax.axhline(sdc["balance_cv"], color=exp_colors[exp],
                       linestyle="--", linewidth=1, alpha=0.7)

    ax.set_xlabel("Iteration")
    ax.set_ylabel("Balance CV norm  (0 = perfect, 1 = worst skew)")
    ax.set_title("Stage-Load Balance per Iteration\n(dashed = SDC baseline; lower = more even)")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_dir / "balance_cv.png", dpi=150)
    plt.close(fig)
    print(f"  → balance_cv.png")

    # ─────────────────────────────────────────────────────────────────────────
    # Chart 4 — Radar: best agent vs SDC per experiment
    # ─────────────────────────────────────────────────────────────────────────
    # 4 axes: timing utilisation, balance CV, area (norm), reg bits (norm)
    axes_labels = ["Timing\nUtil", "Balance\nCV", "Area\n(norm)", "Reg Bits\n(norm)"]
    N = len(axes_labels)
    angles = [n / float(N) * 2 * math.pi for n in range(N)]
    angles += angles[:1]  # close the polygon

    # Reference normalisation (across all experiments, for consistent radar scale)
    all_areas  = [bm["total_area_um2"] for d in exp_data.values()
                  for bm in d["iter_bm"].values() if bm["total_area_um2"] > 0]
    all_regs   = [float(r["pipeline_reg_bits"]) for d in exp_data.values()
                  for r in d["rows"] if float(r.get("pipeline_reg_bits", 0)) > 0]
    ref_area   = max(all_areas) if all_areas else 100_000.0
    ref_regs   = max(all_regs)  if all_regs  else 10_000.0

    n_exps = len(exp_data)
    cols   = min(3, n_exps)
    rows_c = math.ceil(n_exps / cols)
    fig, axes_r = plt.subplots(rows_c, cols, figsize=(5 * cols, 4.5 * rows_c),
                                subplot_kw=dict(polar=True))
    if n_exps == 1:
        axes_r = [[axes_r]]
    elif rows_c == 1:
        axes_r = [list(axes_r)]
    else:
        axes_r = [list(row) for row in axes_r]

    for idx, (exp, d) in enumerate(exp_data.items()):
        r_i, c_i = divmod(idx, cols)
        ax_r = axes_r[r_i][c_i]
        clock = d["clock"]

        def _radar(bm_dict: dict, reg_bits: float) -> list[float]:
            timing = (bm_dict.get("max_stage_delay_ps", 0) / clock) if clock else 0
            bal    = bm_dict.get("balance_cv", 0.0)
            area   = bm_dict.get("total_area_um2", 0.0) / ref_area
            regs   = reg_bits / ref_regs
            return [min(v, 1.0) for v in [timing, bal, area, regs]]

        # Best agent candidate
        best_score = float("inf")
        best_row   = None
        for r in d["rows"]:
            try:
                sc = float(r["ppa_score"])
                if sc < best_score:
                    best_score = sc
                    best_row   = r
            except (ValueError, KeyError):
                pass

        # Find its benchmark snapshot
        best_it  = int(best_row["iteration"]) if best_row else -1
        best_bm  = d["iter_bm"].get(best_it, {})
        best_reg = float(best_row.get("pipeline_reg_bits", 0)) if best_row else 0.0
        agent_vals = _radar(best_bm, best_reg) + [None]
        agent_vals[-1] = agent_vals[0]  # close polygon

        # SDC
        sdc_vals = _radar(d["sdc"], d["sdc"].get("total_pipeline_flops", 0)) + [None]
        sdc_vals[-1] = sdc_vals[0]

        ax_r.set_theta_offset(math.pi / 2)
        ax_r.set_theta_direction(-1)
        ax_r.set_thetagrids([a * 180 / math.pi for a in angles[:-1]], axes_labels)
        ax_r.set_ylim(0, 1)
        ax_r.set_yticks([0.25, 0.5, 0.75, 1.0])
        ax_r.set_yticklabels(["0.25", "0.5", "0.75", "1.0"], fontsize=6)

        if any(v is not None and v > 0 for v in agent_vals[:-1]):
            ax_r.plot(angles, agent_vals, "o-", color=exp_colors[exp],
                      linewidth=2, label=f"Agent (best)")
            ax_r.fill(angles, agent_vals, alpha=0.15, color=exp_colors[exp])

        if any(v is not None and v > 0 for v in sdc_vals[:-1]):
            ax_r.plot(angles, sdc_vals, "s--", color="gray",
                      linewidth=1.5, label="SDC baseline")
            ax_r.fill(angles, sdc_vals, alpha=0.1, color="gray")

        ax_r.set_title(PRETTY_NAMES.get(exp, exp), size=9, pad=12)
        ax_r.legend(loc="upper right", bbox_to_anchor=(1.35, 1.1), fontsize=7)

    # Hide any unused subplot panels
    for idx in range(n_exps, rows_c * cols):
        r_i, c_i = divmod(idx, cols)
        axes_r[r_i][c_i].set_visible(False)

    fig.suptitle("Radar: Best Agent vs SDC  (all axes: lower = better)", y=1.02)
    fig.tight_layout()
    fig.savefig(output_dir / "summary_radar.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  → summary_radar.png")

    # ── Print text summary table ──────────────────────────────────────────────
    print()
    print("=" * 90)
    print(f"{'Experiment':<20} {'Design':<22} {'Stg':>4} {'Clock':>7}  "
          f"{'Agent best':>10}  {'Stg':>4}  {'Delay':>7}  {'Bal':>5}  {'Area':>7}  "
          f"{'SDC Delay':>9}  {'SDC Bal':>7}")
    print("-" * 90)
    for exp, d in exp_data.items():
        clock   = d["clock"]
        best_sc = min((float(r["ppa_score"]) for r in d["rows"]), default=float("nan"))
        best_r  = min(d["rows"], key=lambda r: float(r.get("ppa_score", "inf")), default={})
        best_it = int(best_r.get("iteration", -1))
        best_bm = d["iter_bm"].get(best_it, {})
        delay   = best_bm.get("max_stage_delay_ps", int(best_r.get("max_stage_delay_ps", 0)))
        bal     = best_bm.get("balance_cv", 0.0)
        area    = best_bm.get("total_area_um2", 0.0)
        n_stg   = int(best_r.get("num_stages", 0))

        sdc_d   = d["sdc"].get("max_stage_delay_ps", 0)
        sdc_bal = d["sdc"].get("balance_cv", 0.0)

        print(f"{exp:<20} {d['design']:<22} {d['stages']:>4} {clock:>7}  "
              f"{best_sc:>10.4f}  {n_stg:>4}  {delay:>7}  {bal:>5.3f}  {area:>7.0f}  "
              f"{sdc_d:>9}  {sdc_bal:>7.3f}")
    print("=" * 90)
    print()
    print(f"Charts written to: {output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
