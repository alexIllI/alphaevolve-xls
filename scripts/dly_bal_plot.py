#!/usr/bin/env python3
"""
Plot delay-vs-balance metrics from one or more AlphaEvolve-XLS experiment runs.

Examples
--------
python scripts/dly_bal_plot.py ^
  --input_dir "D:\\final\\alphaevolve-xls\\results\\final_exp_0010"

python scripts/dly_bal_plot.py ^
  --input_dir "D:\\final\\alphaevolve-xls\\results\\final_exp_0010" ^
  --extra_dir "D:\\final\\alphaevolve-xls\\results\\final_exp_009" "D:\\final\\alphaevolve-xls\\results\\final_exp_011"
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "results" / "analytics"


@dataclass
class IterationPoint:
    iteration: int
    score: float
    max_stage_delay_ps: int
    delay_utilization: float
    balance_penalty: float | None
    num_stages: int
    build_status: str
    note: str


@dataclass
class ExperimentSeries:
    name: str
    exp_dir: Path
    display_name: str
    design_name: str
    input_file: str
    clock_period_ps: int
    points: list[IterationPoint]
    best_point: IterationPoint | None
    successful_count: int
    failed_count: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input_dir", required=True, help="Primary experiment folder.")
    parser.add_argument(
        "--extra_dir",
        nargs="*",
        default=[],
        help="Optional additional experiment folders to compare.",
    )
    parser.add_argument(
        "--output_dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory where analytics artifacts are written.",
    )
    return parser.parse_args()


def load_run_meta(exp_dir: Path) -> dict:
    db_path = exp_dir / "candidates_db.sqlite"
    if not db_path.exists():
        return {}
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute("SELECT value FROM run_meta WHERE key = 'config'").fetchone()
        return json.loads(row[0]) if row else {}
    finally:
        con.close()


def infer_names(exp_dir: Path, meta: dict) -> tuple[str, str, str, int]:
    args = meta.get("args", {}) if isinstance(meta, dict) else {}
    ppa = meta.get("ppa", {}) if isinstance(meta, dict) else {}
    input_file = str(args.get("input_file", ""))
    design_name = Path(input_file).stem if input_file else exp_dir.name
    clock_period_ps = int(
        args.get("clock_period") or ppa.get("clock_period_ps") or 0
    )
    display_name = f"{design_name} ({exp_dir.name})" if design_name != exp_dir.name else design_name
    return display_name, design_name, input_file, clock_period_ps


def load_csv_rows(csv_path: Path) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def group_best_rows_by_iteration(rows: Iterable[dict]) -> dict[int, dict]:
    grouped: dict[int, list[dict]] = {}
    for row in rows:
        try:
            iteration = int(row.get("iteration", ""))
        except ValueError:
            continue
        grouped.setdefault(iteration, []).append(row)

    best_by_iteration: dict[int, dict] = {}
    for iteration, candidates in grouped.items():
        success = [r for r in candidates if r.get("build_status") == "success"]
        chosen = None
        if success:
            chosen = min(success, key=lambda r: float(r.get("ppa_score", "inf")))
        else:
            chosen = min(
                candidates,
                key=lambda r: (
                    0 if r.get("build_status") == "run_failed" else 1,
                    float(r.get("ppa_score", "inf")),
                ),
            )
        best_by_iteration[iteration] = chosen
    return best_by_iteration


def parse_stage_delays(text: str) -> list[int]:
    import re

    delays: list[int] = []
    for match in re.finditer(
        r"\[Stage\s+\d+\]\s+flops:[^\n]*\n\s*nodes:\s*\d+,\s*delay:\s*(\d+)ps",
        text,
        re.MULTILINE,
    ):
        delays.append(int(match.group(1)))
    if not delays:
        fallback = re.search(r"^\s*nodes:\s*\d+,\s*delay:\s*(\d+)ps", text, re.MULTILINE)
        if fallback:
            delays = [int(fallback.group(1))]
    return delays


def balance_penalty(stage_delays: list[int], clock_period_ps: int) -> float | None:
    if not stage_delays or clock_period_ps <= 0:
        return None
    utilizations = [delay / clock_period_ps for delay in stage_delays]
    avg = mean(utilizations)
    if avg == 0.0:
        return 0.0
    variance = sum((value - avg) ** 2 for value in utilizations) / len(utilizations)
    spread = math.sqrt(variance)
    overload = math.sqrt(
        sum(max(0.0, value - 1.0) ** 2 for value in utilizations) / len(utilizations)
    )
    return spread + 2.0 * overload


def parse_benchmark_info(benchmark_path: Path, clock_period_ps: int) -> tuple[float | None, list[int]]:
    if benchmark_path is None or not benchmark_path.exists():
        return None, []
    text = benchmark_path.read_text(encoding="utf-8", errors="replace")
    delays = parse_stage_delays(text)
    return balance_penalty(delays, clock_period_ps), delays


def find_benchmark_path(exp_dir: Path, iteration: int, design_name: str) -> Path | None:
    eval_root = exp_dir / "eval_runs"
    pattern = f"iter{iteration:04d}_island*"
    for iter_dir in sorted(eval_root.glob(pattern)):
        candidate = iter_dir / design_name / f"{design_name}_benchmark.txt"
        if candidate.exists():
            return candidate
    return None


def build_series(exp_dir: Path) -> ExperimentSeries:
    csv_path = exp_dir / "evolution_log.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"Missing evolution_log.csv in {exp_dir}")

    meta = load_run_meta(exp_dir)
    display_name, design_name, input_file, clock_period_ps = infer_names(exp_dir, meta)
    rows = load_csv_rows(csv_path)
    best_rows = group_best_rows_by_iteration(rows)

    points: list[IterationPoint] = []
    successful_count = 0
    failed_count = 0

    for iteration in sorted(best_rows):
        row = best_rows[iteration]
        status = row.get("build_status", "")
        score = float(row.get("ppa_score", "inf"))
        delay_ps = int(float(row.get("max_stage_delay_ps", "0") or 0))
        num_stages = int(float(row.get("num_stages", "0") or 0))
        benchmark_path = find_benchmark_path(exp_dir, iteration, design_name)
        bal_penalty, _stage_delays = parse_benchmark_info(benchmark_path, clock_period_ps)

        if status == "success":
            successful_count += 1
        else:
            failed_count += 1

        points.append(
            IterationPoint(
                iteration=iteration,
                score=score,
                max_stage_delay_ps=delay_ps,
                delay_utilization=(delay_ps / clock_period_ps) if clock_period_ps > 0 else float("nan"),
                balance_penalty=bal_penalty,
                num_stages=num_stages,
                build_status=status,
                note=row.get("notes", ""),
            )
        )

    success_points = [point for point in points if point.build_status == "success"]
    best_point = min(success_points, key=lambda point: point.score) if success_points else None

    return ExperimentSeries(
        name=exp_dir.name,
        exp_dir=exp_dir,
        display_name=display_name,
        design_name=design_name,
        input_file=input_file,
        clock_period_ps=clock_period_ps,
        points=points,
        best_point=best_point,
        successful_count=successful_count,
        failed_count=failed_count,
    )


def ensure_plot_deps():
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError("matplotlib is required for PNG plots: pip install matplotlib") from exc
    return plt


def sanitize_prefix(names: list[str]) -> str:
    safe = "__vs__".join(names)
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in safe)


def write_iteration_csv(series_list: list[ExperimentSeries], output_path: Path) -> None:
    fieldnames = [
        "experiment",
        "design",
        "iteration",
        "build_status",
        "score",
        "max_stage_delay_ps",
        "delay_utilization",
        "balance_penalty",
        "num_stages",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for series in series_list:
            for point in series.points:
                writer.writerow(
                    {
                        "experiment": series.name,
                        "design": series.design_name,
                        "iteration": point.iteration,
                        "build_status": point.build_status,
                        "score": f"{point.score:.6f}" if math.isfinite(point.score) else "inf",
                        "max_stage_delay_ps": point.max_stage_delay_ps,
                        "delay_utilization": (
                            f"{point.delay_utilization:.6f}"
                            if math.isfinite(point.delay_utilization)
                            else ""
                        ),
                        "balance_penalty": (
                            f"{point.balance_penalty:.6f}"
                            if point.balance_penalty is not None
                            else ""
                        ),
                        "num_stages": point.num_stages,
                    }
                )


def plot_delay_balance(series_list: list[ExperimentSeries], output_path: Path) -> None:
    plt = ensure_plot_deps()
    fig, ax = plt.subplots(figsize=(11, 8))
    colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    
    # Filter out failed runs (runs with no successful points)
    successful_series = [s for s in series_list if s.successful_count > 0]
    if not successful_series:
        raise RuntimeError("No successful runs to plot")

    for index, series in enumerate(successful_series):
        color = colors[index % len(colors)]
        valid = [
            point
            for point in series.points
            if point.build_status == "success"
            and point.balance_penalty is not None
            and math.isfinite(point.delay_utilization)
        ]
        if not valid:
            continue

        xs = [point.delay_utilization for point in valid]
        ys = [point.balance_penalty for point in valid]
        ax.scatter(xs, ys, color=color, s=40, alpha=0.85, label=series.display_name)

        if series.best_point and series.best_point.balance_penalty is not None:
            best = series.best_point
            ax.scatter(
                [best.delay_utilization],
                [best.balance_penalty],
                color=color,
                s=180,
                marker="*",
                edgecolors="black",
                linewidths=0.8,
                zorder=5,
            )

    ax.axvline(1.0, color="gray", linestyle="--", linewidth=1.0, alpha=0.7)
    ax.set_xlabel("Delay Utilization = max_stage_delay_ps / clock_period_ps")
    ax.set_ylabel("Balance Penalty")
    ax.set_title("Delay-Balance Scatter by Iteration")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best", fontsize=9)
    fig.tight_layout()
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def plot_score_progression(series_list: list[ExperimentSeries], output_path: Path) -> None:
    plt = ensure_plot_deps()
    fig, ax = plt.subplots(figsize=(11, 6))
    colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    
    # Filter out failed runs (runs with no successful points)
    successful_series = [s for s in series_list if s.successful_count > 0]
    if not successful_series:
        raise RuntimeError("No successful runs to plot")

    for index, series in enumerate(successful_series):
        color = colors[index % len(colors)]
        valid = [point for point in series.points if point.build_status == "success"]
        if not valid:
            continue
        valid.sort(key=lambda point: point.iteration)
        xs = [point.iteration for point in valid]
        ys = [point.score for point in valid]
        ax.plot(xs, ys, color=color, alpha=0.8, linewidth=1.3, marker="o", markersize=4, label=series.display_name)

    ax.set_xlabel("Iteration")
    ax.set_ylabel("PPA Score")
    ax.set_title("Score Progression")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best", fontsize=9)
    ax.xaxis.set_major_locator(plt.MaxNLocator(integer=True))
    fig.tight_layout()
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def build_analysis(series_list: list[ExperimentSeries]) -> str:
    lines: list[str] = []
    lines.append("# Delay-Balance Analysis")
    lines.append("")
    lines.append("## Rules Used")
    lines.append("")
    lines.append("- One displayed point per iteration: the best successful candidate for that iteration. If an iteration had no successful candidate, it is counted in the summary but omitted from the delay-balance scatter.")
    lines.append("- Delay is plotted as normalized utilization (`max_stage_delay_ps / clock_period_ps`) so runs with different clocks remain comparable.")
    lines.append("- Balance uses the current clock-aware penalty: `spread(utilization) + 2 * overload_rms`.")
    lines.append("- The starred point is the best score found in that experiment.")
    lines.append("")
    lines.append("## Per-Experiment Summary")
    lines.append("")

    for series in series_list:
        lines.append(f"### {series.display_name}")
        lines.append("")
        lines.append(f"- Input file: `{series.input_file or series.design_name}`")
        lines.append(f"- Clock period: `{series.clock_period_ps} ps`")
        lines.append(f"- Successful plotted iterations: `{series.successful_count}`")
        lines.append(f"- Failed / skipped iterations: `{series.failed_count}`")

        valid = [
            point for point in series.points
            if point.build_status == "success" and point.balance_penalty is not None
        ]
        if not valid or not series.best_point:
            lines.append("- No valid success points with balance data were available.")
            lines.append("")
            continue

        first = min(valid, key=lambda point: point.iteration)
        best = series.best_point
        avg_balance = mean(point.balance_penalty for point in valid if point.balance_penalty is not None)
        avg_util = mean(point.delay_utilization for point in valid)

        lines.append(f"- First successful iteration: `i{first.iteration}` score `{first.score:.4f}`")
        lines.append(f"- Best iteration: `i{best.iteration}` score `{best.score:.4f}`")
        lines.append(
            f"- Best point: delay utilization `{best.delay_utilization:.4f}`, "
            f"balance penalty `{best.balance_penalty:.4f}`, stages `{best.num_stages}`"
        )
        lines.append(
            f"- Average successful point: delay utilization `{avg_util:.4f}`, "
            f"balance penalty `{avg_balance:.4f}`"
        )

        improvement = first.score - best.score
        if improvement > 0:
            lines.append(f"- Score improvement from first success to best: `{improvement:.4f}`")
        else:
            lines.append("- No score improvement beyond the first successful iteration.")

        low_delay = [point for point in valid if point.delay_utilization < 0.9]
        low_balance = [point for point in valid if (point.balance_penalty or 0.0) < 0.15]
        if low_delay and low_balance:
            lines.append(
                "- There are points that are both comfortably under the target clock and well balanced; "
                "those are strong candidates for manual inspection."
            )
        elif low_delay:
            lines.append(
                "- Timing looks comfortable on some iterations, but balance remains noisy. "
                "The scheduler may be meeting the clock while still packing work unevenly."
            )
        elif low_balance:
            lines.append(
                "- Stage balance is good on some iterations, but the worst stage is still close to or over the clock. "
                "Focus on reducing the heaviest stage rather than only equalizing spread."
            )
        else:
            lines.append(
                "- Most successful points are either close to the clock limit, imbalanced, or both. "
                "That usually means the heuristic is still struggling with stage placement rather than compile/runtime issues."
            )
        lines.append("")

    if len(series_list) > 1:
        lines.append("## Cross-Run Reading")
        lines.append("")
        lines.append("- Prefer the run whose cluster is furthest toward the lower-left: lower delay utilization and lower balance penalty.")
        lines.append("- If one run has slightly worse delay utilization but much lower balance penalty, it may still be the healthier scheduler because it spreads timing risk across more stages.")
        lines.append("- If a run has many failures but a great best point, it may be high variance rather than robust. Use the success/failure counts together with the scatter.")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    exp_dirs = [Path(args.input_dir)] + [Path(path) for path in args.extra_dir]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    series_list = [build_series(exp_dir) for exp_dir in exp_dirs]
    prefix = sanitize_prefix([series.name for series in series_list])
    
    # Create a subfolder for this run's artifacts
    run_output_dir = output_dir / prefix
    run_output_dir.mkdir(parents=True, exist_ok=True)

    csv_path = run_output_dir / f"{prefix}_iteration_metrics.csv"
    scatter_path = run_output_dir / f"{prefix}_delay_balance.png"
    score_path = run_output_dir / f"{prefix}_score_progression.png"
    analysis_path = run_output_dir / f"{prefix}_analysis.md"
    manifest_path = run_output_dir / f"{prefix}_artifacts.txt"

    write_iteration_csv(series_list, csv_path)
    plot_messages: list[str] = []
    try:
        plot_delay_balance(series_list, scatter_path)
        plot_score_progression(series_list, score_path)
        plot_messages.extend(
            [
                f"Wrote {scatter_path}",
                f"Wrote {score_path}",
            ]
        )
    except RuntimeError as exc:
        plot_messages.append(f"Skipped PNG plots: {exc}")
    analysis_path.write_text(build_analysis(series_list), encoding="utf-8")
    manifest_path.write_text(
        "\n".join(
            [
                f"Wrote {csv_path}",
                *plot_messages,
                f"Wrote {analysis_path}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"Wrote {csv_path}")
    for message in plot_messages:
        print(message)
    print(f"Wrote {analysis_path}")
    print(f"Wrote {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
